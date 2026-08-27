import logging
import time
from typing import Dict, List, Optional

from app.adapters.base import (
    AccountSummary,
    AdapterConnectionError,
    AdapterRejectedError,
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    Position,
)
from app.atr import Bar
from app.contract_specs import get_contract_spec

logger = logging.getLogger(__name__)


class IBKRAdapter(BrokerAdapter):
    """Talks to Interactive Brokers via TWS/IB Gateway's API, through
    ib_insync.

    Dormant: registered in the adapter factory and structurally complete to
    the same standard as SchwabAdapter, but not exercised against a real
    TWS/Gateway in this environment -- there's no way to do that without an
    actual IBKR account and a running terminal. `ib_insync` is lazy-imported
    so it isn't a hard dependency for deployments that don't use IBKR;
    `pip install ib_insync` when you're ready to migrate off Schwab (per
    the project's design decision, that migration should be a config change
    only -- Copy Engines only ever talk through the BrokerAdapter interface).

    Unlike Schwab, IBKR uses a single account for both futures and
    equities, so `account_type` is accepted for interface parity but
    ignored.

    KNOWN GAP: `_qualified_contract` resolves futures as `ContFuture`
    (continuous, auto-rolling), which is correct for quotes/historical bars
    but IBKR does not accept continuous contracts for order placement --
    real trading needs a dated `Future` contract for the current front
    month (via `reqContractDetails` or IB's `lastTradeDateOrContractMonth`).
    This needs to be resolved properly before `open()` is used for futures
    against a live account.

    TODO (deliberately deferred, not forgotten): unlike MT4Adapter and
    SchwabAdapter, none of this adapter's ib_insync calls are wrapped in
    app.retry.retry_async. That's intentional for now -- ib_insync doesn't
    raise typed exceptions the way httpx does, and guessing which generic
    exceptions are "transient" without ever having seen this adapter run
    would risk silently swallowing a real rejection as retryable. Add retry
    wrapping (connect() at minimum; likely open/modify/close/get_quote too)
    once this is actually connected to a real TWS/Gateway and its real
    failure modes are observable -- not before.
    """

    def __init__(self, slave_id: str, host: str, port: int, client_id: int) -> None:
        self.slave_id = slave_id
        self.host = host
        self.port = port
        self.client_id = client_id
        self.ib = None  # ib_insync.IB, created in connect()
        self._contracts: Dict[str, object] = {}
        self._positions: Dict[int, dict] = {}

    @staticmethod
    def _require_ib_insync():
        try:
            import ib_insync
        except ImportError as exc:
            raise AdapterConnectionError(
                "ib_insync is not installed -- run `pip install ib_insync` on the hub to use "
                "the IBKR adapter (it's kept optional since IBKR is dormant until you migrate "
                "off Schwab)"
            ) from exc
        return ib_insync

    async def connect(self) -> None:
        ib_insync = self._require_ib_insync()
        self.ib = ib_insync.IB()
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
        except Exception as exc:
            raise AdapterConnectionError(
                f"could not connect to IBKR TWS/Gateway at {self.host}:{self.port}: {exc}"
            ) from exc
        logger.info(
            "IBKRAdapter[%s] connected to %s:%s (clientId=%s)", self.slave_id, self.host, self.port, self.client_id
        )

    async def disconnect(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
            self.ib = None

    def _contract_for(self, symbol: str):
        ib_insync = self._require_ib_insync()
        if get_contract_spec(symbol):
            return ib_insync.ContFuture(symbol, exchange="CME", currency="USD")
        return ib_insync.Stock(symbol, exchange="SMART", currency="USD")

    async def _qualified_contract(self, symbol: str):
        assert self.ib is not None, "adapter not connected"
        if symbol not in self._contracts:
            contract = self._contract_for(symbol)
            qualified = await self.ib.qualifyContractsAsync(contract)
            if not qualified:
                raise AdapterRejectedError(f"IBKR could not qualify a contract for symbol={symbol}")
            self._contracts[symbol] = qualified[0]
        return self._contracts[symbol]

    async def open(self, order: OrderRequest) -> OrderResult:
        ib_insync = self._require_ib_insync()
        assert self.ib is not None, "adapter not connected"
        contract = await self._qualified_contract(order.symbol)
        action = "BUY" if order.direction == "BUY" else "SELL"
        closing_action = "SELL" if action == "BUY" else "BUY"

        entry = ib_insync.MarketOrder(action, order.size)
        trade = self.ib.placeOrder(contract, entry)
        slave_ticket = trade.order.orderId

        sl_order_id = tp_order_id = None
        if order.sl is not None or order.tp is not None:
            oca_group = f"tc-{self.slave_id}-{slave_ticket}"
            if order.sl is not None:
                sl_order = ib_insync.StopOrder(closing_action, order.size, order.sl)
                sl_order.ocaGroup = oca_group
                sl_order.ocaType = 1  # cancel the sibling order(s) once one fills
                sl_order_id = self.ib.placeOrder(contract, sl_order).order.orderId
            if order.tp is not None:
                tp_order = ib_insync.LimitOrder(closing_action, order.size, order.tp)
                tp_order.ocaGroup = oca_group
                tp_order.ocaType = 1
                tp_order_id = self.ib.placeOrder(contract, tp_order).order.orderId

        self._positions[slave_ticket] = {
            "contract": contract,
            "direction": order.direction,
            "size": order.size,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
        }
        logger.info(
            "IBKRAdapter[%s] opened %s %s x%s -> ticket=%s", self.slave_id, action, order.symbol, order.size, slave_ticket
        )
        return OrderResult(slave_ticket=slave_ticket)

    def _cancel_tracked_order(self, order_id: Optional[int]) -> None:
        if order_id is None:
            return
        trade = next((t for t in self.ib.trades() if t.order.orderId == order_id), None)
        if trade is not None:
            self.ib.cancelOrder(trade.order)

    async def modify(self, slave_ticket: int, sl: Optional[float], tp: Optional[float]) -> None:
        ib_insync = self._require_ib_insync()
        assert self.ib is not None, "adapter not connected"
        pos = self._positions.get(slave_ticket)
        if pos is None:
            raise AdapterRejectedError(f"no tracked IBKR position for slave_ticket={slave_ticket}")

        self._cancel_tracked_order(pos.get("sl_order_id"))
        self._cancel_tracked_order(pos.get("tp_order_id"))

        closing_action = "SELL" if pos["direction"] == "BUY" else "BUY"
        oca_group = f"tc-{self.slave_id}-{slave_ticket}-{int(time.time())}"
        sl_order_id = tp_order_id = None
        if sl is not None:
            sl_order = ib_insync.StopOrder(closing_action, pos["size"], sl)
            sl_order.ocaGroup = oca_group
            sl_order.ocaType = 1
            sl_order_id = self.ib.placeOrder(pos["contract"], sl_order).order.orderId
        if tp is not None:
            tp_order = ib_insync.LimitOrder(closing_action, pos["size"], tp)
            tp_order.ocaGroup = oca_group
            tp_order.ocaType = 1
            tp_order_id = self.ib.placeOrder(pos["contract"], tp_order).order.orderId

        pos["sl_order_id"] = sl_order_id
        pos["tp_order_id"] = tp_order_id

    async def close(self, slave_ticket: int) -> None:
        ib_insync = self._require_ib_insync()
        assert self.ib is not None, "adapter not connected"
        pos = self._positions.get(slave_ticket)
        if pos is None:
            raise AdapterRejectedError(f"no tracked IBKR position for slave_ticket={slave_ticket}")

        self._cancel_tracked_order(pos.get("sl_order_id"))
        self._cancel_tracked_order(pos.get("tp_order_id"))

        closing_action = "SELL" if pos["direction"] == "BUY" else "BUY"
        closing_order = ib_insync.MarketOrder(closing_action, pos["size"])
        self.ib.placeOrder(pos["contract"], closing_order)
        del self._positions[slave_ticket]

    async def get_status(self) -> bool:
        return self.ib is not None and self.ib.isConnected()

    async def get_account_summary(self, account_type: str) -> AccountSummary:
        assert self.ib is not None, "adapter not connected"
        summary = {item.tag: item.value for item in self.ib.accountSummary()}
        try:
            balance = float(summary["TotalCashValue"])
            equity = float(summary["NetLiquidation"])
        except KeyError as exc:
            raise AdapterRejectedError(f"IBKR account summary missing expected field: {exc}") from exc
        return AccountSummary(balance=balance, equity=equity)

    async def get_price_history(self, symbol: str, period: int) -> List[Bar]:
        assert self.ib is not None, "adapter not connected"
        contract = await self._qualified_contract(symbol)
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=f"{max(period + 5, 30)} D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
        )
        return [Bar(high=b.high, low=b.low, close=b.close) for b in bars]

    async def get_quote(self, symbol: str) -> float:
        assert self.ib is not None, "adapter not connected"
        contract = await self._qualified_contract(symbol)
        ticker = self.ib.reqMktData(contract, "", False, False)
        await self.ib.sleep(1)  # let the snapshot populate
        price = ticker.marketPrice()
        self.ib.cancelMktData(contract)
        if price != price:  # NaN: no price arrived
            raise AdapterRejectedError(f"IBKR returned no market price for symbol={symbol}")
        return float(price)

    async def get_open_positions(self, account_type: str) -> List[Position]:
        assert self.ib is not None, "adapter not connected"
        results = []
        for pos in self.ib.positions():
            if pos.position == 0:
                continue
            results.append(
                Position(symbol=pos.contract.symbol, quantity=pos.position, average_price=pos.avgCost)
            )
        return results
