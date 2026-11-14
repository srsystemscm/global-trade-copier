import asyncio
import json
import logging
import time
from typing import Dict, List, Optional

from pydantic import ValidationError

from app.config import settings
from app.db import db_cursor
from app.events import EventBus
from app.magic_filter import ALL as EVERY_MAGIC
from app.master_filter import EVERY_MASTER, MasterFilter
from app.magic_filter import MagicFilter
from app.models import Signal
from app.risk_controls import is_kill_switch_active

logger = logging.getLogger(__name__)


class SignalBus:
    """Validates incoming signals, deduplicates them, persists them to the
    Trade Registry, and fans them out to a per-slave asyncio.Queue.
    """

    def __init__(self, events: Optional[EventBus] = None) -> None:
        self._slave_queues: Dict[str, "asyncio.Queue[Signal]"] = {}
        self._seen: Dict[str, float] = {}  # f"{ticket}:{ts}" -> received_at
        # Which strategies each slave takes, by magic number. See
        # app/magic_filter.py for why the selection lives on the slave and
        # is applied here rather than in the engines.
        self._magic: Dict[str, MagicFilter] = {}
        self._masters: Dict[str, MasterFilter] = {}
        self.events = events

    def register_slave(
        self, slave_id: str, magic_filter: Optional[MagicFilter] = None
    ) -> "asyncio.Queue[Signal]":
        queue: "asyncio.Queue[Signal]" = asyncio.Queue()
        self._slave_queues[slave_id] = queue
        self._magic[slave_id] = magic_filter or EVERY_MAGIC
        logger.info("registered slave queue: %s (takes %s)",
                    slave_id, self._magic[slave_id].describe())
        return queue

    def set_master_filter(
        self, slave_id: str, master_filter: Optional[MasterFilter] = None
    ) -> None:
        """Change which masters a running slave takes, without a restart.

        Same reasoning as set_magic_filter: a selection that needed a hub
        restart would be the one piece of a slave's config you could not
        adjust while the market was open.
        """
        if slave_id not in self._slave_queues:
            return
        selection = master_filter or EVERY_MASTER
        self._masters[slave_id] = selection
        logger.info("slave=%s now takes %s", slave_id, selection.describe())

    def set_magic_filter(
        self, slave_id: str, magic_filter: Optional[MagicFilter] = None
    ) -> None:
        """Change which strategies a running slave takes, without a restart.

        PATCH /slaves/{id} already live-updates symbol maps and sizing; a
        magic selection that needed a hub restart would be the one piece of
        a slave's config you could not adjust while the market was open.
        """
        if slave_id not in self._slave_queues:
            return
        self._magic[slave_id] = magic_filter or EVERY_MAGIC
        logger.info("slave=%s now takes %s",
                    slave_id, self._magic[slave_id].describe())

    def unregister_slave(self, slave_id: str) -> None:
        self._slave_queues.pop(slave_id, None)
        self._magic.pop(slave_id, None)
        self._masters.pop(slave_id, None)
        logger.info("unregistered slave queue: %s", slave_id)

    def _recipients(self, signal: Signal) -> List[str]:
        """The slaves configured to take this signal.

        Decided before the arrival is announced, because the announcement
        reports how many slaves it fanned to -- and a count of every
        registered slave would be wrong for every signal on a hub where
        each slave takes a selection of the strategies.
        """
        taking = []
        for slave_id in self._slave_queues:
            # The MASTER is asked first. Magic numbers are per-master and
            # collide across them -- every fxDreema project numbers from
            # 1001 -- so "is this one of my masters" has to be settled
            # before "is this one of my strategies" means anything.
            accepts, why = self._masters.get(slave_id, EVERY_MASTER).accepts(signal)
            if accepts:
                accepts, why = self._magic.get(slave_id, EVERY_MAGIC).accepts(signal)
            if accepts:
                taking.append(slave_id)
            else:
                # Debug, not a warning, and never an event: a slave not
                # taking a strategy is this working, not a problem, and on
                # a master running forty EAs the warning would be most of
                # the log.
                logger.debug("slave=%s not taking ticket=%s: %s",
                             slave_id, signal.ticket, why)
        return taking

    def _dedup_key(self, signal: Signal) -> str:
        # `ts` comes from the master EA's TimeGMT(), which has whole-second
        # resolution, while the EA polls every 500ms -- so keying on
        # ticket+ts alone collides whenever two *different* actions land on
        # the same ticket inside one wall-clock second (e.g. an OPEN followed
        # by the broker setting SL/TP), silently dropping the second one.
        # Including the action still deduplicates a genuinely replayed
        # message, which is all this is meant to catch.
        #
        # The account leads the key because MT4 ticket numbers are per
        # account: with several masters feeding one hub, two of them can
        # legitimately send the same ticket number for different trades, and
        # keyed without the account the second is silently dropped as a dupe.
        return f"{signal.account}:{signal.ticket}:{signal.action}:{signal.ts}"

    def _is_duplicate(self, signal: Signal) -> bool:
        self._evict_expired()
        return self._dedup_key(signal) in self._seen

    def _evict_expired(self) -> None:
        cutoff = time.time() - settings.dedup_window_seconds
        expired = [k for k, seen_at in self._seen.items() if seen_at < cutoff]
        for k in expired:
            del self._seen[k]

    def _persist(self, signal: Signal, raw: dict) -> None:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT OR IGNORE INTO trades
                    (master_ticket, master_account, symbol, action, direction,
                     lots, price, sl, tp, signal_ts, received_at, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.ticket,
                    signal.account,
                    signal.symbol,
                    signal.action,
                    signal.direction,
                    signal.lots,
                    signal.price,
                    signal.sl,
                    signal.tp,
                    signal.ts,
                    time.time(),
                    json.dumps(raw),
                ),
            )

    async def publish_raw(self, raw_message: bytes) -> None:
        try:
            raw = json.loads(raw_message.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("dropping malformed signal message: %s", exc)
            return

        try:
            signal = Signal.model_validate(raw)
        except ValidationError as exc:
            logger.warning("dropping invalid signal: %s | raw=%s", exc, raw)
            return

        if self._is_duplicate(signal):
            logger.debug(
                "dropping duplicate signal ticket=%s action=%s ts=%s",
                signal.ticket, signal.action, signal.ts,
            )
            return

        self._seen[self._dedup_key(signal)] = time.time()
        # Stamped before anything is told about the signal, so every
        # consumer measures its own latency from the same moment.
        signal.received_at = time.time()
        self._persist(signal, raw)

        logger.info(
            "signal received: ticket=%s symbol=%s action=%s direction=%s lots=%s price=%s sl=%s tp=%s",
            signal.ticket, signal.symbol, signal.action, signal.direction,
            signal.lots, signal.price, signal.sl, signal.tp,
        )

        # Both decided before the arrival is announced so the announcement
        # can say how many slaves this signal actually reached. "Received"
        # is only worth reporting next to "copied" if a signal that routed
        # nowhere is distinguishable from one that was never sent.
        blocked = is_kill_switch_active()
        recipients = self._recipients(signal)

        if self.events:
            await self.events.emit(
                {
                    "type": "signal",
                    "ticket": signal.ticket,
                    # The hub reporter's identity fields, under the names
                    # every other event on this bus uses. `ticket`/`symbol`
                    # stay as they were: the dashboard's activity feed reads
                    # them.
                    "master_ticket": signal.ticket,
                    "master_account": signal.account,
                    "master_symbol": signal.symbol,
                    "magic": signal.magic,
                    "symbol": signal.symbol,
                    "action": signal.action,
                    "direction": signal.direction,
                    "lots": signal.lots,
                    "price": signal.price,
                    "sl": signal.sl,
                    "tp": signal.tp,
                    "atr": signal.atr,
                    "account": signal.account,
                    "ts": signal.ts,
                    "targets": 0 if blocked else len(recipients),
                }
            )

        if blocked:
            logger.warning("kill switch active, not fanning out ticket=%s to any slave", signal.ticket)
            if self.events:
                await self.events.emit({
                    "type": "kill_switch_blocked",
                    "ticket": signal.ticket,
                    "master_ticket": signal.ticket,
                    "master_account": signal.account,
                    "master_symbol": signal.symbol,
                    "magic": signal.magic,
                })
            return

        if not recipients:
            logger.info(
                "ticket=%s magic=%s reached no slave: none of %d is configured "
                "to take that strategy",
                signal.ticket, signal.magic, len(self._slave_queues),
            )
            return

        for slave_id in recipients:
            await self._slave_queues[slave_id].put(signal)
            logger.debug("fanned out ticket=%s to slave=%s", signal.ticket, slave_id)
