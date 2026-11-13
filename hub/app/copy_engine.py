import asyncio
import contextlib
import logging
import time
from typing import Any, Dict, Optional

from app import mirror, trade_registry
from app.adapters.base import AdapterError, BrokerAdapter, OrderRequest
from app.events import EventBus
from app.models import Signal
from app.notifier import Notifier
from app.risk_controls import is_within_trading_hours
from app.session_gate import SessionGateMixin

logger = logging.getLogger(__name__)

# Both match the autonomous engine's. See AutonomousCopyEngine.stop() and
# CANCEL_RETRY_SECONDS there for why one cancellation is not enough.
STOP_GRACE_SECONDS = 5.0
CANCEL_RETRY_SECONDS = 0.25


def _elapsed_ms(signal: Signal, at: float) -> int:
    """Milliseconds from the hub first seeing this signal to `at`.

    Measured from `received_at`, the hub's own clock, and never from the
    master EA's `ts`: that is another machine's clock at whole-second
    resolution, and differences against it are mostly skew. Falls back to
    `ts` only for a signal that never went through the bus, which in
    practice means one a test constructed by hand.
    """
    return round((at - (signal.received_at or signal.ts)) * 1000)


class CopyEngine(SessionGateMixin):
    """Mirror-mode copy engine: one instance per slave.

    Copies OPEN with the absolute SL/TP the master sent, and follows MODIFY
    and CLOSE via the master_ticket -> slave_ticket mapping in the Trade
    Registry. One master symbol becomes exactly one slave symbol -- there is
    no fan-out here, so there's exactly one mapping per master_ticket -- but
    that one symbol need no longer be the master's own: see app.mirror, where
    `mirror_map` translates the instrument and converts the lots.
    """

    def __init__(
        self,
        slave_id: str,
        adapter: BrokerAdapter,
        queue: "asyncio.Queue[Signal]",
        slave_config: Optional[Dict[str, Any]] = None,
        paused: bool = False,
        events: Optional[EventBus] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self.slave_id = slave_id
        self.adapter = adapter
        self.queue = queue
        # Mirror mode needs little of this, but naming it slave_config means
        # PATCH /slaves/{id} live-updates mirror engines too: it refreshes
        # engine.slave_config wherever that attribute exists.
        self.slave_config = slave_config or {}
        self.paused = paused
        self.events = events
        self.notifier = notifier
        self._task: Optional[asyncio.Task] = None

    def _scaled_lots(self, lots: float):
        """The master's lots, scaled by risk_scale. None means "don't trade".

        Kept as a thin wrapper over app.mirror so that a slave with no
        mirror_map behaves exactly as it always did. The sizing itself moved
        there when the futures slave arrived: a scale factor is no longer the
        only thing between a master lot and a slave lot, because one BTCUSD
        lot is ten MBT_X contracts.
        """
        target, reason = mirror.resolve_target(self.slave_config, "", lots)
        if target is None:
            logger.warning("slave=%s %s", self.slave_id, reason)
            return None
        return target.lots

    async def _plan(self, signal):
        """Which instrument, how many lots, and where the stop goes.

        Two sizing models, chosen by whether the slave has a `sizing` block:

        With one, the size comes from a percentage of the SLAVE's own
        capital, which is how risk has ported to every other slave --
        ibkr-paper-1 risks 2% of its own equity per signal and the master's
        lot count never enters into it. It needs the slave's balance and the
        target's specification, so it only works against an EA that answers
        ACCOUNT and SYMBOLS (SlaveBridge.mq5 1.03+).

        Without one, the master's lots are converted by the instruments'
        point values and the master's own SL/TP are passed through. That is
        what mirror mode has always done, and slaves configured that way
        keep behaving exactly as before.
        """
        if not self.slave_config.get("sizing"):
            target, reason = mirror.resolve_target(
                self.slave_config, signal.symbol, signal.lots
            )
            if target is None:
                return None, reason
            # Whatever the map states, since lot mirroring never asks the
            # broker. Defaults to the 0.01 grid, as it always has.
            entry = (self.slave_config.get("mirror_map") or {}).get(signal.symbol)
            entry = entry if isinstance(entry, dict) else {}
            step = float(entry.get("volume_step") or mirror.DEFAULT_STEP)
            return mirror.MirrorPlan(
                symbol=target.symbol, lots=target.lots,
                sl=signal.sl, tp=signal.tp, how=target.how,
                volume_step=step,
                volume_min=float(entry.get("volume_min") or step),
            ), None

        symbol, route, reason = mirror.resolve_symbol(self.slave_config, signal.symbol)
        if symbol is None:
            return None, reason

        read_spec = getattr(self.adapter, "get_symbol_spec", None)
        if read_spec is None:
            return None, (
                f"{type(self.adapter).__name__} cannot report a symbol "
                f"specification, so this slave cannot size by risk"
            )
        try:
            spec = await read_spec(symbol)
            account = await self.adapter.get_account_summary("")
        except NotImplementedError as exc:
            # Refused, not sized some other way. Falling back to lot
            # mirroring here would put on a position sized by a model nobody
            # configured, which is worse than not trading.
            return None, (
                f"this slave is configured to size by risk, which needs the EA's "
                f"ACCOUNT and SYMBOLS commands: {exc}"
            )

        return mirror.plan_by_risk(
            self.slave_config, symbol, signal.direction,
            master_price=signal.price, master_sl=signal.sl, master_tp=signal.tp,
            spec=spec, balance=account.balance, equity=account.equity,
        )

    async def _fit_margin(self, plan, signal):
        """The configured margin ceiling, applied to a sized order.

        Returns (lots, None) or (None, reason). Opt-in via a `margin` block:
        it costs a round trip, and a slave whose instruments are nowhere near
        its margin has no use for it.
        """
        policy = self.slave_config.get("margin")
        if policy is None:
            return plan.lots, None

        preview_margin = getattr(self.adapter, "preview_margin", None)
        if preview_margin is None:
            return None, (
                f"a margin ceiling is configured but "
                f"{type(self.adapter).__name__} cannot preview margin"
            )

        try:
            preview = await preview_margin(OrderRequest(
                symbol=plan.symbol, direction=signal.direction, size=plan.lots,
                sl=plan.sl, tp=plan.tp, master_ticket=signal.ticket,
            ))
        except NotImplementedError as exc:
            # Switched on but unanswerable: refusing is the only honest
            # outcome. Trading past a ceiling because the EA is old would
            # make the setting decorative.
            return None, (
                f"a margin ceiling is configured, which needs the EA's MARGIN "
                f"command (SlaveBridge.mq5 1.05+): {exc}"
            )
        except AdapterError as exc:
            return None, f"could not price the margin for this order: {exc}"

        lots, note, refusal = mirror.margin_fit(
            plan.lots, preview, policy,
            volume_step=plan.volume_step, volume_min=plan.volume_min,
        )
        if refusal:
            return None, refusal
        if note:
            logger.warning("slave=%s ticket=%s %s: %s",
                           self.slave_id, signal.ticket, plan.symbol, note)
            asked = plan.lots
            plan.how += f" -- {note}"
            # The numbers, not only the sentence. A reduction is the one
            # case where the size on the account is not the size the risk
            # model chose, so "how much of the asked risk is this leg
            # actually carrying" has to be answerable without parsing prose.
            await self._emit(
                "margin_reduced", signal=signal,
                slave_symbol=plan.symbol, size=lots, message=note,
                asked=asked, sent=lots,
                margin_required=getattr(preview, "initial_change", None),
                margin_free=getattr(preview, "free_before", None),
                ceiling_pct=policy.get("max_pct_of_free"),
                risk_fraction_carried=(round(lots / asked, 6) if asked else None),
            )
        return lots, None

    async def _slave_positions(self):
        """The slave's open positions, or None when it cannot say.

        None is not an empty book, and the difference matters: an empty book
        permits a trade and an unknown book should not be read as permission.
        """
        read = getattr(self.adapter, "get_open_positions", None)
        if read is None:
            # Asked explicitly rather than catching AttributeError, which
            # would also swallow one raised inside a working implementation.
            return None
        try:
            return await read()
        except NotImplementedError:
            # An EA older than the POSITIONS command. Reported by the adapter
            # with a recompile instruction of its own; nothing to add here.
            return None
        except AdapterError as exc:
            logger.warning(
                "slave=%s could not read open positions for the netting check: %s",
                self.slave_id, exc,
            )
            return None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"copy-engine-{self.slave_id}")

    async def stop(self) -> None:
        """Stop the consume loop. Bounded, for the same reason the
        autonomous engine's is: `stop_engine` is called from the app's
        lifespan AND from PATCH/DELETE /slaves, so an unbounded wait is an
        API request that never answers as well as a shutdown that hangs.
        """
        if self._task is None:
            return
        # Cancel and ask again until it stops or the grace runs out. One
        # cancellation is not enough: this loop parks on `queue.get()`, and
        # a cancellation arriving just as an item lands is deferred and
        # then lost. See the autonomous engine's CANCEL_RETRY_SECONDS.
        pending = {self._task}
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        rounds = 0
        while pending and time.monotonic() < deadline:
            rounds += 1
            self._task.cancel()
            _done, pending = await asyncio.wait(
                pending, timeout=CANCEL_RETRY_SECONDS)

        with contextlib.suppress(asyncio.CancelledError, Exception):
            if self._task.done():
                self._task.result()

        if pending:
            logger.error(
                "slave=%s the consume loop did not stop within %.0fs and has "
                "been abandoned; shutting this engine down anyway",
                self.slave_id, STOP_GRACE_SECONDS,
            )
        elif rounds > 1:
            logger.warning(
                "slave=%s stopped after %d cancellation rounds -- a "
                "cancellation was lost and had to be repeated",
                self.slave_id, rounds,
            )

    async def _emit(self, event_type: str, signal: Optional[Signal] = None, **fields) -> None:
        """Put an event on the bus, stamped with the trade it belongs to.

        `signal=` fills master_ticket, master_account, magic and
        master_symbol from one place. All four identify the trade on the
        Operations hub's side and three of them were missing from every
        event this engine emitted, because each call site passed
        `master_ticket=signal.ticket` and stopped there. `magic` matters
        most: it is how a copy is attributed to the strategy that produced
        it, and the master EAs set it precisely so that attribution is
        possible downstream.

        Call sites that hold a key rather than a signal -- the trailing
        monitor, the roll -- still pass the fields they have by hand.
        """
        if not self.events:
            return
        payload: Dict[str, Any] = {"type": event_type, "slave_id": self.slave_id}
        if signal is not None:
            payload.update(
                master_ticket=signal.ticket,
                master_account=signal.account,
                magic=signal.magic,
                master_symbol=signal.symbol,
            )
        payload.update(fields)
        await self.events.emit(payload)

    async def _run(self) -> None:
        while True:
            signal = await self.queue.get()
            if self.paused:
                logger.debug("slave=%s paused, dropping %s ticket=%s", self.slave_id, signal.action, signal.ticket)
                continue
            if await self.refuse_while_down(signal):
                continue
            try:
                await self._handle(signal)
            except AdapterError as exc:
                logger.error(
                    "slave=%s failed to handle %s ticket=%s: %s",
                    self.slave_id, signal.action, signal.ticket, exc,
                )
                await self._emit(
                    "slave_error", signal=signal, action=signal.action, message=str(exc)
                )

    async def _handle(self, signal: Signal) -> None:
        if signal.action == "OPEN":
            if not is_within_trading_hours():
                logger.info(
                    "slave=%s skipping OPEN ticket=%s: outside configured trading hours",
                    self.slave_id, signal.ticket,
                )
                await self._emit("trading_hours_blocked", signal=signal)
                return

            # Already copied. The dedup in SignalBus keys on the EA's
            # timestamp, which has whole-second resolution -- so two copies
            # of one signal published milliseconds apart can land either
            # side of a second boundary and both get through. Seen live: a
            # second TradeCopierBridge instance on another chart produced
            # exactly that. A duplicate CLOSE is harmless because the
            # mapping is already gone, but a duplicate OPEN would put on a
            # second position and record_open() would upsert over the first,
            # leaving a live position nothing is tracking.
            existing = trade_registry.get_open_mappings(
                self.slave_id, signal.account, signal.ticket
            )
            if existing:
                logger.warning(
                    "slave=%s already holding master_ticket=%s as slave_ticket=%s, "
                    "ignoring a repeat OPEN",
                    self.slave_id, signal.ticket, existing[0].slave_ticket,
                )
                await self._emit("duplicate_open_ignored", signal=signal,
                                 slave_ticket=existing[0].slave_ticket)
                return

            plan, reason = await self._plan(signal)
            if plan is None:
                logger.warning(
                    "slave=%s refusing OPEN ticket=%s: %s",
                    self.slave_id, signal.ticket, reason,
                )
                await self._emit("slave_error", signal=signal,
                                 action="OPEN", message=reason)
                return
            target, lots = plan, plan.lots

            # The margin ceiling, where one is configured. Last, because it
            # trims a size the risk model has already decided -- and it can
            # only be asked once the size exists.
            lots, reason = await self._fit_margin(plan, signal)
            if lots is None:
                logger.warning(
                    "slave=%s refusing OPEN ticket=%s on %s: %s",
                    self.slave_id, signal.ticket, plan.symbol, reason,
                )
                await self._emit("margin_blocked", signal=signal,
                                 slave_symbol=plan.symbol, message=reason,
                                 reason=reason)
                return

            # Checked against what the account actually holds, not against what
            # this copier opened. A slave that runs its own strategies can
            # already be positioned in the target instrument, and on a hedging
            # account an opposing copy sits alongside it rather than being
            # refused by the terminal.
            #
            # Opt-in per slave, and deliberately so. A master running two
            # strategies that hold both sides of one symbol is a legitimate
            # workflow this would block, and it costs a POSITIONS round trip
            # on every OPEN -- neither belongs to a slave that never asked.
            # `is not None`, not truthiness: "netting": {} means "on, with the
            # defaults", and an empty dict is falsy. Read as a boolean it
            # would switch the guard off at exactly the moment someone had
            # just switched it on.
            netting_policy = self.slave_config.get("netting")
            positions = (
                await self._slave_positions() if netting_policy is not None else None
            )
            if positions is not None:
                refusal = mirror.netting_refusal(
                    positions, target.symbol, signal.direction, lots, netting_policy,
                )
                if refusal:
                    logger.warning(
                        "slave=%s refusing OPEN ticket=%s on %s: %s",
                        self.slave_id, signal.ticket, target.symbol, refusal,
                    )
                    await self._emit(
                        "netting_blocked", signal=signal,
                        slave_symbol=target.symbol, message=refusal,
                        reason=refusal, direction=signal.direction,
                        # What the account was already holding when the leg
                        # was turned away. Read back off the same positions
                        # the refusal was decided from, rather than asked
                        # for again: a second POSITIONS round trip could
                        # answer differently and then the reason and the
                        # number beside it would disagree.
                        net_held=sum(
                            float(getattr(p, "quantity", 0.0) or 0.0)
                            for p in positions
                            if getattr(p, "symbol", None) == target.symbol
                        ),
                    )
                    return

            logger.info(
                "slave=%s ticket=%s %s -> %s: %s",
                self.slave_id, signal.ticket, signal.symbol, target.symbol, target.how,
            )

            order = OrderRequest(
                symbol=target.symbol,
                direction=signal.direction,
                size=lots,
                # plan.sl/tp, not signal.sl/tp. Where the slave trades a
                # different instrument these are the master's levels
                # translated onto the target's own price scale; where it
                # trades the same one they are the master's levels unchanged.
                sl=plan.sl,
                tp=plan.tp,
                master_ticket=signal.ticket,
            )
            # Timed either side of the adapter call so the two halves of
            # copy latency stay separable: queue-and-hub time is
            # requested_at - signal.ts, broker time is filled_at -
            # requested_at. Blaming the wrong half is the usual way latency
            # work goes nowhere.
            requested_at = time.time()
            result = await self.adapter.open(order)
            filled_at = time.time()
            trade_registry.record_open(
                # The TARGET symbol, not the master's: this column is the
                # slave's own, reconciliation matches open positions on it,
                # and MODIFY/CLOSE read it back out of the mapping.
                self.slave_id, signal.account, signal.ticket, target.symbol,
                result.slave_ticket,
                master_price=signal.price,
                slave_price=result.price,
                slave_lots=result.lots if result.lots is not None else lots,
                requested_at=requested_at,
                filled_at=filled_at,
            )
            logger.info(
                "slave=%s opened master_ticket=%s as slave_ticket=%s",
                self.slave_id, signal.ticket, result.slave_ticket,
            )
            await self._emit(
                "slave_open",
                signal=signal,
                slave_symbol=target.symbol,
                slave_ticket=result.slave_ticket,
                # What actually went on, where the broker says so. The
                # requested size is what was asked for, not what is held.
                size=result.lots if result.lots is not None else lots,
                # One line saying how the size was arrived at, so a figure
                # on the hub can be explained without the hub log.
                sizing=target.how,
                # Mirror mode aims at the master's own level: same
                # instrument, or one mapped onto it. It is the reference
                # the fill is worth comparing against.
                requested_price=signal.price,
                fill_price=result.price,
                # plan's levels, not the master's: where the slave trades a
                # different instrument these are the translated ones, and
                # they are what was actually placed.
                sl=plan.sl,
                tp=plan.tp,
                # One order per copy here -- mirror mode never splits.
                legs=1,
                latency_ms=round((filled_at - requested_at) * 1000),
                signal_to_fill_ms=_elapsed_ms(signal, filled_at),
            )
            return

        mappings = trade_registry.get_open_mappings(self.slave_id, signal.account, signal.ticket)
        if not mappings:
            logger.warning(
                "slave=%s %s for unknown master_ticket=%s (never opened here?)",
                self.slave_id, signal.action, signal.ticket,
            )
            return

        for mapping in mappings:
            if signal.action == "MODIFY":
                await self.adapter.modify(mapping.slave_ticket, signal.sl, signal.tp)
                logger.info("slave=%s modified slave_ticket=%s", self.slave_id, mapping.slave_ticket)
                await self._emit(
                    "slave_modify",
                    signal=signal,
                    slave_symbol=mapping.slave_symbol,
                    slave_ticket=mapping.slave_ticket,
                    sl=signal.sl,
                    tp=signal.tp,
                    legs_moved=1,
                )
            elif signal.action == "CLOSE":
                requested_at = time.time()
                slave_close = await self.adapter.close(mapping.slave_ticket)
                closed_at = time.time()
                trade_registry.record_close(
                    self.slave_id, signal.account, signal.ticket, mapping.slave_symbol,
                    # 0 means the EA could not determine the exit; recorded as
                    # "not captured" rather than as a price of zero.
                    master_close_price=signal.price or None,
                    slave_close_price=slave_close,
                )
                logger.info("slave=%s closed slave_ticket=%s", self.slave_id, mapping.slave_ticket)
                await self._emit(
                    "slave_close",
                    signal=signal,
                    slave_symbol=mapping.slave_symbol,
                    slave_ticket=mapping.slave_ticket,
                    fill_price=slave_close,
                    latency_ms=round((closed_at - requested_at) * 1000),
                )
                if self.notifier:
                    await self.notifier.notify(
                        "trade_close",
                        f"Slave {self.slave_id} closed {mapping.slave_symbol} (slave_ticket={mapping.slave_ticket})",
                        slave_id=self.slave_id,
                        master_ticket=signal.ticket,
                        slave_symbol=mapping.slave_symbol,
                        slave_ticket=mapping.slave_ticket,
                    )
