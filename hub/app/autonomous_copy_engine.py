import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

from app import trade_registry
from app.adapters.base import AdapterError, BrokerAdapter, OrderRequest
from app.atr import compute_atr
from app.contract_specs import get_contract_spec
from app.events import EventBus
from app.models import Signal
from app.notifier import Notifier
from app.risk_controls import is_within_trading_hours
from app.session_gate import SessionGateMixin
from app.sizing import compute_size, resolve_sizing_config
from app.contract_roll import roll_sequence, volume_crossover
from app import position_store
from app.routing import Route, describe_refusal, resolve_routes

logger = logging.getLogger(__name__)


class AutonomousCopyEngine(SessionGateMixin):
    """Autonomous-mode copy engine: one instance per slave.

    Unlike mirror mode, this does not copy the master's absolute SL/TP.
    Instead, for each OPEN it:

    1. Back-calculates the ATR multiple the master's own SL/TP represent,
       using `signal.atr` (the master's own iATR() value, since fxDreema
       can't yet publish its internal ATR multiple directly -- see
       bridge_ea/TradeCopierBridge.mq4).
    2. Fans out across this slave's symbol_map (one master symbol can open
       several slave instruments, e.g. XAUUSD -> MGC and GLD).
    3. For each target, pulls that instrument's own ATR + current quote
       from the adapter and reapplies the same ATR multiple to compute a
       slave-native SL/TP.
    4. Sizes the order via the sizing engine (fixed/lot-multiplier/%risk/
       $notional) using the adapter's own account balance/equity.

    It follows MODIFY the only way it can: by re-deriving the ATR multiple
    the master's new levels represent and reapplying it to this instrument,
    the same translation OPEN uses. Copying the master's absolute price
    would put a gold CFD level on a GC futures contract.

    A background monitor loop also manages breakeven + trailing per
    position, so the two can disagree: a master that widens its stop while
    this leg has already moved to breakeven will see the monitor pull the
    stop back in on its next pass, because the monitor only ever moves a
    stop favourably. Last writer wins, as in mirror mode.

    It still follows CLOSE: when the master closes, all slave legs close.
    """

    def __init__(
        self,
        slave_id: str,
        adapter: BrokerAdapter,
        queue: "asyncio.Queue[Signal]",
        slave_config: Dict[str, Any],
        paused: bool = False,
        events: Optional[EventBus] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self.slave_id = slave_id
        self.adapter = adapter
        self.queue = queue
        self.paused = paused
        self.events = events
        self.notifier = notifier
        self.slave_config = slave_config
        self.atr_period = slave_config.get("atr_period", 14)
        self.risk_config = slave_config.get("risk_management", {})
        self.roll_config = slave_config.get("contract_roll", {})
        # Follow the master's MODIFY by re-deriving this leg's stops from the
        # ATR multiple the master's new levels represent. Off means the leg
        # is managed solely by the trailing monitor below, which is how this
        # engine behaved before.
        self.follow_master_modify = slave_config.get("follow_master_modify", True)
        # An open the broker cannot accept yet is held and retried rather
        # than dropped. CME halts its whole complex 17:00-18:00 ET, and an
        # MT4 server on GMT+2/+3 opens its daily bar right on that boundary
        # -- so a D1 strategy's entry lands inside the halt roughly once a
        # day. Dropped, it fills on the CFD legs and silently does not exist
        # on the futures one, with no registry row for reconciliation to
        # catch.
        defer = slave_config.get("defer_unavailable", {})
        self.defer_enabled = defer.get("enabled", True)
        self.defer_retry_seconds = defer.get("retry_seconds", 30)
        # 90 minutes covers the 60-minute halt with room for a late reopen.
        self.defer_max_seconds = defer.get("max_seconds", 5400)
        self.defer_poll_seconds = defer.get("poll_seconds", 5)
        # keyed (master_account, master_ticket, target, month_slot)
        self._deferred: Dict[tuple, dict] = {}
        self._last_roll_check = 0.0

        # Keyed (master_account, master_ticket, slave_symbol): ticket
        # numbers are per master account, so the account has to be in the key
        # or two masters' positions collide in here as well as in the tables.
        self._positions: Dict[Tuple[str, int, str], dict] = {}
        self._consume_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._deferred_task: Optional[asyncio.Task] = None

    async def restore(self) -> None:
        """Reload positions that were open when the hub last stopped.

        Called before the engine starts consuming signals, so a restart is
        invisible to a position's lifecycle: the trailing monitor picks it
        back up and a later CLOSE still finds it.
        """
        restored = 0
        for key, pos in position_store.load_all(self.slave_id):
            _master_account, master_ticket, slave_symbol = key
            try:
                await self.adapter.restore_position(
                    pos["slave_ticket"], {**pos, "slave_symbol": slave_symbol}
                )
            except Exception as exc:
                # Keep the engine's own record either way -- losing it would
                # strand the position entirely. A failed adapter restore just
                # means the broker-side handle has to be rebuilt later.
                logger.error(
                    "slave=%s could not restore adapter state for %s slave_ticket=%s: %s",
                    self.slave_id, slave_symbol, pos["slave_ticket"], exc,
                )
            self._positions[key] = pos
            restored += 1
        if restored:
            logger.info("slave=%s restored %d open position(s) across the restart",
                        self.slave_id, restored)

    def start(self) -> None:
        self._consume_task = asyncio.create_task(self._run(), name=f"autonomous-engine-{self.slave_id}")
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name=f"autonomous-monitor-{self.slave_id}")
        self._deferred_task = asyncio.create_task(
            self._deferred_loop(), name=f"autonomous-deferred-{self.slave_id}")

    async def stop(self) -> None:
        for task in (self._consume_task, self._monitor_task, self._deferred_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def _emit(self, event_type: str, **fields) -> None:
        if self.events:
            await self.events.emit({"type": event_type, "slave_id": self.slave_id, **fields})

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
                    "slave_error", master_ticket=signal.ticket, action=signal.action, message=str(exc)
                )

    async def _handle(self, signal: Signal) -> None:
        if signal.action == "MODIFY":
            await self._handle_modify(signal)
            return
        if signal.action == "OPEN":
            await self._handle_open(signal)
            return
        if signal.action == "CLOSE":
            await self._handle_close(signal)
            return

    async def _handle_open(self, signal: Signal) -> None:
        if not is_within_trading_hours():
            logger.info(
                "slave=%s skipping OPEN ticket=%s: outside configured trading hours",
                self.slave_id, signal.ticket,
            )
            await self._emit("trading_hours_blocked", master_ticket=signal.ticket)
            return

        routes = resolve_routes(self.slave_config, signal)
        if not routes:
            logger.warning("slave=%s refusing signal ticket=%s: %s",
                           self.slave_id, signal.ticket, describe_refusal(self.slave_config, signal))
            await self._emit("signal_unrouted", master_ticket=signal.ticket,
                             symbol=signal.symbol, magic=signal.magic)
            return

        if not signal.atr or signal.atr <= 0:
            logger.error(
                "slave=%s skipping OPEN ticket=%s: no usable master ATR on signal (fxDreema/EA not sending it?)",
                self.slave_id, signal.ticket,
            )
            return
        if signal.sl is None and signal.tp is None:
            logger.error(
                "slave=%s skipping OPEN ticket=%s: master signal has neither SL nor TP to back-calculate from",
                self.slave_id, signal.ticket,
            )
            return

        sl_mult = abs(signal.price - signal.sl) / signal.atr if signal.sl is not None else None
        tp_mult = abs(signal.tp - signal.price) / signal.atr if signal.tp is not None else None

        for route in routes:
            try:
                await self._open_one(signal, route, sl_mult, tp_mult)
            except AdapterError as exc:
                # Deliberately not classified by error text. IBKR's wording
                # for a closed market is not something to pin behaviour on,
                # and the alternative -- dropping anything unrecognised --
                # is the failure this exists to prevent. A genuinely bad
                # order retries until the ceiling and then says so, which is
                # noisier than dropping it and far easier to notice.
                await self._defer_open(signal, route, sl_mult, tp_mult, exc)

    async def _open_one(
        self, signal: Signal, route: Route, sl_mult: Optional[float], tp_mult: Optional[float]
    ) -> None:
        target_symbol = route.target

        # ATR and price both come from the contract actually traded. A back
        # month's own volatility is what its stops have to survive, and
        # borrowing the front month's would misstate it -- deferred months
        # damp differently, and the calendar spread between them is real
        # money (GCZ6 4051.6 vs GCJ7 4118.5).
        #
        # The cost is that thin back months give thin bars: slot 2 on gold
        # trades ~174 contracts/day. compute_atr() raises on insufficient
        # history and the open is skipped with a reason rather than sized off
        # a bad number.
        # The route's period when the strategy declares one, this slave's
        # otherwise. Pinned onto the position below so a roll re-derives the
        # new contract's ATR the same way the entry was measured.
        atr_period = route.atr_period or self.atr_period
        bar_size = route.bar_size

        bars = await self.adapter.get_price_history(
            target_symbol, atr_period, month_slot=route.month_slot, bar_size=bar_size
        )
        try:
            slave_atr = compute_atr(bars, atr_period)
        except ValueError as exc:
            logger.error("slave=%s skipping %s: %s", self.slave_id, target_symbol, exc)
            return

        quote = await self.adapter.get_quote(target_symbol, month_slot=route.month_slot)
        sign = 1 if signal.direction == "BUY" else -1
        slave_sl = quote - sign * sl_mult * slave_atr if sl_mult is not None else None
        slave_tp = quote + sign * tp_mult * slave_atr if tp_mult is not None else None

        spec = get_contract_spec(target_symbol)
        account_type = "futures" if spec else "brokerage"
        account = await self.adapter.get_account_summary(account_type)

        sizing_config = route.sizing or resolve_sizing_config(self.slave_config, target_symbol)
        sizing_result = compute_size(
            sizing_config=sizing_config,
            master_lots=signal.lots or 0.0,
            entry_price=quote,
            stop_price=slave_sl,
            account_balance=account.balance,
            account_equity=account.equity,
            contract_spec=spec,
        )
        if sizing_result.skipped:
            logger.warning("slave=%s skipping %s: %s", self.slave_id, target_symbol, sizing_result.reason)
            return

        order = OrderRequest(
            symbol=target_symbol,
            direction=signal.direction,
            size=sizing_result.size,
            sl=slave_sl,
            tp=slave_tp,
            master_ticket=signal.ticket,
            month_slot=route.month_slot,
        )
        requested_at = time.time()
        result = await self.adapter.open(order)
        filled_at = time.time()
        # master_price is the master's own entry on its own instrument, so
        # for this engine it is context rather than a slippage reference:
        # the slave is trading a different contract at a different price
        # scale. The comparable figure is slave_price against `quote`, the
        # level the stops were sized from, which is stored on the position.
        trade_registry.record_open(
            self.slave_id, signal.account, signal.ticket, target_symbol, result.slave_ticket,
            master_price=signal.price,
            slave_price=result.price,
            slave_lots=sizing_result.size,
            requested_at=requested_at,
            filled_at=filled_at,
        )

        # Pin the concrete contract. Slot numbers shift under you when the
        # front month rolls; an expiry doesn't, and the roll needs to know
        # exactly what it's moving from.
        try:
            info = await self.adapter.resolve_contract(target_symbol, route.month_slot)
            contract_id, contract_local, contract_expiry = (
                info.contract_id, info.local_symbol, info.expiry)
        except NotImplementedError:
            contract_id = contract_local = contract_expiry = None

        key = (signal.account, signal.ticket, target_symbol)
        self._positions[key] = {
            "slave_ticket": result.slave_ticket,
            "entry_price": quote,
            "direction": signal.direction,
            "atr": slave_atr,
            "sl": slave_sl,
            "tp": slave_tp,
            "breakeven_done": False,
            "size": sizing_result.size,
            "month_slot": route.month_slot,
            # per-route overrides, falling back to the slave's defaults
            "risk_config": route.risk_management or self.risk_config,
            "contract_id": contract_id,
            "contract_local": contract_local,
            "contract_expiry": contract_expiry,
            # kept so a roll can re-derive stops on the new contract's own ATR
            # rather than carrying absolute prices across a calendar spread
            "sl_mult": sl_mult,
            "tp_mult": tp_mult,
            "atr_period": atr_period,
            "bar_size": bar_size,
            "direction": signal.direction,
            "sl_order_id": result.sl_order_id,
            "tp_order_id": result.tp_order_id,
        }
        position_store.save(
            self.slave_id, signal.account, signal.ticket, target_symbol, self._positions[key]
        )
        logger.info(
            "slave=%s opened %s master_ticket=%s -> slave_ticket=%s size=%s sl=%s tp=%s",
            self.slave_id, target_symbol, signal.ticket, result.slave_ticket, sizing_result.size, slave_sl, slave_tp,
        )
        if route.month_slot:
            logger.info("slave=%s   (magic=%s -> month_slot=%s)", self.slave_id, signal.magic, route.month_slot)
        await self._emit(
            "slave_open",
            master_ticket=signal.ticket,
            slave_symbol=target_symbol,
            slave_ticket=result.slave_ticket,
            size=sizing_result.size,
            sl=slave_sl,
            tp=slave_tp,
        )

    async def _handle_modify(self, signal: Signal) -> None:
        """Re-derive this leg's stops from the master's new levels.

        Mirror mode copies the master's absolute SL/TP. This engine cannot:
        the slave is a different instrument at a different price scale, so
        a gold CFD stop of 3937 is meaningless on a GC contract. What does
        carry across is the *risk distance* measured in ATR multiples --
        exactly the translation _handle_open() performs -- so a master
        widening its stop from 1.5 to 3.0 ATR widens this leg by the same
        factor against this instrument's own ATR.

        The multiples are recomputed from the master's own entry price and
        ATR, both of which ride on every signal, rather than from the ones
        captured at open. That way a master whose ATR has moved since entry
        still describes its *current* intent.
        """
        if not self.follow_master_modify:
            logger.debug(
                "slave=%s ignoring MODIFY for ticket=%s (follow_master_modify is off)",
                self.slave_id, signal.ticket,
            )
            return

        if not signal.atr or signal.atr <= 0 or signal.price is None:
            # Without the master's ATR there is no multiple to carry across,
            # and guessing one would move a real stop on a real position.
            logger.warning(
                "slave=%s cannot follow MODIFY for ticket=%s: master sent atr=%s price=%s",
                self.slave_id, signal.ticket, signal.atr, signal.price,
            )
            return

        # SIGNED, unlike _handle_open(). At open a stop is always on the
        # losing side so its absolute distance is unambiguous, but a MODIFY
        # is usually the master moving its stop toward -- and then past --
        # breakeven. Taking abs() there would read a stop 15 points ABOVE a
        # long's entry as one 15 points below it, and push this leg's stop
        # the wrong way at the exact moment the master was protecting profit.
        m = 1 if signal.direction == "BUY" else -1
        sl_mult = m * (signal.price - signal.sl) / signal.atr if signal.sl is not None else None
        tp_mult = m * (signal.tp - signal.price) / signal.atr if signal.tp is not None else None

        matched = [
            (key, pos) for key, pos in self._positions.items()
            if key[0] == signal.account and key[1] == signal.ticket
        ]
        if not matched:
            logger.warning(
                "slave=%s MODIFY for unknown master_ticket=%s (never opened here?)",
                self.slave_id, signal.ticket,
            )
            return

        for key, pos in matched:
            target_symbol = key[2]
            sign = 1 if pos["direction"] == "BUY" else -1
            entry, slave_atr = pos["entry_price"], pos["atr"]

            slave_sl = entry - sign * sl_mult * slave_atr if sl_mult is not None else None
            slave_tp = entry + sign * tp_mult * slave_atr if tp_mult is not None else None

            try:
                await self.adapter.modify(pos["slave_ticket"], slave_sl, slave_tp)
            except AdapterError as exc:
                logger.error(
                    "slave=%s could not follow MODIFY on %s slave_ticket=%s: %s",
                    self.slave_id, target_symbol, pos["slave_ticket"], exc,
                )
                continue

            pos.update({"sl": slave_sl, "tp": slave_tp,
                        "sl_mult": sl_mult, "tp_mult": tp_mult})
            position_store.save(
                self.slave_id, signal.account, signal.ticket, target_symbol, pos
            )

            logger.info(
                "slave=%s followed MODIFY on %s slave_ticket=%s -> sl=%s tp=%s "
                "(%.4f/%.4f ATR of its own instrument)",
                self.slave_id, target_symbol, pos["slave_ticket"], slave_sl, slave_tp,
                sl_mult or 0.0, tp_mult or 0.0,
            )
            await self._emit(
                "slave_modify",
                master_ticket=signal.ticket,
                slave_symbol=target_symbol,
                slave_ticket=pos["slave_ticket"],
                sl=slave_sl,
                tp=slave_tp,
            )

    async def _defer_open(self, signal, route, sl_mult, tp_mult, exc) -> None:
        """Hold an open the broker refused, to try again shortly."""
        if not self.defer_enabled:
            logger.error(
                "slave=%s could not open %s for master_ticket=%s: %s",
                self.slave_id, route.target, signal.ticket, exc,
            )
            await self._emit("slave_error", master_ticket=signal.ticket,
                             action="OPEN", message=str(exc))
            return

        key = (signal.account, signal.ticket, route.target, route.month_slot)
        if key in self._deferred:
            return

        now = time.time()
        self._deferred[key] = {
            "signal": signal, "route": route, "sl_mult": sl_mult, "tp_mult": tp_mult,
            "first_at": now, "next_at": now + self.defer_retry_seconds,
            "attempts": 1, "last_error": str(exc),
        }
        logger.warning(
            "slave=%s deferring %s for master_ticket=%s, retrying every %ss for up "
            "to %ss: %s",
            self.slave_id, route.target, signal.ticket,
            self.defer_retry_seconds, self.defer_max_seconds, exc,
        )
        await self._emit("open_deferred", master_ticket=signal.ticket,
                         slave_symbol=route.target, reason=str(exc))

    async def _deferred_loop(self) -> None:
        """Retry held opens until the broker takes them, or time runs out.

        Each retry re-runs the whole of _open_one(), so the contract is
        re-priced and re-sized against live data at the moment it actually
        fills -- not against whatever could be read during the halt. What
        carries over from the original signal is the master's intent: the
        ATR multiple its stop represented.

        These live in memory. A hub restart mid-halt loses them, which is
        the honest limit of this: it narrows a daily certainty to a rare
        coincidence rather than removing the failure outright.
        """
        while True:
            await asyncio.sleep(self.defer_poll_seconds)
            if not self._deferred:
                continue
            if not self.session_up:
                # Nothing to gain from retrying into a dead session; the
                # watchdog is rebuilding it and the ceiling still applies.
                continue

            now = time.time()
            for key, item in list(self._deferred.items()):
                if now < item["next_at"]:
                    continue

                if now - item["first_at"] > self.defer_max_seconds:
                    del self._deferred[key]
                    await self._abandon_deferred(key, item)
                    continue

                item["attempts"] += 1
                try:
                    await self._open_one(item["signal"], item["route"],
                                         item["sl_mult"], item["tp_mult"])
                except AdapterError as exc:
                    item["last_error"] = str(exc)
                    item["next_at"] = now + self.defer_retry_seconds
                    continue

                del self._deferred[key]
                waited = now - item["first_at"]
                logger.info(
                    "slave=%s opened deferred %s for master_ticket=%s after %.0fs "
                    "and %d attempt(s)",
                    self.slave_id, key[2], key[1], waited, item["attempts"],
                )
                await self._emit("deferred_open_filled", master_ticket=key[1],
                                 slave_symbol=key[2], waited_seconds=round(waited))

    async def _abandon_deferred(self, key, item) -> None:
        """Give up, loudly. The master is in this trade and this leg is not."""
        msg = (
            f"Slave {self.slave_id} never opened {key[2]} for master_ticket={key[1]} "
            f"after {self.defer_max_seconds}s and {item['attempts']} attempts. The "
            f"master IS in this trade and this leg is NOT -- check it by hand. "
            f"Last error: {item['last_error']}"
        )
        logger.error(msg)
        await self._emit("deferred_open_abandoned", master_ticket=key[1],
                         slave_symbol=key[2], attempts=item["attempts"],
                         message=item["last_error"])
        if self.notifier:
            await self.notifier.notify(
                "deferred_open_abandoned", msg, slave_id=self.slave_id,
                master_ticket=key[1], slave_symbol=key[2],
            )

    def _cancel_deferred(self, account: str, master_ticket: int) -> int:
        """Drop held opens for a ticket the master has already closed.

        Without this a deferred entry outlives the trade it belongs to and
        opens a position the master is no longer in -- the copier putting on
        risk entirely by itself, which is worse than the missed fill it was
        trying to avoid.
        """
        stale = [k for k in self._deferred if k[0] == account and k[1] == master_ticket]
        for k in stale:
            del self._deferred[k]
        return len(stale)

    async def _handle_close(self, signal: Signal) -> None:
        dropped = self._cancel_deferred(signal.account, signal.ticket)
        if dropped:
            logger.warning(
                "slave=%s master closed ticket=%s before %d deferred open(s) could "
                "fill; dropping them rather than opening into a closed trade",
                self.slave_id, signal.ticket, dropped,
            )
            await self._emit("deferred_open_cancelled", master_ticket=signal.ticket,
                             dropped=dropped)

        mappings = trade_registry.get_open_mappings(self.slave_id, signal.account, signal.ticket)
        if not mappings:
            logger.warning(
                "slave=%s CLOSE for unknown master_ticket=%s (never opened here?)", self.slave_id, signal.ticket
            )
            return
        for mapping in mappings:
            slave_close = await self.adapter.close(mapping.slave_ticket)
            trade_registry.record_close(
                self.slave_id, signal.account, signal.ticket, mapping.slave_symbol,
                # 0 means the EA could not determine the exit; recorded as
                # "not captured" rather than as a price of zero.
                master_close_price=signal.price or None,
                slave_close_price=slave_close,
            )
            self._positions.pop((signal.account, signal.ticket, mapping.slave_symbol), None)
            position_store.delete(
                self.slave_id, signal.account, signal.ticket, mapping.slave_symbol
            )
            logger.info(
                "slave=%s closed %s slave_ticket=%s (master closed)",
                self.slave_id, mapping.slave_symbol, mapping.slave_ticket,
            )
            await self._emit(
                "slave_close",
                master_ticket=signal.ticket,
                slave_symbol=mapping.slave_symbol,
                slave_ticket=mapping.slave_ticket,
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

    async def _monitor_loop(self) -> None:
        interval = self.risk_config.get("poll_interval_seconds", 15)
        while True:
            await asyncio.sleep(interval)

            # Every check below needs the broker, so there is nothing to do
            # with a dead session but wait for the watchdog to rebuild it.
            # Skipping is also what keeps the task alive: a risk check
            # against a disconnected adapter raises AssertionError, which
            # isn't an AdapterError, so it would escape the handlers below
            # and end this task for the life of the process -- taking every
            # position's trailing stop with it, silently, and not coming
            # back when the session does.
            if not self.session_up:
                logger.debug("slave=%s broker session down, skipping monitor pass", self.slave_id)
                continue

            for key, pos in list(self._positions.items()):
                try:
                    await self._check_risk(key, pos)
                except AdapterError as exc:
                    logger.error("slave=%s risk check failed for %s: %s", self.slave_id, key, exc)
                except Exception:
                    # Belt and braces for the same failure mode: one bad
                    # position must not cost every other position its stop.
                    logger.exception("slave=%s risk check raised for %s", self.slave_id, key)
            try:
                await self._check_rolls()
            except AdapterError as exc:
                logger.error("slave=%s roll check failed: %s", self.slave_id, exc)
            except Exception:
                logger.exception("slave=%s roll check raised", self.slave_id)

    async def _check_rolls(self) -> None:
        """Coordinated per-symbol roll sweep, outermost expiry first.

        The trigger is evaluated once per symbol on the FRONT month, not per
        position. When the front is dying every position for that symbol has
        to shift together -- rolling only the front one would land it on the
        month the next strategy still holds, and on a netting account they'd
        merge.
        """
        if not self.roll_config.get("enabled", False) or not self._positions:
            return

        interval = float(self.roll_config.get("check_interval_seconds", 3600))
        now = time.time()
        if now - self._last_roll_check < interval:
            return
        self._last_roll_check = now

        by_symbol: Dict[str, list] = {}
        for key, pos in self._positions.items():
            if pos.get("contract_id") is not None:
                by_symbol.setdefault(key[2], []).append((key, pos))

        min_ratio = float(self.roll_config.get("min_volume_ratio", 1.0))
        for symbol, items in by_symbol.items():
            try:
                front = await self.adapter.get_contract_volume(symbol, month_slot=0)
                nxt = await self.adapter.get_contract_volume(symbol, month_slot=1)
            except NotImplementedError:
                continue

            decision = volume_crossover(front, nxt, min_ratio)
            logger.info("slave=%s roll check %s: %s", self.slave_id, symbol, decision.reason)
            if not decision.should_roll:
                continue

            for key, pos in roll_sequence(items):
                try:
                    await self._roll_one(key, pos, decision.reason)
                except AdapterError as exc:
                    # Stop the sweep: a half-rolled symbol is recoverable, but
                    # continuing would move the next position into the contract
                    # this one failed to vacate.
                    logger.error(
                        "slave=%s roll failed for %s (%s) -- halting this symbol's sweep: %s",
                        self.slave_id, symbol, pos.get("contract_local"), exc,
                    )
                    break

    async def _roll_one(self, key: Tuple[str, int, str], pos: dict, reason: str) -> None:
        master_account, master_ticket, symbol = key
        nxt = await self.adapter.next_contract_after(symbol, pos["contract_id"])
        if nxt is None:
            logger.error(
                "slave=%s cannot roll %s %s: no liquid contract beyond it",
                self.slave_id, symbol, pos.get("contract_local"),
            )
            return

        old_local = pos.get("contract_local")
        old_ticket = pos["slave_ticket"]

        # Stops are re-derived on the new contract's own ATR rather than
        # carried across. The calendar spread is real money (GCZ6 4051.6 vs
        # GCJ7 4118.5), so an absolute level transplanted between contracts is
        # simply wrong, and shift errors would compound over repeated rolls.
        # Whatever period measured this position at entry, not whatever
        # the slave's default happens to be now.
        roll_atr_period = pos.get("atr_period") or self.atr_period
        bars = await self.adapter.get_price_history(
            symbol, roll_atr_period, contract_id=nxt.contract_id,
            bar_size=pos.get("bar_size"),
        )
        try:
            new_atr = compute_atr(bars, roll_atr_period)
        except ValueError as exc:
            logger.error("slave=%s not rolling %s: %s", self.slave_id, symbol, exc)
            return
        quote = await self.adapter.get_quote(symbol, contract_id=nxt.contract_id)

        sign = 1 if pos["direction"] == "BUY" else -1
        sl_mult, tp_mult = pos.get("sl_mult"), pos.get("tp_mult")
        new_sl = quote - sign * sl_mult * new_atr if sl_mult is not None else None
        new_tp = quote + sign * tp_mult * new_atr if tp_mult is not None else None
        # A position already moved to breakeven keeps that protection: on the
        # new contract breakeven is the new entry, not the old price.
        if pos.get("breakeven_done") and (new_sl is None or sign * (quote - new_sl) > 0):
            new_sl = quote

        await self.adapter.close(old_ticket)
        order = OrderRequest(
            symbol=symbol,
            direction=pos["direction"],
            size=pos["size"],
            sl=new_sl,
            tp=new_tp,
            master_ticket=master_ticket,
            contract_id=nxt.contract_id,
        )
        result = await self.adapter.open(order)

        trade_registry.record_open(
            self.slave_id, master_account, master_ticket, symbol, result.slave_ticket
        )
        pos.update({
            "slave_ticket": result.slave_ticket,
            "entry_price": quote,
            "atr": new_atr,
            "sl": new_sl,
            "tp": new_tp,
            "contract_id": nxt.contract_id,
            "contract_local": nxt.local_symbol,
            "contract_expiry": nxt.expiry,
            "sl_order_id": result.sl_order_id,
            "tp_order_id": result.tp_order_id,
        })
        position_store.save(self.slave_id, master_account, master_ticket, symbol, pos)

        logger.info(
            "slave=%s rolled %s %s -> %s: slave_ticket %s -> %s, sl=%s tp=%s (%s)",
            self.slave_id, symbol, old_local, nxt.local_symbol,
            old_ticket, result.slave_ticket, new_sl, new_tp, reason,
        )
        await self._emit(
            "contract_roll",
            master_ticket=master_ticket,
            slave_symbol=symbol,
            from_contract=old_local,
            to_contract=nxt.local_symbol,
            slave_ticket=result.slave_ticket,
            sl=new_sl,
            tp=new_tp,
        )
        if self.notifier:
            await self.notifier.notify(
                "contract_roll",
                f"Slave {self.slave_id} rolled {symbol} {old_local} -> {nxt.local_symbol}",
                slave_id=self.slave_id,
                master_ticket=master_ticket,
                from_contract=old_local,
                to_contract=nxt.local_symbol,
            )

    async def _check_risk(self, key: Tuple[str, int, str], pos: dict) -> None:
        _master_account, _master_ticket, target_symbol = key
        quote = await self.adapter.get_quote(target_symbol, month_slot=pos.get("month_slot", 0))
        sign = 1 if pos["direction"] == "BUY" else -1
        favorable_move = sign * (quote - pos["entry_price"])
        atr = pos["atr"]

        risk_config = pos.get("risk_config") or self.risk_config
        be_trigger_atr = risk_config.get("breakeven_trigger_atr")
        trailing_atr = risk_config.get("trailing_atr")

        new_sl = pos["sl"]

        if be_trigger_atr and not pos["breakeven_done"] and favorable_move >= be_trigger_atr * atr:
            new_sl = pos["entry_price"]
            pos["breakeven_done"] = True

        if trailing_atr and pos["breakeven_done"]:
            candidate_sl = quote - sign * trailing_atr * atr
            if new_sl is None or sign * (candidate_sl - new_sl) > 0:
                new_sl = candidate_sl

        if new_sl is not None and new_sl != pos["sl"]:
            await self.adapter.modify(pos["slave_ticket"], new_sl, pos["tp"])
            pos["sl"] = new_sl
            position_store.save(self.slave_id, key[0], key[1], target_symbol, pos)
            logger.info(
                "slave=%s trailing %s slave_ticket=%s -> sl=%s",
                self.slave_id, target_symbol, pos["slave_ticket"], new_sl,
            )
            await self._emit(
                "slave_modify",
                master_ticket=key[1],
                slave_symbol=target_symbol,
                slave_ticket=pos["slave_ticket"],
                sl=new_sl,
                tp=pos["tp"],
            )
