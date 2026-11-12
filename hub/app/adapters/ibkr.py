import asyncio
import logging
import time
from typing import Dict, List, Optional

from app.adapters.base import (
    AccountSummary,
    ContractInfo,
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

# How long one bar of each IBKR size covers, in seconds.
_BAR_SECONDS = {
    "secs": 1, "sec": 1, "min": 60, "mins": 60, "hour": 3600, "hours": 3600,
    "day": 86400, "week": 604800, "month": 2592000,
}


def _duration_for(period: int, bar_size: str) -> str:
    """How much history to ask IBKR for to get `period` + 5 bars of this size.

    A fixed "30 D" was fine while every caller wanted daily bars, but it
    breaks both ways once the size varies: 30 days of 1-minute bars is tens
    of thousands of bars and IBKR refuses the request outright, while 30
    days of weekly bars is only four and compute_atr() would reject it.

    The x3 covers the gap between calendar time and session time -- weekends
    and the overnight break mean a day of wall clock is nothing like a day of
    RTH bars, and asking for too little is the failure that costs an entry.
    """
    want = period + 5
    parts = bar_size.split()
    try:
        count = int(parts[0])
        seconds = _BAR_SECONDS[parts[1].lower()] * count
    except (IndexError, ValueError, KeyError):
        raise AdapterRejectedError(
            f"unrecognised bar size {bar_size!r} -- expected something like "
            f"'1 day', '4 hours' or '15 mins'"
        )

    if seconds >= 86400:
        return f"{max(int(want * seconds / 86400) + 1, 30)} D"

    # Sub-daily: ask in days, since IBKR caps the seconds form at 86400.
    days = int((want * seconds * 3) / 86400) + 1
    return f"{min(days, 365)} D"


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

    # IB Gateway force-restarts daily and this session dies with it. Nothing
    # in ib_async re-establishes it, so the watchdog has to.
    requires_session_recovery = True

    def __init__(
        self,
        slave_id: str,
        host: str,
        port: int,
        client_id: int,
        market_data_type: Optional[int] = None,
        quote_timeout: float = 1.0,
        bracket_tif: str = "GTC",
        entry_tif: str = "DAY",
        fill_timeout: float = 10.0,
        entry_timeout: float = 900.0,
        account: Optional[str] = None,
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
        # how long to let IBKR's async rejection of a bracket leg arrive
        self.bracket_check_delay = 1.0
        # Time in force for the protective legs. GTC by default because a
        # stop that expires at the session close leaves the position naked
        # overnight, silently -- and IBKR's account-level order preset
        # supplies DAY whenever the order doesn't say otherwise ("Error
        # 10349: Order TIF was set to DAY based on order preset"). Relying on
        # a GUI preset the code can't see means a fresh Gateway install
        # regresses it without a trace.
        self.bracket_tif = bracket_tif
        # Time in force for the ENTRY, stated explicitly for the same reason
        # the brackets are: left unset, IBKR's account preset supplies DAY
        # and reports it as "Error 10349", cancelling the order and
        # re-creating it before it fills. The fill survives that, but the
        # order passes through Cancelled on the way, so anything watching
        # order state sees a failure that isn't one. DAY is what the preset
        # was imposing anyway, and it is what an entry should have: a market
        # order that cannot fill is better off expiring than resting.
        self.entry_tif = entry_tif
        # How long to wait for the fill before recording the trade without a
        # price. The order is live at the broker either way -- a timeout here
        # means "no price captured", never "no position".
        self.fill_timeout = fill_timeout
        # How long a LIMIT entry may rest before it is cancelled outright.
        # Not the same question as fill_timeout: a market order that hasn't
        # filled in ten seconds is a problem, while a limit resting in a thin
        # evening book for ten minutes is the plan. What must not happen is
        # resting forever -- see abandon_unfilled_entry().
        self.entry_timeout = entry_timeout
        # Which of the login's accounts this slave trades. Empty means
        # "work it out at connect time" -- see _resolve_account().
        self.account = account or ""
        self.ib = None  # the client instance (ib_async.IB), created in connect()
        # (symbol, month_slot) -> qualified contract
        self._contracts: Dict[tuple, object] = {}
        self._positions: Dict[int, dict] = {}
        # slave_ticket -> the ib_async Trade, kept only until its fill has
        # been collected. Fills are collected out of band: see
        # await_entry_fill(). Popped on collection, so a long-resting order
        # holds one entry and nothing accumulates.
        self._entry_trades: Dict[int, object] = {}
        self._exit_trades: Dict[int, object] = {}

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
        await self._resolve_account()
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
            try:
                self.ib.disconnect()
            except Exception as exc:
                # Recovery calls this to clean up a session that is already
                # dead, which is exactly when disconnect() is most likely to
                # throw. Dropping the reference is what matters -- connect()
                # builds a fresh IB() -- so never let this stop a reconnect.
                logger.debug("IBKRAdapter[%s] disconnect of a dead session: %s", self.slave_id, exc)
            self.ib = None
            # Qualified contracts are keyed to the old session's conIds. They
            # survive a reconnect in practice, but re-qualifying is cheap and
            # a stale handle fails as a rejected order.
            self._contracts.clear()

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

    async def _liquid_dated_contracts(self, symbol: str) -> List:
        """Unexpired dated contracts for a futures root, liquid months only,
        nearest first.

        Serial months are filtered out because they're near-dead: measured on
        COMEX gold, cycle months carried 100-29,000 contracts/day while the
        serials between them carried 0-23. A strategy routed to "the next
        expiry" rather than "the next liquid expiry" would sit in one of
        those.
        """
        assert self.ib is not None, "adapter not connected"
        ib_mod = self._require_ib()
        spec = get_contract_spec(symbol)
        if spec is None:
            raise AdapterRejectedError(f"{symbol} has no futures spec, so it has no contract months")

        details = await self.ib.reqContractDetailsAsync(
            ib_mod.Future(symbol, exchange=spec.exchange, currency="USD")
        )
        today = time.strftime("%Y%m%d")
        out, seen = [], set()
        for d in details:
            k = d.contract
            expiry = k.lastTradeDateOrContractMonth or ""
            if len(expiry) < 8 or expiry[:8] < today or k.conId in seen:
                continue
            if spec.liquid_months and int(expiry[4:6]) not in spec.liquid_months:
                continue
            seen.add(k.conId)
            out.append(k)
        out.sort(key=lambda k: k.lastTradeDateOrContractMonth)
        return out

    async def _contract_by_id(self, symbol: str, contract_id: int):
        """A specific contract by id -- the roll destination, and anything else
        that must not be re-derived from a slot number that may have shifted."""
        ib_mod = self._require_ib()
        qualified = await self.ib.qualifyContractsAsync(
            ib_mod.Future(conId=contract_id, exchange=get_contract_spec(symbol).exchange)
        )
        if not qualified:
            raise AdapterRejectedError(f"IBKR could not qualify contract_id={contract_id} for {symbol}")
        return qualified[0]

    async def _contract_for_request(self, symbol: str, month_slot: int, contract_id: Optional[int]):
        if contract_id is not None:
            return await self._contract_by_id(symbol, contract_id)
        return await self._qualified_contract(symbol, month_slot)

    async def _qualified_contract(self, symbol: str, month_slot: int = 0):
        """`month_slot` 0 is the front month, 1 the next liquid month, and so
        on. Strategies that would otherwise net against each other on a single
        IBKR account are separated by giving each its own slot -- same
        contract size, so they stay risk-equivalent.
        """
        assert self.ib is not None, "adapter not connected"

        cached = self._contracts.get((symbol, month_slot))
        if cached is not None and not self._is_expired(cached):
            return cached

        ib_mod = self._require_ib()
        contract = self._contract_for(symbol)
        if month_slot and not isinstance(contract, ib_mod.ContFuture):
            raise AdapterRejectedError(
                f"month_slot={month_slot} requested for {symbol}, which isn't a future "
                f"-- only futures have contract months"
            )
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

            if month_slot:
                # Step out from the front month through the liquid cycle. The
                # front month comes from ContFuture (which follows liquidity),
                # so this stays anchored to the contract IBKR itself treats as
                # current, rather than to a calendar rule of our own.
                months = await self._liquid_dated_contracts(symbol)
                ids = [k.conId for k in months]
                if resolved.conId not in ids:
                    raise AdapterRejectedError(
                        f"front month {resolved.localSymbol} for {symbol} isn't in its liquid "
                        f"cycle -- check liquid_months in contract_specs.py"
                    )
                idx = ids.index(resolved.conId) + month_slot
                if idx >= len(months):
                    raise AdapterRejectedError(
                        f"{symbol} has no liquid contract at month_slot={month_slot} "
                        f"(only {len(months) - ids.index(resolved.conId)} ahead of the front month)"
                    )
                resolved = months[idx]
                logger.info(
                    "IBKRAdapter[%s] %s slot %s -> %s (expiry %s)",
                    self.slave_id, symbol, month_slot,
                    resolved.localSymbol, resolved.lastTradeDateOrContractMonth,
                )

        self._contracts[(symbol, month_slot)] = resolved
        return resolved

    async def open(self, order: OrderRequest) -> OrderResult:
        ib_mod = self._require_ib()
        assert self.ib is not None, "adapter not connected"

        # Checked before any network work: a request that cannot become a
        # valid order should fail here, not after qualifying a contract and
        # certainly not by quietly becoming a market order -- which outside
        # regular hours is rejected anyway, and inside them fills at any
        # price at all.
        if order.order_type == "limit" and order.limit_price is None:
            raise AdapterRejectedError(
                f"limit order for {order.symbol} has no limit price"
            )
        contract = await self._contract_for_request(order.symbol, order.month_slot, order.contract_id)
        action = "BUY" if order.direction == "BUY" else "SELL"
        closing_action = "SELL" if action == "BUY" else "BUY"

        sl_price = self._round_to_tick(order.sl, order.symbol)
        tp_price = self._round_to_tick(order.tp, order.symbol)
        if sl_price != order.sl or tp_price != order.tp:
            logger.debug(
                "IBKRAdapter[%s] %s snapped to tick: sl %s -> %s, tp %s -> %s",
                self.slave_id, order.symbol, order.sl, sl_price, order.tp, tp_price,
            )

        if order.order_type == "limit":
            entry = ib_mod.LimitOrder(
                action, order.size, self._round_to_tick(order.limit_price, order.symbol)
            )
        else:
            entry = ib_mod.MarketOrder(action, order.size)
        entry.outsideRth = order.outside_rth
        entry.tif = self.entry_tif
        trade = self._place(contract, entry)
        slave_ticket = trade.order.orderId

        sl_order_id = tp_order_id = None
        if sl_price is not None or tp_price is not None:
            oca_group = f"tc-{self.slave_id}-{slave_ticket}"
            if sl_price is not None:
                sl_order = self._protective(
                    ib_mod.StopOrder(closing_action, order.size, sl_price), order.outside_rth)
                sl_order.ocaGroup = oca_group
                sl_order.ocaType = 1  # cancel the sibling order(s) once one fills
                sl_order_id = self._place(contract, sl_order).order.orderId
            if tp_price is not None:
                tp_order = self._protective(
                    ib_mod.LimitOrder(closing_action, order.size, tp_price), order.outside_rth)
                tp_order.ocaGroup = oca_group
                tp_order.ocaType = 1
                tp_order_id = self._place(contract, tp_order).order.orderId

        self._positions[slave_ticket] = {
            "contract": contract,
            "direction": order.direction,
            "size": order.size,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
        }

        # A rejected bracket leg leaves a filled position with no protection,
        # and IBKR rejects asynchronously -- placeOrder returns happily and
        # the cancel arrives moments later. Reporting success regardless is
        # how a naked position gets recorded as a protected one. Seen live:
        # both legs rejected on Error 110 while the entry filled.
        await asyncio.sleep(self.bracket_check_delay)
        for label, order_id in (("stop", sl_order_id), ("target", tp_order_id)):
            if order_id is None:
                continue
            # Named leg_trade, NOT trade: this loop used to shadow the
            # entry's own Trade, and anything added below that reached for
            # `trade` silently got the last bracket leg instead.
            leg_trade = next((t for t in self.ib.trades() if t.order.orderId == order_id), None)
            status = leg_trade.orderStatus.status if leg_trade is not None else "unknown"
            if status in ("Cancelled", "Inactive", "ApiCancelled", "unknown"):
                why = ""
                if leg_trade is not None and leg_trade.log:
                    why = leg_trade.log[-1].message or ""
                logger.error(
                    "IBKRAdapter[%s] %s leg for slave_ticket=%s was %s -- THE POSITION IS "
                    "UNPROTECTED on that side. %s",
                    self.slave_id, label, slave_ticket, status, why,
                )
        self._entry_trades[slave_ticket] = trade
        logger.info(
            "IBKRAdapter[%s] opened %s %s x%s -> ticket=%s", self.slave_id, action, order.symbol, order.size, slave_ticket
        )
        # No fill price here, deliberately. Waiting for one would hold up
        # every later signal for this slave -- the MODIFY and CLOSE of this
        # very ticket queue behind it -- and an ETF limit resting in a thin
        # evening book may not fill for minutes. The caller collects it via
        # await_entry_fill() and records it then.
        return OrderResult(
            slave_ticket=slave_ticket, sl_order_id=sl_order_id, tp_order_id=tp_order_id
        )

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
            sl_order = self._protective(
                ib_mod.StopOrder(closing_action, pos["size"], self._round_to_tick(sl, pos["contract"].symbol))
            )
            sl_order.ocaGroup = oca_group
            sl_order.ocaType = 1
            sl_order_id = self._place(pos["contract"], sl_order).order.orderId
        if tp is not None:
            tp_order = self._protective(
                ib_mod.LimitOrder(closing_action, pos["size"], self._round_to_tick(tp, pos["contract"].symbol))
            )
            tp_order.ocaGroup = oca_group
            tp_order.ocaType = 1
            tp_order_id = self._place(pos["contract"], tp_order).order.orderId

        pos["sl_order_id"] = sl_order_id
        pos["tp_order_id"] = tp_order_id

    async def close(self, slave_ticket: int) -> Optional[float]:
        ib_mod = self._require_ib()
        assert self.ib is not None, "adapter not connected"
        pos = self._positions.get(slave_ticket)
        if pos is None:
            raise AdapterRejectedError(f"no tracked IBKR position for slave_ticket={slave_ticket}")

        self._cancel_tracked_order(pos.get("sl_order_id"))
        self._cancel_tracked_order(pos.get("tp_order_id"))

        closing_action = "SELL" if pos["direction"] == "BUY" else "BUY"
        closing_order = ib_mod.MarketOrder(closing_action, pos["size"])
        closing_order.tif = self.entry_tif
        trade = self._place(pos["contract"], closing_order)
        # Dropped before awaiting the fill, deliberately: the position is
        # gone from IBKR's point of view the moment the closing order is
        # accepted, and a second CLOSE arriving during the wait must not
        # place a second closing order.
        del self._positions[slave_ticket]
        # Collected out of band too, for the same reason as the entry.
        self._exit_trades[slave_ticket] = trade
        return None

    async def _await_fill(self, trade, label: str, timeout: Optional[float] = None):
        """Wait briefly for `trade` to fill, returning (price, size).

        Returns (None, None) if it hasn't filled in time. That is not a
        failure: the order is live at the broker, and the caller has already
        recorded the position. It only means the registry stores no fill
        price for this leg -- variance capture reads it as missing rather
        than as zero.

        Deliberately keyed on the fill rather than on a terminal order
        status. IBKR's own preset handling cancels an order and re-creates it
        (Error 10349), so the status passes through "Cancelled" on a
        perfectly good order; waiting on status would either abort on that or
        have to second-guess which cancels are real. A fill is unambiguous.
        """
        timeout = self.fill_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = trade.orderStatus
            if trade.fills and status.avgFillPrice:
                return float(status.avgFillPrice), float(status.filled)
            # Yield to ib_async's reader so order/exec callbacks can land.
            await asyncio.sleep(0.05)

        logger.warning(
            "IBKRAdapter[%s] %s order %s not filled within %.1fs (status=%s)",
            self.slave_id, label, trade.order.orderId, timeout,
            trade.orderStatus.status,
        )
        return None, None

    async def await_entry_fill(self, slave_ticket: int):
        """Wait for the entry's fill, returning (price, size).

        Separate from open() so that nothing in the signal path waits on a
        broker. The caller runs this in the background and records the fill
        when it lands.

        The Trade is left in place on a timeout, so the caller can still
        reach the order to cancel it -- see abandon_unfilled_entry().
        """
        trade = self._entry_trades.get(slave_ticket)
        if trade is None:
            return None, None
        # A resting limit is supposed to take a while; a market order that
        # takes this long has something wrong with it. Different questions,
        # so different clocks.
        is_limit = getattr(trade.order, "orderType", "") == "LMT"
        timeout = self.entry_timeout if is_limit else self.fill_timeout
        price, size = await self._await_fill(trade, "entry", timeout)
        if price is not None:
            self._entry_trades.pop(slave_ticket, None)
        return price, size

    async def abandon_unfilled_entry(self, slave_ticket: int):
        """Cancel an entry that never filled, and its protective legs.

        Returns (price, size): a price means the order filled after all, in
        the gap between the timeout and this call, and the position stands.

        The legs matter as much as the entry. They are placed alongside it
        and carry GTC, so leaving them would strand a live stop and target
        with no position behind them -- either able to execute later and open
        a position nobody asked for.
        """
        trade = self._entry_trades.pop(slave_ticket, None)
        pos = self._positions.get(slave_ticket)

        # Last look before cancelling. The fill may have landed while this
        # was being scheduled, and cancelling then would leave a real
        # position with no stop.
        if trade is not None:
            status = trade.orderStatus
            if trade.fills and status.avgFillPrice:
                logger.info(
                    "IBKRAdapter[%s] entry %s filled just after its timeout; "
                    "keeping the position", self.slave_id, slave_ticket,
                )
                return float(status.avgFillPrice), float(status.filled)
            self._cancel_tracked_order(trade.order.orderId)

        if pos is not None:
            self._cancel_tracked_order(pos.get("sl_order_id"))
            self._cancel_tracked_order(pos.get("tp_order_id"))
            del self._positions[slave_ticket]

        logger.warning(
            "IBKRAdapter[%s] cancelled unfilled entry %s and its protective legs",
            self.slave_id, slave_ticket,
        )
        return None, None

    async def await_exit_fill(self, slave_ticket: int):
        """Wait for the closing order's fill, returning (price, size)."""
        trade = self._exit_trades.pop(slave_ticket, None)
        if trade is None:
            return None, None
        return await self._await_fill(trade, "close")

    async def get_status(self) -> bool:
        return self.ib is not None and self.ib.isConnected()

    async def get_account_summary(self, account_type: str) -> AccountSummary:
        assert self.ib is not None, "adapter not connected"
        # accountSummaryAsync(), NOT accountSummary(): the sync accessor falls
        # back to self._run() when its cache is cold, which raises
        # "RuntimeError: This event loop is already running" inside the hub's
        # loop. Same trap as IB.sleep(). Verified against a live Gateway.
        summary = {item.tag: item.value for item in await self.ib.accountSummaryAsync(self.account)}
        try:
            balance = float(summary["TotalCashValue"])
            equity = float(summary["NetLiquidation"])
        except KeyError as exc:
            raise AdapterRejectedError(f"IBKR account summary missing expected field: {exc}") from exc
        return AccountSummary(balance=balance, equity=equity)

    async def get_price_history(
        self, symbol: str, period: int, month_slot: int = 0,
        contract_id: Optional[int] = None, bar_size: Optional[str] = None
    ) -> List[Bar]:
        assert self.ib is not None, "adapter not connected"
        contract = await self._contract_for_request(symbol, month_slot, contract_id)
        size = bar_size or "1 day"
        bars = await self.ib.reqHistoricalDataAsync(
            contract,
            endDateTime="",
            durationStr=_duration_for(period, size),
            barSizeSetting=size,
            whatToShow="TRADES",
            useRTH=True,
        )
        return [Bar(high=b.high, low=b.low, close=b.close) for b in bars]

    async def get_quote(
        self, symbol: str, month_slot: int = 0, contract_id: Optional[int] = None
    ) -> float:
        assert self.ib is not None, "adapter not connected"
        contract = await self._contract_for_request(symbol, month_slot, contract_id)
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
        # NaN *or* a non-positive sentinel. IBKR returns -1 (not NaN) when
        # there's no market -- seen live on GLD outside the US cash session
        # while GC, which trades nearly 24h, quoted fine. A -1 slipping
        # through is worse than an error: sizing divides by it and the
        # ATR-derived SL/TP get computed off a negative price.
        if price != price or price <= 0:
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
                f"IBKR returned no usable market price for symbol={symbol} "
                f"(got {price!r}, market_data_type={self.market_data_type}). "
                f"A negative price means IBKR has no market for it right now -- "
                f"usually the instrument's session is closed. Otherwise: {hint}"
            )
        return float(price)

    async def _resolve_account(self) -> None:
        """Pin this adapter to exactly one IBKR account.

        One Gateway login can hold several accounts, and leaving that
        implicit breaks three things of which only the first is loud:

        - IBKR rejects an order that does not name an account when the login
          manages more than one.
        - accountSummary() returns every account's rows blended together.
          get_account_summary() keys them by tag, so with two accounts the
          last row silently wins -- and that equity drives %-risk sizing and
          the drawdown watchdog. Sizing off the wrong account's equity is
          not a failure anyone sees happen.
        - positions() likewise spans all accounts, so reconciliation would
          read another account's positions as untracked ones here.

        A single-account login gets pinned anyway, so the reads are explicit
        rather than relying on IBKR's default.
        """
        assert self.ib is not None
        available = list(self.ib.managedAccounts())

        if not available:
            # The handshake normally delivers these before connectAsync
            # returns. If it hasn't, carry on unpinned rather than refuse to
            # start -- a single-account login behaves correctly either way.
            logger.warning(
                "IBKRAdapter[%s] Gateway reported no managed accounts; continuing unpinned",
                self.slave_id,
            )
            return

        if self.account:
            if self.account not in available:
                raise AdapterConnectionError(
                    f"slave {self.slave_id} is configured for IBKR account {self.account!r}, "
                    f"but this login manages {available}"
                )
        elif len(available) == 1:
            self.account = available[0]
        else:
            raise AdapterConnectionError(
                f"IBKR login at {self.host}:{self.port} manages {len(available)} accounts "
                f"({available}). Set \"account\" in this slave's config to say which one it "
                f"trades -- unset, orders are rejected and account equity and positions come "
                f"back blended across all of them."
            )

        logger.info("IBKRAdapter[%s] trading account %s", self.slave_id, self.account)

    def _place(self, contract, order):
        """Place an order, always naming the account it belongs to.

        Every placement goes through here precisely so a new call site
        cannot quietly omit the account and work fine on a single-account
        login while being rejected on a multi-account one.
        """
        if self.account:
            order.account = self.account
        assert self.ib is not None
        return self.ib.placeOrder(contract, order)

    def _protective(self, order, outside_rth: bool = False):
        """Stamp a protective leg's time-in-force and session.

        Deliberately not applied to the entry: a market order that can't fill
        is better off expiring than resting indefinitely as GTC.

        outsideRth matches the entry. A stop that only arms during regular
        hours on a position opened in the evening session leaves it unguarded
        for the part of the session it was deliberately opened in.
        """
        order.tif = self.bracket_tif
        order.outsideRth = outside_rth
        return order

    @staticmethod
    def _round_to_tick(price: Optional[float], symbol: str) -> Optional[float]:
        """Snap a price to the contract's minimum price variation.

        IBKR rejects anything else outright with "Error 110: The price does
        not conform to the minimum price variation for this contract". Our
        SL/TP come out of ATR arithmetic, so they're arbitrary floats --
        3954.8454525079565 on a contract that moves in 0.10 steps. Seen live:
        both bracket legs rejected, leaving a filled position with no stop.
        """
        if price is None:
            return None
        spec = get_contract_spec(symbol)
        tick = spec.tick_size if spec else 0.01
        if not tick or tick <= 0:
            return price
        # round the multiple, not the price: tick sizes like 0.10 aren't
        # exact in binary floating point
        return round(round(price / tick) * tick, 10)

    @staticmethod
    def _info(symbol: str, contract) -> ContractInfo:
        return ContractInfo(
            symbol=symbol,
            contract_id=contract.conId,
            local_symbol=contract.localSymbol,
            expiry=contract.lastTradeDateOrContractMonth,
        )

    async def restore_position(self, slave_ticket: int, state: dict) -> None:
        contract_id = state.get("contract_id")
        symbol = state.get("slave_symbol") or state.get("symbol")
        if contract_id is None or not symbol:
            logger.warning(
                "IBKRAdapter[%s] cannot restore slave_ticket=%s: no contract recorded",
                self.slave_id, slave_ticket,
            )
            return
        contract = await self._contract_by_id(symbol, int(contract_id))
        self._positions[slave_ticket] = {
            "contract": contract,
            "direction": state["direction"],
            "size": state["size"],
            "sl_order_id": state.get("sl_order_id"),
            "tp_order_id": state.get("tp_order_id"),
        }
        logger.info(
            "IBKRAdapter[%s] restored slave_ticket=%s on %s (%s %s)",
            self.slave_id, slave_ticket, contract.localSymbol, state["direction"], state["size"],
        )

    async def resolve_contract(self, symbol: str, month_slot: int = 0) -> ContractInfo:
        return self._info(symbol, await self._qualified_contract(symbol, month_slot))

    async def next_contract_after(self, symbol: str, contract_id: int) -> Optional[ContractInfo]:
        months = await self._liquid_dated_contracts(symbol)
        ids = [k.conId for k in months]
        if contract_id not in ids:
            # Already past its last trading day, so it's gone from the live
            # list. Fall back to the nearest liquid month still available.
            return self._info(symbol, months[0]) if months else None
        idx = ids.index(contract_id) + 1
        return self._info(symbol, months[idx]) if idx < len(months) else None

    async def get_contract_volume(
        self, symbol: str, month_slot: int = 0, contract_id: Optional[int] = None
    ) -> Optional[float]:
        assert self.ib is not None, "adapter not connected"
        contract = await self._contract_for_request(symbol, month_slot, contract_id)
        ticker = self.ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(self.quote_timeout)
        volume = ticker.volume
        self.ib.cancelMktData(contract)
        if volume != volume or volume is None or volume < 0:   # NaN / absent
            return None
        return float(volume)

    async def get_open_positions(self, account_type: str) -> List[Position]:
        assert self.ib is not None, "adapter not connected"
        results = []
        for pos in self.ib.positions(self.account):
            if pos.position == 0:
                continue
            results.append(
                Position(symbol=pos.contract.symbol, quantity=pos.position, average_price=pos.avgCost)
            )
        return results
