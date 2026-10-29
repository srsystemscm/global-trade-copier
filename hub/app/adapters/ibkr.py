import asyncio
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
    `ib_async`.

    The client library is lazy-imported so it isn't a hard dependency for
    deployments that don't use IBKR -- `pip install ib_async` on the hub
    when you want it. Prefer `ib_async`: the original `ib_insync` was
    archived in March 2024 after its author died, and `ib_async` is the
    maintained drop-in successor. `ib_insync` is still accepted as a
    fallback so existing installs keep working, but new setups should not
    use it.

    Per the project's design decision, swapping a slave onto IBKR is a
    config change only -- Copy Engines only ever talk through the
    BrokerAdapter interface.

    Unlike Schwab, IBKR uses a single account for both futures and
    equities, so `account_type` is accepted for interface parity but
    ignored.

    Futures resolution: `_qualified_contract` asks for a `ContFuture` and
    then converts it to the dated contract behind it, because IBKR accepts a
    continuous contract for market data but not for order placement. The
    ContFuture step isn't incidental -- it's what picks the *liquid* front
    month rather than the nearest expiry, which for GC is an illiquid serial
    month. Cached contracts are re-resolved once their last trading day
    passes, so a long-running hub rolls over on its own.

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

    def __init__(
        self,
        slave_id: str,
        host: str,
        port: int,
        client_id: int,
        market_data_type: Optional[int] = None,
        quote_timeout: float = 1.0,
    ) -> None:
        self.slave_id = slave_id
        self.host = host
        self.port = port
        self.client_id = client_id
        # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen. None leaves IBKR's
        # default (live). A fresh account has no market data subscriptions,
        # so live returns NaN and get_quote() fails -- delayed (3) is free
        # and plenty for paper testing. See config/slaves.json.
        self.market_data_type = market_data_type
        self.quote_timeout = quote_timeout
        self.ib = None  # the client instance (ib_async.IB), created in connect()
        self._contracts: Dict[str, object] = {}
        self._positions: Dict[int, dict] = {}

    @staticmethod
    def _require_ib():
        """The ib_async module (or ib_insync as a legacy fallback).

        ib_async is an API-compatible fork, so everything below works
        against either one unchanged.
        """
        try:
            import ib_async

            return ib_async
        except ImportError:
            pass
        try:
            import ib_insync

            logger.warning(
                "using the archived ib_insync; ib_async is the maintained successor "
                "-- `pip install ib_async` on the hub"
            )
            return ib_insync
        except ImportError as exc:
            raise AdapterConnectionError(
                "no IBKR client library installed -- run `pip install ib_async` on the hub "
                "to use the IBKR adapter (kept optional so non-IBKR deployments don't need it)"
            ) from exc

    async def connect(self) -> None:
        ib_mod = self._require_ib()
        self.ib = ib_mod.IB()
        try:
            await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
        except Exception as exc:
            raise AdapterConnectionError(
                f"could not connect to IBKR TWS/Gateway at {self.host}:{self.port}: {exc}"
            ) from exc
        if self.market_data_type is not None:
            self.ib.reqMarketDataType(self.market_data_type)
            logger.info(
                "IBKRAdapter[%s] requesting market data type %s (%s)",
                self.slave_id,
                self.market_data_type,
                {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}.get(
                    self.market_data_type, "unknown"
                ),
            )
        logger.info(
            "IBKRAdapter[%s] connected to %s:%s (clientId=%s)", self.slave_id, self.host, self.port, self.client_id
        )

    async def disconnect(self) -> None:
        if self.ib is not None:
            self.ib.disconnect()
            self.ib = None

    def _contract_for(self, symbol: str):
        ib_mod = self._require_ib()
        spec = get_contract_spec(symbol)
        if spec:
            # spec.exchange, not a hardcoded "CME": the micro complex spans
            # COMEX (MGC), NYMEX (MCL) and CBOT (MYM) too, and IBKR's
            # qualifyContracts() rejects the wrong exchange outright.
            return ib_mod.ContFuture(symbol, exchange=spec.exchange, currency="USD")
        return ib_mod.Stock(symbol, exchange="SMART", currency="USD")

    @staticmethod
    def _is_expired(contract) -> bool:
        """True once a dated future's last trading day has passed."""
        expiry = getattr(contract, "lastTradeDateOrContractMonth", "")
        if not expiry or len(expiry) < 8:
            return False
        return expiry[:8] < time.strftime("%Y%m%d")

    async def _qualified_contract(self, symbol: str):
        assert self.ib is not None, "adapter not connected"

        cached = self._contracts.get(symbol)
        if cached is not None and not self._is_expired(cached):
            return cached

        ib_mod = self._require_ib()
        contract = self._contract_for(symbol)
        qualified = await self.ib.qualifyContractsAsync(contract)
        if not qualified:
            raise AdapterRejectedError(f"IBKR could not qualify a contract for symbol={symbol}")
        resolved = qualified[0]

        if isinstance(contract, ib_mod.ContFuture):
            # IBKR qualifies a ContFuture happily but refuses it for order
            # placement. Qualifying resolves it to the conId of the actual
            # front-month contract, so re-qualifying that conId as a dated
            # Future yields something placeable.
            #
            # Going through ContFuture rather than picking the nearest expiry
            # ourselves matters: for GC the nearest expiry is an illiquid
            # serial month (GCX6), while ContFuture follows liquidity and
            # picks GCZ6. Verified against a live Gateway.
            dated = await self.ib.qualifyContractsAsync(
                ib_mod.Future(conId=resolved.conId, exchange=resolved.exchange)
            )
            if not dated:
                raise AdapterRejectedError(
                    f"IBKR could not resolve a dated front-month contract for symbol={symbol}"
                )
            resolved = dated[0]
            logger.info(
                "IBKRAdapter[%s] %s -> %s (expiry %s, conId %s)",
                self.slave_id, symbol, resolved.localSymbol,
                resolved.lastTradeDateOrContractMonth, resolved.conId,
            )

        self._contracts[symbol] = resolved
        return resolved

    async def open(self, order: OrderRequest) -> OrderResult:
        ib_mod = self._require_ib()
        assert self.ib is not None, "adapter not connected"
        contract = await self._qualified_contract(order.symbol)
        action = "BUY" if order.direction == "BUY" else "SELL"
        closing_action = "SELL" if action == "BUY" else "BUY"

        entry = ib_mod.MarketOrder(action, order.size)
        trade = self.ib.placeOrder(contract, entry)
        slave_ticket = trade.order.orderId

        sl_order_id = tp_order_id = None
        if order.sl is not None or order.tp is not None:
            oca_group = f"tc-{self.slave_id}-{slave_ticket}"
            if order.sl is not None:
                sl_order = ib_mod.StopOrder(closing_action, order.size, order.sl)
                sl_order.ocaGroup = oca_group
                sl_order.ocaType = 1  # cancel the sibling order(s) once one fills
                sl_order_id = self.ib.placeOrder(contract, sl_order).order.orderId
            if order.tp is not None:
                tp_order = ib_mod.LimitOrder(closing_action, order.size, order.tp)
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
        ib_mod = self._require_ib()
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
            sl_order = ib_mod.StopOrder(closing_action, pos["size"], sl)
            sl_order.ocaGroup = oca_group
            sl_order.ocaType = 1
            sl_order_id = self.ib.placeOrder(pos["contract"], sl_order).order.orderId
        if tp is not None:
            tp_order = ib_mod.LimitOrder(closing_action, pos["size"], tp)
            tp_order.ocaGroup = oca_group
            tp_order.ocaType = 1
            tp_order_id = self.ib.placeOrder(pos["contract"], tp_order).order.orderId

        pos["sl_order_id"] = sl_order_id
        pos["tp_order_id"] = tp_order_id

    async def close(self, slave_ticket: int) -> None:
        ib_mod = self._require_ib()
        assert self.ib is not None, "adapter not connected"
        pos = self._positions.get(slave_ticket)
        if pos is None:
            raise AdapterRejectedError(f"no tracked IBKR position for slave_ticket={slave_ticket}")

        self._cancel_tracked_order(pos.get("sl_order_id"))
        self._cancel_tracked_order(pos.get("tp_order_id"))

        closing_action = "SELL" if pos["direction"] == "BUY" else "BUY"
        closing_order = ib_mod.MarketOrder(closing_action, pos["size"])
        self.ib.placeOrder(pos["contract"], closing_order)
        del self._positions[slave_ticket]

    async def get_status(self) -> bool:
        return self.ib is not None and self.ib.isConnected()

    async def get_account_summary(self, account_type: str) -> AccountSummary:
        assert self.ib is not None, "adapter not connected"
        # accountSummaryAsync(), NOT accountSummary(): the sync accessor falls
        # back to self._run() when its cache is cold, which raises
        # "RuntimeError: This event loop is already running" inside the hub's
        # loop. Same trap as IB.sleep(). Verified against a live Gateway.
        summary = {item.tag: item.value for item in await self.ib.accountSummaryAsync()}
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
        # asyncio.sleep, NOT ib.sleep(): IB.sleep() is a *synchronous* helper
        # that returns a bool, so `await self.ib.sleep(1)` raised
        # "object bool can't be used in 'await' expression" on every call.
        # It also drives the event loop re-entrantly, which is wrong inside a
        # running one -- it exists for sync scripts. (reqTickersAsync() is the
        # tidier one-shot if this ever needs revisiting.)
        await asyncio.sleep(self.quote_timeout)
        price = ticker.marketPrice()
        self.ib.cancelMktData(contract)
        if price != price:  # NaN: no price arrived
            if self.market_data_type in (3, 4):
                hint = (
                    "already requesting delayed data, so the usual cause is another IBKR "
                    "session holding the market data line -- IBKR allows one per account, and "
                    "a second login (mobile app, Client Portal, another TWS/Gateway) takes it, "
                    "logging 'Error 162: ... connected from a different IP address'. Otherwise "
                    "the account has no subscription covering this instrument."
                )
            else:
                hint = (
                    "a fresh account has no market data subscriptions, and live data then "
                    'returns nothing -- set "market_data_type": 3 (delayed, free) in the '
                    "slave's config, or subscribe to the relevant feed."
                )
            raise AdapterRejectedError(
                f"IBKR returned no market price for symbol={symbol} "
                f"(market_data_type={self.market_data_type}): {hint}"
            )
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
