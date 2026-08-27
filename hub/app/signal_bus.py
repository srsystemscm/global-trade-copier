import asyncio
import json
import logging
import time
from typing import Dict, Optional

from pydantic import ValidationError

from app.config import settings
from app.db import db_cursor
from app.events import EventBus
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
        self.events = events

    def register_slave(self, slave_id: str) -> "asyncio.Queue[Signal]":
        queue: "asyncio.Queue[Signal]" = asyncio.Queue()
        self._slave_queues[slave_id] = queue
        logger.info("registered slave queue: %s", slave_id)
        return queue

    def unregister_slave(self, slave_id: str) -> None:
        self._slave_queues.pop(slave_id, None)
        logger.info("unregistered slave queue: %s", slave_id)

    def _dedup_key(self, signal: Signal) -> str:
        return f"{signal.ticket}:{signal.ts}"

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
            logger.debug("dropping duplicate signal ticket=%s ts=%s", signal.ticket, signal.ts)
            return

        self._seen[self._dedup_key(signal)] = time.time()
        self._persist(signal, raw)

        logger.info(
            "signal received: ticket=%s symbol=%s action=%s direction=%s lots=%s price=%s sl=%s tp=%s",
            signal.ticket, signal.symbol, signal.action, signal.direction,
            signal.lots, signal.price, signal.sl, signal.tp,
        )

        if self.events:
            await self.events.emit(
                {
                    "type": "signal",
                    "ticket": signal.ticket,
                    "symbol": signal.symbol,
                    "action": signal.action,
                    "direction": signal.direction,
                    "lots": signal.lots,
                    "price": signal.price,
                    "sl": signal.sl,
                    "tp": signal.tp,
                    "account": signal.account,
                    "ts": signal.ts,
                }
            )

        if is_kill_switch_active():
            logger.warning("kill switch active, not fanning out ticket=%s to any slave", signal.ticket)
            if self.events:
                await self.events.emit({"type": "kill_switch_blocked", "ticket": signal.ticket})
            return

        for slave_id, queue in self._slave_queues.items():
            await queue.put(signal)
            logger.debug("fanned out ticket=%s to slave=%s", signal.ticket, slave_id)
