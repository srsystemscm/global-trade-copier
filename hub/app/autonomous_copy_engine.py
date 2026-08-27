import asyncio
import logging
from typing import Any, Dict, Optional, Tuple

from app import trade_registry
from app.adapters.base import AdapterError, BrokerAdapter, OrderRequest
from app.atr import compute_atr
from app.contract_specs import get_contract_spec
from app.events import EventBus
from app.models import Signal
from app.notifier import Notifier
from app.risk_controls import is_within_trading_hours
from app.sizing import compute_size, resolve_sizing_config
from app.symbol_mapper import map_symbol

logger = logging.getLogger(__name__)


class AutonomousCopyEngine:
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

    It ignores MODIFY signals entirely -- once open, a background monitor
    loop manages its own breakeven + trailing stop per position. It still
    follows CLOSE: when the master closes, all slave legs close too.
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

        # keyed by (master_ticket, slave_symbol)
        self._positions: Dict[Tuple[int, str], dict] = {}
        self._consume_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._consume_task = asyncio.create_task(self._run(), name=f"autonomous-engine-{self.slave_id}")
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name=f"autonomous-monitor-{self.slave_id}")

    async def stop(self) -> None:
        for task in (self._consume_task, self._monitor_task):
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
            logger.debug(
                "slave=%s ignoring MODIFY for ticket=%s (autonomous mode manages its own risk)",
                self.slave_id, signal.ticket,
            )
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

        targets = map_symbol(self.slave_config, signal.symbol)
        if not targets:
            logger.warning("slave=%s no symbol_map entry for master symbol=%s", self.slave_id, signal.symbol)
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

        for target_symbol in targets:
            await self._open_one(signal, target_symbol, sl_mult, tp_mult)

    async def _open_one(
        self, signal: Signal, target_symbol: str, sl_mult: Optional[float], tp_mult: Optional[float]
    ) -> None:
        bars = await self.adapter.get_price_history(target_symbol, self.atr_period)
        try:
            slave_atr = compute_atr(bars, self.atr_period)
        except ValueError as exc:
            logger.error("slave=%s skipping %s: %s", self.slave_id, target_symbol, exc)
            return

        quote = await self.adapter.get_quote(target_symbol)
        sign = 1 if signal.direction == "BUY" else -1
        slave_sl = quote - sign * sl_mult * slave_atr if sl_mult is not None else None
        slave_tp = quote + sign * tp_mult * slave_atr if tp_mult is not None else None

        spec = get_contract_spec(target_symbol)
        account_type = "futures" if spec else "brokerage"
        account = await self.adapter.get_account_summary(account_type)

        sizing_config = resolve_sizing_config(self.slave_config, target_symbol)
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
        )
        result = await self.adapter.open(order)
        trade_registry.record_open(self.slave_id, signal.ticket, target_symbol, result.slave_ticket)

        self._positions[(signal.ticket, target_symbol)] = {
            "slave_ticket": result.slave_ticket,
            "entry_price": quote,
            "direction": signal.direction,
            "atr": slave_atr,
            "sl": slave_sl,
            "tp": slave_tp,
            "breakeven_done": False,
        }
        logger.info(
            "slave=%s opened %s master_ticket=%s -> slave_ticket=%s size=%s sl=%s tp=%s",
            self.slave_id, target_symbol, signal.ticket, result.slave_ticket, sizing_result.size, slave_sl, slave_tp,
        )
        await self._emit(
            "slave_open",
            master_ticket=signal.ticket,
            slave_symbol=target_symbol,
            slave_ticket=result.slave_ticket,
            size=sizing_result.size,
            sl=slave_sl,
            tp=slave_tp,
        )

    async def _handle_close(self, signal: Signal) -> None:
        mappings = trade_registry.get_open_mappings(self.slave_id, signal.ticket)
        if not mappings:
            logger.warning(
                "slave=%s CLOSE for unknown master_ticket=%s (never opened here?)", self.slave_id, signal.ticket
            )
            return
        for mapping in mappings:
            await self.adapter.close(mapping.slave_ticket)
            trade_registry.record_close(self.slave_id, signal.ticket, mapping.slave_symbol)
            self._positions.pop((signal.ticket, mapping.slave_symbol), None)
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
            for key, pos in list(self._positions.items()):
                try:
                    await self._check_risk(key, pos)
                except AdapterError as exc:
                    logger.error("slave=%s risk check failed for %s: %s", self.slave_id, key, exc)

    async def _check_risk(self, key: Tuple[int, str], pos: dict) -> None:
        _master_ticket, target_symbol = key
        quote = await self.adapter.get_quote(target_symbol)
        sign = 1 if pos["direction"] == "BUY" else -1
        favorable_move = sign * (quote - pos["entry_price"])
        atr = pos["atr"]

        be_trigger_atr = self.risk_config.get("breakeven_trigger_atr")
        trailing_atr = self.risk_config.get("trailing_atr")

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
            logger.info(
                "slave=%s trailing %s slave_ticket=%s -> sl=%s",
                self.slave_id, target_symbol, pos["slave_ticket"], new_sl,
            )
            await self._emit(
                "slave_modify",
                master_ticket=key[0],
                slave_symbol=target_symbol,
                slave_ticket=pos["slave_ticket"],
                sl=new_sl,
                tp=pos["tp"],
            )
