import asyncio
import logging
import math
import time
from typing import Any, Dict, Optional

from app import trade_registry
from app.adapters.base import AdapterError, BrokerAdapter, OrderRequest
from app.events import EventBus
from app.models import Signal
from app.notifier import Notifier
from app.risk_controls import is_within_trading_hours
from app.session_gate import SessionGateMixin

logger = logging.getLogger(__name__)


class CopyEngine(SessionGateMixin):
    """Mirror-mode copy engine: one instance per slave.

    Copies OPEN with the absolute SL/TP the master sent, and follows MODIFY
    and CLOSE via the master_ticket -> slave_ticket mapping in the Trade
    Registry. Mirror mode always maps one master symbol to itself (no
    symbol_map fan-out), so there's exactly one mapping per master_ticket.
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

        Mirror mode has no sizing model to lean on, so the rounding is done
        here: MT4/MT5 volumes are quoted to 0.01, and a scaled lot that
        doesn't land on that grid is rejected by the terminal rather than
        clamped. The slave bridge still has the last word -- it knows the
        broker's own min/max/step -- but sending 0.0075 and letting it refuse
        would turn a configuration mistake into a missed trade with a
        confusing error.
        """
        scale = float(self.slave_config.get("risk_scale", 1.0))
        if scale == 1.0:
            return lots
        if scale <= 0:
            logger.error(
                "slave=%s risk_scale is %s, which is not a tradeable fraction; "
                "refusing the trade rather than sending a zero or inverted size",
                self.slave_id, scale,
            )
            return None
        # Floored to the 0.01 grid, not rounded. This dial's job is to
        # carry LESS risk than the master, so it must never hand back more
        # than was asked for. round() would also be erratic here: 0.075 is
        # not exactly representable in binary, so round(0.075, 2) is 0.07
        # while round(0.125, 2) is 0.12 by banker's rounding -- the same
        # input shape giving different answers for invisible reasons.
        scaled = math.floor(lots * scale * 100) / 100
        if scaled < 0.01:
            logger.warning(
                "slave=%s risk_scale %s takes %s lots down to %s, below the 0.01 "
                "minimum volume; skipping rather than sending an order the "
                "terminal will reject",
                self.slave_id, scale, lots, scaled,
            )
            return None
        logger.info(
            "slave=%s risk_scale %s: %s lots -> %s", self.slave_id, scale, lots, scaled,
        )
        return scaled

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"copy-engine-{self.slave_id}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
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
        if signal.action == "OPEN":
            if not is_within_trading_hours():
                logger.info(
                    "slave=%s skipping OPEN ticket=%s: outside configured trading hours",
                    self.slave_id, signal.ticket,
                )
                await self._emit("trading_hours_blocked", master_ticket=signal.ticket)
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
                await self._emit("duplicate_open_ignored", master_ticket=signal.ticket,
                                 slave_ticket=existing[0].slave_ticket)
                return

            lots = self._scaled_lots(signal.lots)
            if lots is None:
                await self._emit("slave_error", master_ticket=signal.ticket,
                                 action="OPEN",
                                 message=f"risk_scale left no tradeable volume for "
                                         f"{signal.lots} lots")
                return

            order = OrderRequest(
                symbol=signal.symbol,
                direction=signal.direction,
                size=lots,
                sl=signal.sl,
                tp=signal.tp,
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
                self.slave_id, signal.account, signal.ticket, signal.symbol, result.slave_ticket,
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
                master_ticket=signal.ticket,
                slave_symbol=signal.symbol,
                slave_ticket=result.slave_ticket,
                size=lots,
                sl=signal.sl,
                tp=signal.tp,
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
                    master_ticket=signal.ticket,
                    slave_symbol=mapping.slave_symbol,
                    slave_ticket=mapping.slave_ticket,
                    sl=signal.sl,
                    tp=signal.tp,
                )
            elif signal.action == "CLOSE":
                slave_close = await self.adapter.close(mapping.slave_ticket)
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
