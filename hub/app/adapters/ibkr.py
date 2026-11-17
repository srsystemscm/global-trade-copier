import asyncio
import logging
import time
from typing import Dict, List, Optional

from app.adapters.base import (
    EntryNeverFilled,
    MarginPreview,
    AccountSummary,
    ContractInfo,
    # Was MISSING while open() already raised it for an unplaceable time in
    # force, so that refusal came out as NameError -- which the engine does
    # not recognise as a broker refusal and does not report as one. Nothing
    # caught it because the only test of that path inspected ENTRY_TIFS
    # rather than executing the raise.
    AdapterError,
    AdapterConnectionError,
    AdapterRejectedError,
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    Position,
    WorkingOrder,
)
from app.atr import Bar
from app.contract_specs import get_contract_spec
from app.order_split import resolve_split, split_sizes
from app.order_timing import OrderTimingError, resolve_good_till, seconds_until

logger = logging.getLogger(__name__)

# IBKR truncates an over-long orderRef silently, which would make a join on
# it match nothing with no error anywhere. The documented field is short,
# so the reference is kept well inside it and trimmed here rather than at
# the broker -- a trimmed-but-consistent string still joins, because what
# is stored is what was sent.
ORDER_REF_MAX = 40


def _stamp(order, ref) -> None:
    """Put the copier's reference on a broker order, if there is one."""
    if ref:
        order.orderRef = str(ref)[:ORDER_REF_MAX]

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

    # What IBKR will take on an entry. Checked here rather than left to the
    # broker because a bad value comes back as a rejected order in a log
    # nobody is reading at 18:00 on a Sunday -- and the whole point of the
    # setting is that the order rests until Monday.
    #
    # Deliberately not the full IBKR list: OPG, FOK and DTC have no meaning
    # for an entry this engine places, and accepting them would let a route
    # be written that silently never fills.
    ENTRY_TIFS = ("DAY", "GTC", "GTD", "IOC")


    def __init__(
        self,
        slave_id: str,
        host: str,
        port: int,
        client_id: int,
        market_data_type: Optional[int] = None,
        quote_timeout: float = 1.0,
        quote_retries: int = 2,
        bracket_tif: str = "GTC",
        entry_tif: str = "DAY",
        fill_timeout: float = 10.0,
        exit_chase_steps: int = 4,
        exit_chase_pct: float = 0.002,
        entry_chase_steps: int = 0,
        entry_chase_interval: float = 300.0,
        entry_chase_max_pct: float = 0.01,
        entry_split_slices: int = 0,
        entry_split_interval: float = 0.0,
        entry_timeout: float = 900.0,
        good_till: Optional[object] = None,
        quote_fallback: str = "strict",
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
        # Extra attempts when a quote arrives empty. The failure this
        # answers is contention rather than latency: two engines on one
        # account asking at the same moment, where IBKR silently delivers
        # nothing to the loser. Asking again a beat later gets it.
        self.quote_retries = quote_retries
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
        # How far an unfilled limit exit may be walked, and in what size of
        # step. Bounded on purpose: a wrong quote should cost a known
        # number of ticks, not an open-ended slide. The step defaults to
        # the route's own limit_offset_pct where it has one.
        self.exit_chase_steps = exit_chase_steps
        self.exit_chase_pct = exit_chase_pct
        # How a RESTING ENTRY follows the market. Off by default: every
        # slave behaves exactly as it does today until this is set.
        #
        # The entry's problem is a stale anchor, not an over-aggressive
        # price, so each step re-prices from a FRESH QUOTE rather than
        # from its own last limit -- see await_entry_fill.
        self.entry_chase_steps = entry_chase_steps
        self.entry_chase_interval = entry_chase_interval
        # How far the limit may drift from where it started, as a
        # fraction. The stop is fixed in absolute terms, so filling worse
        # than sizing assumed means risking more than was sized for --
        # this is what bounds that, and it is why the entry chase has a
        # ceiling where the exit chase has only a step count.
        self.entry_chase_max_pct = entry_chase_max_pct
        # How an entry too large for one print is placed: N orders,
        # entry_split_interval seconds apart, same instrument and
        # direction, collapsing into ONE tracked position. Off by default,
        # and a route may set its own.
        #
        # One position rather than N is the whole point. Chris: "one
        # instrument easier to track and roll". N positions would mean N
        # registry rows, N pairs of protective legs, N things the roll has
        # to find, and a MODIFY arriving against whichever one the lookup
        # happened to return.
        self.entry_split_slices = entry_split_slices
        self.entry_split_interval = entry_split_interval
        # How long a LIMIT entry may rest before it is cancelled outright.
        # Not the same question as fill_timeout: a market order that hasn't
        # filled in ten seconds is a problem, while a limit resting in a thin
        # evening book for ten minutes is the plan. What must not happen is
        # resting forever -- see abandon_unfilled_entry().
        self.entry_timeout = entry_timeout
        # The slave-wide GTD deadline, for routes that ask for GTD without
        # naming one. No default: GTD with nothing to expire at is refused
        # rather than quietly becoming GTC, which is what it did before.
        self.entry_good_till = good_till
        # What to do when IBKR quotes -1 -- "no market on this line".
        # "strict" refuses, which is right for a market order. "last_close"
        # prices from the last trade or the session close, which is right
        # for a resting limit in a session IBKR will not quote but will
        # trade: see _sample_quote.
        self.quote_fallback = (quote_fallback or "strict").lower()
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
        # slave_ticket -> EVERY slice's Trade, the first one included.
        #
        # _entry_trades above keeps the FIRST slice, because its orderId is
        # the slave_ticket and because the entry chase and the roll each
        # want one Trade. This holds all of them, and it is what the three
        # places that have to reason about "the entry" read: the fill is the
        # sum across slices, the cancel has to reach every one, and a close
        # has to settle every one. An unsplit entry is a list of one, so
        # there is one code path rather than two.
        self._entry_slices: Dict[int, List[object]] = {}
        # slave_ticket -> the task placing the slices still to come.
        # Cancelled when the position goes, so a CLOSE arriving mid-window
        # cannot be followed by another slice landing against nothing.
        self._split_tasks: Dict[int, asyncio.Task] = {}
        # slave_ticket -> how long the schedule runs for, so await_entry_fill
        # waits out the window instead of abandoning the entry halfway
        # through placing it.
        self._split_windows: Dict[int, float] = {}
        # slave_ticket -> how long this particular entry may rest
        self._entry_waits: Dict[int, float] = {}
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

    def _require_connected(self):
        """The live IB client, or a refusal the engine already understands.

        Every one of these guards used to be `assert self.ib is not None`.
        disconnect() sets self.ib = None (and a dropped session calls it),
        so any call already in flight on the consume path raised
        AssertionError -- which is not an AdapterError. It escaped
        AutonomousCopyEngine._run, whose only handler was
        `except AdapterError`, and ended that slave's consume task for the
        life of the process with nothing logged anywhere. The engine stayed
        a live object with a dead loop: /slaves still said connected, the
        adapter reconnected, and every later signal for that slave was
        dropped. See hub/tests/test_the_consume_loop_survives.py.

        AdapterConnectionError is the type the engine knows how to refuse,
        report and retry. The same went for the bare asserts: `python -O`
        strips assertions entirely, which would have turned the identical
        disconnect into AttributeError on None -- equally not an
        AdapterError, equally fatal to the loop.
        """
        ib = self.ib
        if ib is None:
            raise AdapterConnectionError(
                f"IBKRAdapter[{self.slave_id}] is not connected "
                "(the session dropped, or disconnect() ran while this call was in flight)"
            )
        return ib

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
            # Any slice schedule still running belongs to the session that
            # has just gone. Left alive it would wake up and place an order
            # through a client that no longer exists -- and recovery
            # reconciles against the broker, so a slice arriving after that
            # is a position nothing has a row for.
            for ticket in list(self._split_tasks):
                self._stop_splitting(ticket)
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
            # ib_symbol/trading_class, not `symbol`: the name used in a
            # route is not always the name IBKR lists. Silver is listed
            # under "SI" for both sizes, so without the trading class this
            # resolves to whichever one IBKR returns first -- 1,000 or
            # 5,000 ounces, a five-fold sizing error that fills happily.
            return ib_mod.ContFuture(
                spec.ib_symbol or symbol, exchange=spec.exchange,
                currency="USD",
                **({"tradingClass": spec.trading_class}
                   if spec.trading_class else {}))
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
        self._require_connected()
        ib_mod = self._require_ib()
        spec = get_contract_spec(symbol)
        if spec is None:
            raise AdapterRejectedError(f"{symbol} has no futures spec, so it has no contract months")

        details = await self.ib.reqContractDetailsAsync(
            ib_mod.Future(
                spec.ib_symbol or symbol, exchange=spec.exchange,
                currency="USD",
                **({"tradingClass": spec.trading_class}
                   if spec.trading_class else {}))
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
            # Belt and braces behind the request filter: one root carrying
            # two contract sizes must not mix them into one month cycle, or
            # a month_slot walk would step from a micro into a full-size.
            if spec.trading_class and k.tradingClass != spec.trading_class:
                continue
            seen.add(k.conId)
            out.append(k)
        out.sort(key=lambda k: k.lastTradeDateOrContractMonth)
        return out

    async def _contract_by_id(self, symbol: str, contract_id: int):
        """A specific contract by id -- the roll destination, and anything else
        that must not be re-derived from a slot number that may have shifted."""
        ib_mod = self._require_ib()
        qualified = self._qualified_only(await self.ib.qualifyContractsAsync(
            ib_mod.Future(conId=contract_id, exchange=get_contract_spec(symbol).exchange)
        ))
        if not qualified:
            raise AdapterRejectedError(f"IBKR could not qualify contract_id={contract_id} for {symbol}")
        return qualified[0]

    async def _contract_for_request(self, symbol: str, month_slot: int, contract_id: Optional[int]):
        if contract_id is not None:
            return await self._contract_by_id(symbol, contract_id)
        return await self._qualified_contract(symbol, month_slot)

    async def _dated_contract(self, symbol: str, month_slot: int = 0):
        """Resolve a slot straight from the dated contract list.

        For roots where one IBKR symbol carries several contract sizes, this
        is the only way to get the right one: ContFuture ignores
        tradingClass. Probed live -- ContFuture("SI", tradingClass="SIL")
        returns the 5,000 ounce contract, which resolves, qualifies and
        quotes perfectly while being five times the intended size.

        reqContractDetails does honour it, and liquid_months filters out the
        near-dead serial months, which is the part ContFuture was doing that
        a plain expiry sort does not.
        """
        months = await self._liquid_dated_contracts(symbol)
        if not months:
            raise AdapterRejectedError(
                f"IBKR listed no liquid contract for {symbol} -- check "
                f"liquid_months and trading_class in contract_specs.py"
            )
        if month_slot >= len(months):
            raise AdapterRejectedError(
                f"{symbol} has no liquid contract at month_slot={month_slot} "
                f"(only {len(months)} listed ahead)"
            )
        resolved = months[month_slot]
        logger.info(
            "IBKRAdapter[%s] %s slot %s -> %s (expiry %s, conId %s, "
            "tradingClass %s)",
            self.slave_id, symbol, month_slot, resolved.localSymbol,
            resolved.lastTradeDateOrContractMonth, resolved.conId,
            resolved.tradingClass,
        )
        return resolved

    async def _qualified_contract(self, symbol: str, month_slot: int = 0):
        """`month_slot` 0 is the front month, 1 the next liquid month, and so
        on. Strategies that would otherwise net against each other on a single
        IBKR account are separated by giving each its own slot -- same
        contract size, so they stay risk-equivalent.
        """
        self._require_connected()

        cached = self._contracts.get((symbol, month_slot))
        if cached is not None and not self._is_expired(cached):
            return cached

        spec = get_contract_spec(symbol)
        if spec is not None and spec.trading_class:
            resolved = await self._dated_contract(symbol, month_slot)
            self._contracts[(symbol, month_slot)] = resolved
            return resolved

        ib_mod = self._require_ib()
        contract = self._contract_for(symbol)
        if month_slot and not isinstance(contract, ib_mod.ContFuture):
            raise AdapterRejectedError(
                f"month_slot={month_slot} requested for {symbol}, which isn't a future "
                f"-- only futures have contract months"
            )
        qualified = self._qualified_only(
            await self.ib.qualifyContractsAsync(contract))
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
            dated = self._qualified_only(await self.ib.qualifyContractsAsync(
                ib_mod.Future(conId=resolved.conId, exchange=resolved.exchange)
            ))
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
        self._require_connected()

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

        # How this entry goes out: one order, or several over a window.
        # Everything below is written in terms of `first` rather than
        # order.size, because what is at the broker right now is the first
        # slice -- and a stop sized to the intended total while only the
        # first slice is working is a short position waiting for a touch.
        sizes = self._slices_for(order)
        first = sizes[0]

        if order.order_type == "limit":
            entry = ib_mod.LimitOrder(
                action, first, self._round_to_tick(order.limit_price, order.symbol)
            )
        else:
            entry = ib_mod.MarketOrder(action, first)
        entry.outsideRth = order.outside_rth
        # Time in force, and the GTD deadline that makes GTD mean anything.
        self._stamp_entry_timing(entry, order)
        # The copier's own reference, which the broker hands back on its
        # statement. Set on the entry AND on both protective legs below, so
        # every execution belonging to this position is recognisable as
        # ours -- including a stop that fires months later, when nothing in
        # this hub placed a closing order at all.
        _stamp(entry, order.order_ref)
        trade = self._place(contract, entry)
        slave_ticket = trade.order.orderId
        # permId is IBKR's own permanent id for the order, and the only one
        # that appears in an Activity Flex statement. orderId above is TWS's
        # client-side sequence number and is not in the statement at all, so
        # without this there is nothing to reconcile the two on.
        #
        # It is assigned when the order is acknowledged, which can be a
        # moment after placing it -- so 0/None here is normal and means "not
        # yet known", not "no id".
        broker_order_id = getattr(trade.order, "permId", None) or None

        sl_order_id = tp_order_id = None
        if not order.broker_brackets and (sl_price is not None or tp_price is not None):
            logger.info(
                "IBKRAdapter[%s] %s: not resting broker stop/target -- this "
                "route is exit-driven by the master, because those orders do "
                "not execute in extended hours",
                self.slave_id, order.symbol,
            )
        if order.broker_brackets and (sl_price is not None or tp_price is not None):
            oca_group = f"tc-{self.slave_id}-{slave_ticket}"
            if sl_price is not None:
                sl_order = self._protective(
                    ib_mod.StopOrder(closing_action, first, sl_price), order.outside_rth)
                sl_order.ocaGroup = oca_group
                sl_order.ocaType = 1  # cancel the sibling order(s) once one fills
                _stamp(sl_order, order.order_ref)
                sl_order_id = self._place(contract, sl_order).order.orderId
            if tp_price is not None:
                tp_order = self._protective(
                    ib_mod.LimitOrder(closing_action, first, tp_price), order.outside_rth)
                tp_order.ocaGroup = oca_group
                tp_order.ocaType = 1
                _stamp(tp_order, order.order_ref)
                tp_order_id = self._place(contract, tp_order).order.orderId

        self._positions[slave_ticket] = {
            "contract": contract,
            "direction": order.direction,
            # What the broker holds or is working for this position RIGHT
            # NOW, which for a split entry grows as the slices go out. The
            # exit is sized from this and so are the protective legs, so it
            # has to be the live figure rather than the intention.
            "size": first,
            # What it is meant to reach once every slice is placed. Kept
            # only so the logs and the dashboard can say "9 of 35" instead
            # of looking like a sizing error.
            "intended_size": order.size,
            # The LEVELS, not just the order ids. A split entry has to
            # re-place its legs at a larger size as it grows, and it cannot
            # ask the orders it just cancelled what price they were at.
            "sl": sl_price,
            "tp": tp_price,
            "sl_order_id": sl_order_id,
            "tp_order_id": tp_order_id,
            # Kept so the EXIT can mirror the entry. A position opened with a
            # limit in the evening session has to be closed the same way: a
            # market order is refused outside regular hours, and an exit
            # without outsideRth waits for the 09:30 open.
            "order_type": order.order_type,
            "outside_rth": order.outside_rth,
            "limit_offset_pct": order.limit_offset_pct,
            "symbol": order.symbol,
            "month_slot": order.month_slot,
            "contract_id": order.contract_id,
        }

        # A rejected bracket leg leaves a filled position with no protection,
        # and IBKR rejects asynchronously -- placeOrder returns happily and
        # the cancel arrives moments later. Reporting success regardless is
        # how a naked position gets recorded as a protected one. Seen live:
        # both legs rejected on Error 110 while the entry filled.
        await self._check_legs_took(slave_ticket, sl_order_id, tp_order_id)
        # Stashed for the entry chase, which later has to recompute
        # "through the quote" against a FRESH quote and no longer has the
        # route that chose the offset.
        try:
            trade.order._gtc_offset = float(order.limit_offset_pct or 0.0)
        except Exception:  # pragma: no cover - ib_async orders accept it
            pass
        self._entry_trades[slave_ticket] = trade
        # One code path for split and unsplit: an unsplit entry is a list
        # of one, and the three places that reason about "the entry" --
        # the fill, the cancel, the close -- all read this.
        self._entry_slices[slave_ticket] = [trade]
        # How long THIS entry may rest, remembered beside the trade because
        # _collect_fill is handed a ticket and no longer has the order. A
        # GTC entry with the adapter's 900s clock is cancelled by us a
        # quarter of an hour in, so the TIF alone would have changed
        # nothing -- the pair has to travel together.
        if order.entry_timeout is not None:
            # Resolved HERE, at placement, not float()-ed. A timeout written
            # as "rth_open" is a different number of seconds every time it
            # is asked, which is the whole reason it is written that way --
            # 93600 is right for a Sunday 18:00 placement and wrong for any
            # other. A plain number still passes through unchanged.
            try:
                self._entry_waits[slave_ticket] = seconds_until(order.entry_timeout)
            except OrderTimingError as exc:
                # Not fatal: the order IS placed. Falling back to the
                # adapter's own clock cancels it eventually, where raising
                # here would leave a live order with nothing watching it.
                logger.error(
                    "IBKRAdapter[%s] %s entry_timeout %r is unusable, falling "
                    "back to %ss: %s",
                    self.slave_id, order.symbol, order.entry_timeout,
                    self.entry_timeout, exc,
                )
        if len(sizes) > 1:
            interval = resolve_split(
                order.split_slices, order.split_interval,
                self.entry_split_slices, self.entry_split_interval,
            )[1]
            logger.info(
                "IBKRAdapter[%s] opened %s %s x%s -> ticket=%s -- slice 1/%d of "
                "an intended %s, the rest %gs apart (%s)",
                self.slave_id, action, order.symbol, first, slave_ticket,
                len(sizes), order.size, interval,
                ", ".join(str(x) for x in sizes),
            )
            self._schedule_slices(slave_ticket, contract, order, sizes, interval)
        else:
            logger.info(
                "IBKRAdapter[%s] opened %s %s x%s -> ticket=%s", self.slave_id, action, order.symbol, order.size, slave_ticket
            )
        # No fill price here, deliberately. Waiting for one would hold up
        # every later signal for this slave -- the MODIFY and CLOSE of this
        # very ticket queue behind it -- and an ETF limit resting in a thin
        # evening book may not fill for minutes. The caller collects it via
        # await_entry_fill() and records it then.
        return OrderResult(
            slave_ticket=slave_ticket, sl_order_id=sl_order_id,
            tp_order_id=tp_order_id,
            # What actually went on, which for a split entry is the FIRST
            # SLICE. Left as the requested size, the engine logged
            # "size=16.0" for a position with 6 at the broker -- a line a
            # reader would check the size against. The rest arrive from
            # the schedule and are recorded by await_entry_fill.
            lots=first,
            broker_order_id=None if broker_order_id is None else str(broker_order_id),
            broker_order_ref=order.order_ref,
        )

    async def _check_legs_took(self, slave_ticket: int,
                               sl_order_id: Optional[int],
                               tp_order_id: Optional[int]) -> None:
        """Did the protective legs survive being placed?

        A rejected bracket leg leaves a filled position with no protection,
        and IBKR rejects ASYNCHRONOUSLY -- placeOrder returns happily and
        the cancel arrives moments later. Reporting success regardless is
        how a naked position gets recorded as a protected one. Seen live:
        both legs rejected on Error 110 while the entry filled.

        Called after the opening brackets AND after the splitter regrows
        them. A regrow is a cancel and a re-place, so it can fail in
        exactly this way, on a position that was protected a moment
        earlier -- and it happens with no signal in flight, so nothing
        else would be looking.
        """
        await asyncio.sleep(self.bracket_check_delay)
        for label, order_id in (("stop", sl_order_id), ("target", tp_order_id)):
            if order_id is None:
                continue
            # Named leg_trade, NOT trade: this loop used to shadow the
            # entry's own Trade in open(), and anything that reached for
            # `trade` afterwards silently got the last bracket leg instead.
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

    # ------------------------------------------------- the order splitter

    # Read by the engine, which otherwise cannot tell a route asking for a
    # split that will happen from one asking for a split that is silently
    # dropped. MT4/MT5/Schwab carry the fields and ignore them.
    supports_order_split = True

    def _slices_for(self, order: OrderRequest) -> List[float]:
        """The sizes this entry goes out in. One of them means no split."""
        slices, interval = resolve_split(
            order.split_slices, order.split_interval,
            self.entry_split_slices, self.entry_split_interval,
        )
        if slices <= 1:
            return [order.size]
        sizes = split_sizes(order.size, slices)
        if len(sizes) > 1 and self.entry_chase_steps > 0:
            # Both re-price a resting entry and neither knows about the
            # other: chasing slice one while slice four is still to be
            # placed walks the limit away from the price the rest were
            # planned at. Said out loud, because a setting that is live in
            # the config and inert in the code is the worst kind.
            logger.warning(
                "IBKRAdapter[%s] %s: splitting into %d slices, so the entry "
                "chase does NOT run for this order -- the two cannot both "
                "move the same entry",
                self.slave_id, order.symbol, len(sizes),
            )
        return sizes

    @staticmethod
    def _filled_across(slices):
        """What every slice has filled between them, as (avg price, size).

        Size-weighted, because the slices fill at different prices and the
        position has one entry price. (None, 0.0) when nothing has filled.
        """
        total = 0.0
        notional = 0.0
        for trade in slices:
            if trade is None:
                continue
            status = trade.orderStatus
            filled = float(status.filled or 0.0)
            price = float(status.avgFillPrice or 0.0)
            if filled > 0 and price:
                total += filled
                notional += filled * price
        if total <= 0:
            return None, 0.0
        return notional / total, total

    @staticmethod
    def _all_slices_done(slices) -> bool:
        """True when no slice has anything left working at the broker."""
        for trade in slices:
            if trade is None:
                continue
            if float(trade.orderStatus.remaining or 0.0) > 0:
                return False
        return True

    async def _settle_all(self, slices, label: str):
        """Cancel every slice and report what exists afterwards.

        One _settle_cancel per slice, because the fill that lands inside
        the cancel round trip can land on any of them -- and the whole
        reason _settle_cancel exists is that believing the cancel request
        over the fills is how untracked volume accumulates.
        """
        live = [t for t in slices if t is not None]
        for trade in live:
            self._cancel_tracked_order(trade.order.orderId)
        # CONCURRENTLY, not one after another. Each settle waits up to five
        # seconds for its own order to go terminal, and these waits have
        # nothing to do with each other -- run in sequence, abandoning a
        # four-slice entry would hold the consume loop for twenty seconds,
        # with every later signal for this slave queued behind it.
        settled = await asyncio.gather(
            *(self._settle_cancel(t, label) for t in live),
            return_exceptions=True,
        )
        total = 0.0
        notional = 0.0
        for trade, outcome in zip(live, settled):
            if isinstance(outcome, BaseException):
                # One slice that would not settle must not hide the others.
                # Believing the fills over the cancel request is the whole
                # point of _settle_cancel, and an unanswered cancel is the
                # case where that matters most.
                logger.exception(
                    'IBKRAdapter[%s] could not settle the cancel of %s order %s',
                    self.slave_id, label, trade.order.orderId,
                    exc_info=outcome,
                )
                continue
            price, size = outcome
            if price is not None and size:
                total += float(size)
                notional += float(size) * float(price)
        if total <= 0:
            return None, None
        return notional / total, total

    def _stop_splitting(self, slave_ticket: int) -> int:
        """Call off the slices still to come. Returns 1 if any were.

        Called from every path that ends the position -- close, abandon,
        disconnect -- because a slice placed after the position is gone is
        a naked order for an instrument nothing is tracking.
        """
        task = self._split_tasks.pop(slave_ticket, None)
        self._split_windows.pop(slave_ticket, None)
        if task is None or task.done():
            return 0
        task.cancel()
        return 1

    def _split_pending(self, slave_ticket: int) -> bool:
        task = self._split_tasks.get(slave_ticket)
        return task is not None and not task.done()

    def _schedule_slices(self, slave_ticket: int, contract, order: OrderRequest,
                         sizes: List[float], interval: float) -> None:
        """Place the slices after the first, one interval apart.

        IN THE BACKGROUND, deliberately. open() is called from the consume
        loop, and holding it for the length of the window would queue every
        later signal for this slave behind it -- the MODIFY and the CLOSE of
        this very ticket included. The same reason fills are collected out
        of band.

        A HUB RESTART ENDS THE SCHEDULE. The position is restored at the
        size the registry recorded, and the slices that had not gone out
        do not go out later -- which is the right way round: a slice
        arriving from a schedule the restart forgot would join a position
        whose size nothing had re-measured. A restart mid-window therefore
        leaves the position smaller than intended, and the log line from
        each slice is what says how much went out.
        """
        remaining = list(sizes[1:])
        placed = [sizes[0]]
        self._split_windows[slave_ticket] = interval * len(remaining)

        async def run() -> None:
            try:
                for n, size in enumerate(remaining, start=2):
                    await asyncio.sleep(interval)
                    if slave_ticket not in self._positions:
                        # Closed, abandoned or stopped out while the
                        # schedule ran. The slices still to come are not
                        # wanted: there is nothing left for them to join.
                        logger.info(
                            "IBKRAdapter[%s] %s slave_ticket=%s is gone; "
                            "dropping the remaining %d slice(s)",
                            self.slave_id, order.symbol, slave_ticket,
                            len(remaining) - n + 2,
                        )
                        return
                    await self._place_slice(slave_ticket, contract, order, size,
                                            n, len(sizes), placed)
            except asyncio.CancelledError:
                raise
            except Exception:
                # The position exists and is tracked. Dying quietly here
                # would leave it short of its intended size with nothing
                # said -- the same silence the consume loop was fixed for.
                logger.exception(
                    "IBKRAdapter[%s] %s slave_ticket=%s: the slice schedule "
                    "failed; the position holds %s of an intended %s and no "
                    "further slices will be placed",
                    self.slave_id, order.symbol, slave_ticket,
                    sum(placed), order.size,
                )
            finally:
                self._split_windows.pop(slave_ticket, None)

        self._split_tasks[slave_ticket] = asyncio.create_task(
            run(), name=f"split-{self.slave_id}-{slave_ticket}")

    async def _place_slice(self, slave_ticket: int, contract, order: OrderRequest,
                           size: float, n: int, of: int, placed) -> None:
        """One later slice, joined onto the position the first one opened."""
        ib_mod = self._require_ib()
        self._require_connected()
        if order.order_type == "limit":
            # The SAME limit the first slice went out at, not a fresh one
            # priced through a newer quote. The stop was fixed when the
            # position was sized, so a slice filled higher than planned
            # risks more than was sized for -- and the point of the window
            # is to spread the fills in TIME, not to re-decide the price.
            entry = ib_mod.LimitOrder(
                order.direction, size,
                self._round_to_tick(order.limit_price, order.symbol))
        else:
            entry = ib_mod.MarketOrder(order.direction, size)
        entry.outsideRth = order.outside_rth
        self._stamp_entry_timing(entry, order)
        # The same order_ref as the first slice, on purpose: every slice is
        # part of one position, and the statement should say so.
        _stamp(entry, order.order_ref)
        trade = self._place(contract, entry)
        self._entry_slices.setdefault(slave_ticket, []).append(trade)
        placed.append(size)

        pos = self._positions.get(slave_ticket)
        if pos is not None:
            # What the broker is now holding or working for this position.
            # close() sizes the exit from this and modify() sizes the legs
            # from it, so it has to grow as the slices go out -- a close
            # against the first slice alone would leave the rest held.
            pos["size"] = round(float(pos["size"]) + float(size), 6)
        logger.info(
            "IBKRAdapter[%s] slice %d/%d: %s %s x%s joined slave_ticket=%s "
            "(%s of %s placed)",
            self.slave_id, n, of, order.direction, order.symbol, size,
            slave_ticket, sum(placed), order.size,
        )
        await self._regrow_protective_legs(slave_ticket, order)

    async def _regrow_protective_legs(self, slave_ticket: int,
                                      order: OrderRequest) -> None:
        """Re-place the stop and target to cover the larger position.

        Cancel and re-place, which is what modify() does. Sized to what is
        now working rather than to the intended total: a stop for 35 while
        only 9 has been placed is a 26-lot short waiting for a touch, and
        it would be a position the copier opened entirely by itself.
        """
        pos = self._positions.get(slave_ticket)
        if pos is None or not order.broker_brackets:
            return
        if pos.get("sl") is None and pos.get("tp") is None:
            return
        ib_mod = self._require_ib()
        self._require_connected()
        self._cancel_tracked_order(pos.get("sl_order_id"))
        self._cancel_tracked_order(pos.get("tp_order_id"))
        closing_action = "SELL" if pos["direction"] == "BUY" else "BUY"
        oca_group = f"tc-{self.slave_id}-{slave_ticket}-{int(time.time())}"
        sl_order_id = tp_order_id = None
        if pos.get("sl") is not None:
            sl_order = self._protective(
                ib_mod.StopOrder(closing_action, pos["size"], pos["sl"]),
                pos.get("outside_rth", False))
            sl_order.ocaGroup = oca_group
            sl_order.ocaType = 1
            _stamp(sl_order, order.order_ref)
            sl_order_id = self._place(pos["contract"], sl_order).order.orderId
        if pos.get("tp") is not None:
            tp_order = self._protective(
                ib_mod.LimitOrder(closing_action, pos["size"], pos["tp"]),
                pos.get("outside_rth", False))
            tp_order.ocaGroup = oca_group
            tp_order.ocaType = 1
            _stamp(tp_order, order.order_ref)
            tp_order_id = self._place(pos["contract"], tp_order).order.orderId
        pos["sl_order_id"] = sl_order_id
        pos["tp_order_id"] = tp_order_id
        # The same check the opening brackets get. A regrow that was
        # rejected leaves a position that WAS protected and now is not,
        # with no signal in flight and nothing else looking.
        await self._check_legs_took(slave_ticket, sl_order_id, tp_order_id)

    def _cancel_tracked_order(self, order_id: Optional[int]) -> None:
        if order_id is None:
            return
        trade = next((t for t in self.ib.trades() if t.order.orderId == order_id), None)
        if trade is not None:
            self.ib.cancelOrder(trade.order)

    async def modify(self, slave_ticket: int, sl: Optional[float], tp: Optional[float]) -> None:
        ib_mod = self._require_ib()
        self._require_connected()
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
                ib_mod.StopOrder(closing_action, pos["size"], self._round_to_tick(
                    sl, pos.get("symbol") or pos["contract"].symbol))
            )
            sl_order.ocaGroup = oca_group
            sl_order.ocaType = 1
            sl_order_id = self._place(pos["contract"], sl_order).order.orderId
        if tp is not None:
            tp_order = self._protective(
                ib_mod.LimitOrder(closing_action, pos["size"], self._round_to_tick(
                    tp, pos.get("symbol") or pos["contract"].symbol))
            )
            tp_order.ocaGroup = oca_group
            tp_order.ocaType = 1
            tp_order_id = self._place(pos["contract"], tp_order).order.orderId

        pos["sl_order_id"] = sl_order_id
        pos["tp_order_id"] = tp_order_id
        # The LEVELS move too. A split entry re-places its legs as it grows
        # and reads them from here, so leaving these at the opening stop
        # would undo every trail the monitor had made.
        pos["sl"] = None if sl is None else self._round_to_tick(
            sl, pos.get("symbol") or pos["contract"].symbol)
        pos["tp"] = None if tp is None else self._round_to_tick(
            tp, pos.get("symbol") or pos["contract"].symbol)

    async def close(self, slave_ticket: int) -> Optional[float]:
        ib_mod = self._require_ib()
        self._require_connected()
        pos = self._positions.get(slave_ticket)
        if pos is None:
            raise AdapterRejectedError(f"no tracked IBKR position for slave_ticket={slave_ticket}")

        self._cancel_tracked_order(pos.get("sl_order_id"))
        self._cancel_tracked_order(pos.get("tp_order_id"))

        # Did the entry ever fill? _entry_trades keeps the Trade until the
        # fill is collected, so a Trade still sitting there with no fill on
        # it means there is no position to close. Selling anyway opens the
        # opposite position -- seen live on UGL, BITX and ETHA.
        entry = self._entry_trades.get(slave_ticket)
        if entry is not None:
            # No more slices. The master is out: a slice landing after this
            # would be a position opened by the copier on its own, for a
            # trade that has already finished.
            if self._stop_splitting(slave_ticket):
                logger.info(
                    "IBKRAdapter[%s] %s: CLOSE arrived mid-window; the slices "
                    "still to come are called off",
                    self.slave_id, pos.get("symbol"),
                )
            # Across EVERY slice, not just the first. A split entry whose
            # second and third slices filled while the first did not is a
            # real position, and reading only the first would abandon it.
            slices = self._entry_slices.get(slave_ticket) or [entry]
            _, filled = self._filled_across(slices)
            if filled <= 0:
                # The RETURN VALUE decides, not the reading above.
                # abandon_unfilled_entry takes one more look and settles
                # the cancel, and it returns a price when the entry
                # filled in that window -- keeping the position and its
                # protective legs for exactly this case. Ignoring that
                # and raising anyway is what happened to MGC 1189 on
                # 2026-11-16: the adapter logged "filled inside the
                # cancel round trip (2.0 @ 3846.0); keeping the
                # position", then told the engine the entry never
                # filled. The engine marked the row CANCELLED, dropped
                # the position from memory and from position_store, and
                # 2 MGC stayed at the broker with nothing managing them.
                settled_price, settled_size = await self.abandon_unfilled_entry(slave_ticket)
                if settled_price is None:
                    raise EntryNeverFilled(
                        f"entry {slave_ticket} for {pos.get('symbol')} never filled; "
                        f"cancelled it and its protective legs instead of placing a "
                        f"closing order against a position that does not exist"
                    )
                filled = float(settled_size or 0.0)
                if filled <= 0:
                    # A price with no size is not a position. Treated as
                    # the cancel it is, rather than sent on to become a
                    # zero-quantity closing order.
                    raise EntryNeverFilled(
                        f"entry {slave_ticket} for {pos.get('symbol')} never filled; "
                        f"cancelled it and its protective legs instead of placing a "
                        f"closing order against a position that does not exist"
                    )
                logger.warning(
                    "IBKRAdapter[%s] entry %s filled %s inside the cancel round "
                    "trip; closing that rather than reporting it as never filled",
                    self.slave_id, slave_ticket, filled,
                )
                # The master is already out, so what is held has to go --
                # all of it and only it. Sizing the close from the order's
                # original size would sell the part that never filled, and
                # on 1189 that was 35 against 2 actually held.
                pos["size"] = filled
            if filled < pos["size"]:
                # Partially filled: close what exists and stop the rest from
                # filling after the master is already out.
                logger.warning(
                    "IBKRAdapter[%s] entry %s filled only %s of %s; closing the "
                    "filled part and cancelling the remainder",
                    self.slave_id, slave_ticket, filled, pos["size"],
                )
                # Same race as abandon_unfilled_entry: the remainder can
                # fill inside the cancel round trip, and closing only the
                # part known a moment ago leaves the rest held under a row
                # about to be marked CLOSED. Every slice is cancelled and
                # settled, because the late fill can land on any of them.
                settled_price, settled = await self._settle_all(
                    slices, "entry remainder")
                if settled is not None and settled > filled:
                    logger.warning(
                        "IBKRAdapter[%s] entry %s filled a further %s inside the "
                        "cancel round trip (%s total); closing all of it",
                        self.slave_id, slave_ticket, settled - filled, settled,
                    )
                    filled = settled
                pos["size"] = filled

        closing_action = "SELL" if pos["direction"] == "BUY" else "BUY"
        closing_order = await self._closing_order(ib_mod, pos, closing_action)
        closing_order.tif = self.entry_tif
        closing_order.outsideRth = pos.get("outside_rth", False)
        trade = self._place(pos["contract"], closing_order)
        # Dropped before awaiting the fill, deliberately: the position is
        # gone from IBKR's point of view the moment the closing order is
        # accepted, and a second CLOSE arriving during the wait must not
        # place a second closing order.
        del self._positions[slave_ticket]
        # Collected out of band too, for the same reason as the entry.
        self._exit_trades[slave_ticket] = (
            trade, float(pos.get("limit_offset_pct", 0.0) or 0.0))
        return None

    async def _closing_order(self, ib_mod, pos, action):
        """Build the exit the same way the entry was built.

        A market order is simply refused outside regular hours, so a leg
        deliberately opened on a limit in the evening session has to be
        closed on one too. The limit is priced THROUGH the current quote in
        the direction that gets filled -- the point is to exit, not to post
        a resting order that quietly never trades.
        """
        if pos.get("order_type") != "limit":
            return ib_mod.MarketOrder(action, pos["size"])

        offset = float(pos.get("limit_offset_pct", 0.0))
        try:
            quote = await self.get_quote(
                pos.get("symbol"), pos.get("month_slot", 0), pos.get("contract_id"))
        except Exception as exc:
            # No quote means no limit price. A market order at least reaches
            # the broker, and inside regular hours it fills; outside them it
            # is refused, which surfaces as an error rather than as a
            # position quietly left open.
            logger.warning(
                "IBKRAdapter[%s] no quote to price the exit for %s (%s); "
                "falling back to a market order",
                self.slave_id, pos.get("symbol"), exc,
            )
            return ib_mod.MarketOrder(action, pos["size"])

        through = (1 - offset) if action == "SELL" else (1 + offset)
        price = self._round_to_tick(quote * through, pos.get("symbol"))
        return ib_mod.LimitOrder(action, pos["size"], price)

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
        partial = None
        while time.monotonic() < deadline:
            status = trade.orderStatus
            filled = float(status.filled or 0.0)
            remaining = float(status.remaining or 0.0)
            if filled > 0 and status.avgFillPrice:
                # Remember it, but do not return yet: a large market order
                # fills in pieces, and the first piece is not the trade. A
                # 47-lot order once reported 1.
                partial = (float(status.avgFillPrice), filled)
                if remaining <= 0:
                    return partial
            # Yield to ib_async's reader so order/exec callbacks can land.
            await asyncio.sleep(0.05)

        if partial is not None:
            # Honest partial: this much really did fill, so recording it
            # beats recording nothing, and close() needs the true number to
            # avoid selling more than exists.
            logger.warning(
                "IBKRAdapter[%s] %s order %s only partially filled within %.1fs "
                "(%s of %s, status=%s) -- recording the part that filled",
                self.slave_id, label, trade.order.orderId, timeout,
                partial[1], trade.order.totalQuantity, trade.orderStatus.status,
            )
            return partial

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
        # A route that asked for its own wait gets it -- but only for a
        # limit. A market order that has not filled in ten seconds has
        # something wrong with it whatever the route says.
        if is_limit and slave_ticket in self._entry_waits:
            timeout = self._entry_waits[slave_ticket]

        slices = self._entry_slices.get(slave_ticket) or [trade]
        if len(slices) > 1 or self._split_pending(slave_ticket):
            price, size = await self._await_split_fill(slave_ticket, timeout)
        elif is_limit and self.entry_chase_steps > 0:
            price, size = await self._chase_entry(trade, timeout)
        else:
            price, size = await self._await_fill(trade, "entry", timeout)
        if price is not None:
            self._entry_trades.pop(slave_ticket, None)
            self._entry_slices.pop(slave_ticket, None)
            self._entry_waits.pop(slave_ticket, None)
        return price, size

    async def _await_split_fill(self, slave_ticket: int, per_slice_timeout: float):
        """Wait for a SPLIT entry, reporting the fill across every slice.

        The window has to be waited out, not just the fill clock. The
        slices after the first have not been placed yet, and returning
        (None, None) at the ordinary timeout tells the engine the entry
        never filled -- which sends it down abandon_unfilled_entry while
        the schedule is still running, cancelling a position halfway
        through building it and leaving the rest of the schedule to place
        orders against nothing.

        So: wait until the schedule has finished AND every slice it placed
        has stopped working, then report the size-weighted fill across all
        of them. One position, one entry price, one number -- which is the
        whole point of splitting into slices rather than into positions.

        A timeout still reports what filled. A split entry where three of
        four slices filled is a real position of three slices, and saying
        so beats saying nothing.
        """
        # The window plus one fill clock: the last slice is placed at the
        # end of the window and deserves the same wait as the first.
        window = float(self._split_windows.get(slave_ticket, 0.0) or 0.0)
        deadline = time.monotonic() + window + per_slice_timeout
        while time.monotonic() < deadline:
            if not self._split_pending(slave_ticket):
                slices = self._entry_slices.get(slave_ticket) or []
                if slices and self._all_slices_done(slices):
                    price, size = self._filled_across(slices)
                    if price is None:
                        # Every slice is terminal and none of them filled.
                        # Nothing more will arrive, so there is no reason
                        # to hold the clock open.
                        break
                    logger.info(
                        "IBKRAdapter[%s] split entry %s filled %s across %d "
                        "slice(s) at %s",
                        self.slave_id, slave_ticket, size, len(slices), price,
                    )
                    return price, size
            await asyncio.sleep(0.1)

        slices = self._entry_slices.get(slave_ticket) or []
        price, size = self._filled_across(slices)
        if price is not None:
            logger.warning(
                "IBKRAdapter[%s] split entry %s filled %s across %d slice(s) "
                "within %.1fs, with some of it still working -- recording the "
                "part that filled",
                self.slave_id, slave_ticket, size, len(slices),
                window + per_slice_timeout,
            )
            return price, size
        logger.warning(
            "IBKRAdapter[%s] split entry %s filled nothing across %d slice(s) "
            "within %.1fs",
            self.slave_id, slave_ticket, len(slices), window + per_slice_timeout,
        )
        return None, None

    async def _chase_entry(self, trade, timeout: float):
        """Wait for a resting entry, re-pricing it toward the quote.

        The wait is cut into slices of entry_chase_interval. After each
        slice that did not fill, the broker is asked for a fresh quote and
        the limit is moved to it -- the same `quote x (1 +/- offset)` the
        order was first priced with, recomputed against a market that has
        since opened.

        WHY NOT STEP FROM THE PREVIOUS LIMIT, which is what the exit
        chase does: an unfilled sell exit proves the market is below it,
        so stepping down from its own price is sound. An unfilled entry
        priced through a stale close proves only that the close was
        stale. Stepping blindly from it would walk away from the market
        as readily as toward it.

        A slice with NO usable quote spends no step. Between 20:00 and
        04:00 ET that is every slice, and burning the budget against a
        shut line would leave nothing for the open.

        The walk only ever moves toward the market, and never further
        than entry_chase_max_pct from the original limit. Chasing an
        entry upward raises the realised risk, because the stop was
        already fixed when the position was sized.
        """
        original = float(trade.order.lmtPrice)
        action = trade.order.action
        symbol = getattr(trade.contract, "symbol", None)
        offset = float(getattr(trade.order, "_gtc_offset", 0.0) or 0.0)
        # ROUNDED TO A TICK HERE, once. The clamp below used to be applied
        # AFTER rounding the wanted price, so a clamped order went out at
        # the raw ceiling -- 39.9455 on a penny-increment stock, which is
        # not a price IBKR accepts. Its own test caught it. Rounding the
        # ceiling instead means both the wanted price and the clamp are
        # on ticks, and the clamp can no longer undo the rounding.
        worst = self._round_to_tick(
            original * (1 + self.entry_chase_max_pct) if action == "BUY"
            else original * (1 - self.entry_chase_max_pct),
            symbol,
        )

        # A DEADLINE, not a decremented counter. The first version of this
        # subtracted each slice from a remaining total, which never
        # terminates when the interval is 0 -- `remaining -= 0.0`, forever,
        # pinning a core. Its own test hung and caught it. A monotonic
        # deadline is correct whatever the interval is, and the 0.05 floor
        # below guarantees the loop always advances.
        deadline = time.monotonic() + float(timeout)
        steps_used = 0
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            if steps_used < self.entry_chase_steps:
                slice_s = min(max(self.entry_chase_interval, 0.05), left)
            else:
                slice_s = left
            price, size = await self._await_fill(trade, "entry", slice_s)
            if price is not None:
                return price, size
            if steps_used >= self.entry_chase_steps:
                break
            if deadline - time.monotonic() <= 0:
                break
            if trade.orderStatus.status in (
                    "Cancelled", "ApiCancelled", "Inactive", "Filled"):
                break

            try:
                quote = await self.get_quote(symbol)
            except AdapterError as exc:
                logger.info(
                    "IBKRAdapter[%s] entry %s still resting; no quote to "
                    "re-price through yet (%s) -- the step is not spent",
                    self.slave_id, trade.order.orderId, exc,
                )
                continue

            through = (1 + offset) if action == "BUY" else (1 - offset)
            want = self._round_to_tick(quote * through, symbol)
            # Toward the market only, and never past the ceiling.
            if action == "BUY":
                want = min(want, worst)
                moved = want > float(trade.order.lmtPrice)
            else:
                want = max(want, worst)
                moved = want < float(trade.order.lmtPrice)
            if not moved:
                logger.info(
                    "IBKRAdapter[%s] entry %s: the quote (%s) does not ask "
                    "for a better price than %s, leaving it",
                    self.slave_id, trade.order.orderId, quote,
                    trade.order.lmtPrice,
                )
                continue

            steps_used += 1
            logger.warning(
                "IBKRAdapter[%s] entry %s still resting at %s; re-pricing to "
                "%s against a fresh quote of %s (chase %d of %d, ceiling %s)",
                self.slave_id, trade.order.orderId, trade.order.lmtPrice,
                want, quote, steps_used, self.entry_chase_steps,
                self._round_to_tick(worst, symbol),
            )
            trade.order.lmtPrice = want
            self._place(trade.contract, trade.order)

        return None, None

    async def _settle_cancel(self, trade, label: str, timeout: float = 5.0):
        """Wait for a cancel REQUEST to resolve, and report what filled.

        `cancelOrder` is a request, not an outcome. When the fill lands
        inside the round trip IBKR answers error 10148 -- "OrderId N that
        needs to be cancelled cannot be cancelled, state: Filled" -- and
        the position is real. Returning before that answer arrives is how
        the copier came to hold 1889 UGL and 641 TQQQ it had recorded as
        CANCELLED: untracked volume that nothing downstream can ever
        reconcile, because reconciliation works from open rows and there
        is no open row.

        Returns (price, size) of whatever actually exists at the broker, or
        (None, None) if the cancel really did take. A PARTIAL fill followed
        by a successful cancel still returns that part: those shares are as
        real as a full fill, and they are where the stray M2K and MBT
        contracts came from.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = trade.orderStatus
            filled = float(status.filled or 0.0)
            remaining = float(status.remaining or 0.0)
            has_fill = filled > 0 and status.avgFillPrice

            if has_fill and remaining <= 0:
                return float(status.avgFillPrice), filled
            if status.status in ("Cancelled", "ApiCancelled", "Inactive"):
                # Terminal. Anything that filled before it stands.
                if has_fill:
                    return float(status.avgFillPrice), filled
                return None, None
            await asyncio.sleep(0.05)

        # No terminal answer in time. Believe the fills, not the request:
        # claiming a cancel we never saw confirmed is the failure this
        # method exists to stop.
        status = trade.orderStatus
        filled = float(status.filled or 0.0)
        if filled > 0 and status.avgFillPrice:
            logger.warning(
                "IBKRAdapter[%s] %s cancel for order %s did not settle in %.1fs; "
                "%s already filled, treating the position as real",
                self.slave_id, label, trade.order.orderId, timeout, filled,
            )
            return float(status.avgFillPrice), filled
        logger.warning(
            "IBKRAdapter[%s] %s cancel for order %s did not settle in %.1fs and "
            "nothing filled; treating it as cancelled",
            self.slave_id, label, trade.order.orderId, timeout,
        )
        return None, None

    async def abandon_unfilled_entry(self, slave_ticket: int):
        """Cancel an entry that never filled, and its protective legs.

        Returns (price, size): a price means the order filled after all --
        either before the cancel was sent, or inside its round trip -- and
        the position stands.

        ORDER OF OPERATIONS MATTERS. The entry's fate is settled BEFORE the
        protective legs are touched, because tearing them down first and
        then discovering the entry filled leaves a live position with no
        stop and no target: strictly worse than either outcome this method
        is choosing between.

        The legs matter as much as the entry. They are placed alongside it
        and carry GTC, so leaving them behind a genuinely cancelled entry
        would strand a stop and target able to execute later and open a
        position nobody asked for.

        A SPLIT ENTRY is several orders and one position, so every slice
        has to go -- and the schedule placing them has to be called off
        first. Cancelling the slices that exist while the task that places
        the next one is still running is how an order gets left at the
        broker under a row the hub has just marked CANCELLED.
        """
        # First, so nothing new is placed behind our back.
        if self._stop_splitting(slave_ticket):
            logger.info(
                "IBKRAdapter[%s] entry %s is being abandoned; the slices still "
                "to come are called off", self.slave_id, slave_ticket,
            )
        trade = self._entry_trades.pop(slave_ticket, None)
        slices = self._entry_slices.pop(slave_ticket, None)
        if not slices:
            slices = [trade] if trade is not None else []
        self._entry_waits.pop(slave_ticket, None)
        pos = self._positions.get(slave_ticket)

        # Last look before cancelling. The fill may have landed while this
        # was being scheduled, and cancelling then would leave a real
        # position with no stop. Across every slice: one filled slice is a
        # position, whichever of them it was.
        if slices:
            price, size = self._filled_across(slices)
            if price is not None:
                logger.info(
                    "IBKRAdapter[%s] entry %s filled just after its timeout "
                    "(%s across %d slice(s)); keeping the position",
                    self.slave_id, slave_ticket, size, len(slices),
                )
                self._entry_trades[slave_ticket] = trade
                self._entry_slices[slave_ticket] = slices
                return price, size

            price, size = await self._settle_all(slices, "entry")
            if price is not None:
                # It filled anyway. Keep the position AND its protective
                # legs -- they are correct for it, and they have not been
                # cancelled yet precisely so that this case can keep them.
                logger.warning(
                    "IBKRAdapter[%s] entry %s filled inside the cancel round trip "
                    "(%s @ %s); keeping the position and its protective legs "
                    "rather than recording a cancel that did not happen",
                    self.slave_id, slave_ticket, size, price,
                )
                self._entry_trades[slave_ticket] = trade
                self._entry_slices[slave_ticket] = slices
                if pos is not None:
                    # Only part of the intended size is held now. The legs
                    # were grown as the slices went out, so they cover more
                    # than exists until this is corrected -- and close()
                    # sizes the exit from it.
                    pos["size"] = float(size)
                return price, size

        if pos is not None:
            self._cancel_tracked_order(pos.get("sl_order_id"))
            self._cancel_tracked_order(pos.get("tp_order_id"))
            del self._positions[slave_ticket]

        logger.warning(
            "IBKRAdapter[%s] cancelled unfilled entry %s (%d slice(s)) and its "
            "protective legs", self.slave_id, slave_ticket, len(slices),
        )
        return None, None

    async def await_exit_fill(self, slave_ticket: int):
        """Wait for the closing order's fill, chasing a limit that rests.

        A resting exit is the one failure this path cannot tolerate: the
        master is already out, so the copy is left carrying a position
        nothing is managing. Seen live on SPXL, where the SELL limit went
        out at 268.14 against a 267.71 market, because the quote it was
        priced through was a delayed one and 0.36% stale.

        So each step re-prices from the PREVIOUS LIMIT, not from a fresh
        quote: an unfilled sell at 268.14 is itself evidence that the market
        is below 268.14, which is more than the stale quote knows. The walk
        is bounded by exit_chase_steps, so a bad quote costs a known number
        of ticks rather than an open-ended slide.

        A market order is not substituted after the first failure. Outside
        regular hours IBKR refuses one, and outside regular hours is when
        these exits run.
        """
        entry = self._exit_trades.pop(slave_ticket, None)
        if entry is None:
            return None, None
        trade, offset = entry

        price, size = await self._await_fill(trade, "close")
        if price is not None:
            return price, size
        if getattr(trade.order, "orderType", "") != "LMT":
            # A market order that did not fill is not a pricing problem,
            # and re-pricing it is not a thing that exists.
            return price, size

        step_pct = float(offset or 0.0) or float(self.exit_chase_pct)
        steps = max(0, int(self.exit_chase_steps))
        action = trade.order.action
        symbol = getattr(trade.contract, "symbol", None)

        for step in range(1, steps + 1):
            status = trade.orderStatus.status
            if status in ("Cancelled", "ApiCancelled", "Inactive", "Filled"):
                break
            through = (1 - step_pct) if action == "SELL" else (1 + step_pct)
            was = float(trade.order.lmtPrice)
            now = self._round_to_tick(was * through, symbol)
            if now == was:
                # One tick is coarser than the offset; step by a tick so the
                # chase always actually moves.
                tick = self._tick_size(symbol)
                now = self._round_to_tick(
                    was - tick if action == "SELL" else was + tick, symbol)
            logger.warning(
                "IBKRAdapter[%s] exit %s still resting at %s; re-pricing to %s "
                "(chase %d of %d) -- the quote it was priced through was stale",
                self.slave_id, trade.order.orderId, was, now, step, steps,
            )
            trade.order.lmtPrice = now
            self._place(trade.contract, trade.order)
            price, size = await self._await_fill(trade, "close chase")
            if price is not None:
                return price, size

        logger.error(
            "IBKRAdapter[%s] exit %s for %s never filled after %d re-prices; "
            "the order is still live at the broker and the position is still "
            "open there -- it needs a look",
            self.slave_id, trade.order.orderId, symbol, steps,
        )
        return None, None

    async def preview_margin(self, order: OrderRequest) -> MarginPreview:
        """IBKR's own margin and commission for this order, unplaced.

        whatIfOrder is a real round trip, so it is only worth making when
        something will act on the answer. IBKR returns a huge sentinel
        (~1.8e308) rather than null for a field it cannot supply, which has
        to be filtered or it arrives looking like a plausible number.
        """
        ib_mod = self._require_ib()
        self._require_connected()
        contract = await self._contract_for_request(
            order.symbol, order.month_slot, order.contract_id)
        action = "BUY" if order.direction == "BUY" else "SELL"

        probe = ib_mod.MarketOrder(action, order.size)
        probe.tif = self.entry_tif
        if self.account:
            probe.account = self.account
        state = await self.ib.whatIfOrderAsync(contract, probe)

        def number(value):
            try:
                f = float(value)
            except (TypeError, ValueError):
                return None
            return None if abs(f) > 1e100 else f

        return MarginPreview(
            initial_change=number(state.initMarginChange),
            initial_after=number(state.initMarginAfter),
            maintenance_after=number(state.maintMarginAfter),
            commission=number(state.commission),
        )

    async def get_status(self) -> bool:
        return self.ib is not None and self.ib.isConnected()

    async def get_account_summary(self, account_type: str) -> AccountSummary:
        self._require_connected()
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
        self._require_connected()
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
        self._require_connected()
        contract = await self._contract_for_request(symbol, month_slot, contract_id)

        attempts = 1 + max(0, int(self.quote_retries))
        price = float("nan")
        for attempt in range(attempts):
            if attempt:
                # A short beat, not a long one: the losing request needs
                # only the competing one to have finished, which takes about
                # as long as a quote does.
                await asyncio.sleep(self.quote_timeout)
            price = await self._sample_quote(contract, symbol)
            if price == price and price > 0:
                if attempt:
                    logger.info(
                        "IBKRAdapter[%s] %s quoted on attempt %d of %d",
                        self.slave_id, symbol, attempt + 1, attempts)
                break
            if price == price:
                # A real number that is not positive: IBKR saying there is
                # no market. Still true in a second, so stop.
                break
            if attempt + 1 < attempts:
                logger.info(
                    "IBKRAdapter[%s] %s returned nothing on attempt %d of %d; "
                    "retrying -- usually another request on this account held "
                    "the market data line",
                    self.slave_id, symbol, attempt + 1, attempts)

        return self._require_usable_quote(price, symbol)

    async def _sample_quote(self, contract, symbol: str) -> float:
        """One subscribe-wait-read cycle. NaN when nothing arrived."""
        self._require_connected()
        ticker = self.ib.reqMktData(contract, "", False, False)
        # asyncio.sleep, NOT ib.sleep(): IB.sleep() is a *synchronous* helper
        # that returns a bool, so `await self.ib.sleep(1)` raised
        # "object bool can't be used in 'await' expression" on every call.
        # It also drives the event loop re-entrantly, which is wrong inside a
        # running one -- it exists for sync scripts. (reqTickersAsync() is the
        # tidier one-shot if this ever needs revisiting.)
        await asyncio.sleep(self.quote_timeout)
        price = ticker.marketPrice()
        # NaN means "nothing has arrived", which is not the same as IBKR
        # saying there is no market (-1). With delayed data NaN is common
        # even on an open session, and refusing on it dropped MES at 00:42
        # while the session was open -- US500 produced no futures leg at
        # all. So fall back to the last trade, then the session close,
        # before giving up. A -1 is left to fail below: that is IBKR
        # stating there is no market, and a stale close is no substitute.
        #
        # A -1 USED TO FALL STRAIGHT THROUGH TO THE REFUSAL, on the
        # reasoning that IBKR is stating there is no market and a stale
        # close is no substitute. That reasoning holds for a market order
        # on a futures leg. It is wrong for the ETF legs, and Chris caught
        # it: "UGL is trading EXO hours on schwab right now" -- at 22:38
        # on a Sunday, inside the 24/5 overnight session. The hub's own
        # log agrees, carrying marketPrice=39.2155 for the UGL position
        # from IBKR's account feed at the very moment reqMktData returned
        # -1. So the instrument is trading and IBKR holds a price for it;
        # what is missing is a quote on THIS market-data line.
        #
        # Enabled per slave, and off by default, because the two cases
        # really are different. These routes place a LIMIT that rests with
        # outside_rth set, so a stale anchor costs a missed fill, never a
        # bad one -- a BUY limit priced below a market that has risen
        # simply does not fill. A market order priced off a stale close is
        # a genuinely bad fill, which is why futures slaves keep the
        # strict behaviour.
        #
        # Sizing is unaffected either way: the stop distance is ATR-based,
        # so only the limit's anchor is stale, not the risk.
        stale_ok = self.quote_fallback == "last_close"
        if price != price or (price <= 0 and stale_ok):
            why = "no marketPrice" if price != price else f"marketPrice={price:g}"
            for name, candidate in (("last", ticker.last), ("close", ticker.close)):
                if candidate is not None and candidate == candidate and candidate > 0:
                    logger.info(
                        "IBKRAdapter[%s] %s had %s; using %s=%s",
                        self.slave_id, symbol, why, name, candidate,
                    )
                    price = float(candidate)
                    break
        self.ib.cancelMktData(contract)
        return float(price)

    def _require_usable_quote(self, price: float, symbol: str) -> float:
        """Accept a usable price, or refuse with advice on what to do.

        Kept separate from the sampling so the retry loop can decide whether
        a given failure is worth another attempt.
        """
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
        self._require_connected()
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
        self._require_connected()
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

    def _stamp_entry_timing(self, entry, order: OrderRequest):
        """Put the entry's time in force, and its deadline, on the order.

        The ROUTE's TIF wins, falling back to this slave's default. It used
        to be the slave's always, which is why an ETF entry could not be
        made to survive the session it was placed in: every route on
        ibkr-etf-1 got DAY, and a DAY order placed at 18:00 ET expires that
        evening rather than resting into Monday's pre-market.

        GTD is the one that needs more than a name. It was in ENTRY_TIFS
        from the start and `goodTillDate` was never set, so a route asking
        to expire at the open got an order with no deadline at all --
        rejected outright by some TWS versions and treated as GTC by
        others. The second is the dangerous one: the entry rests
        indefinitely and can fill days later, off a signal nobody is acting
        on any more, into a position no stop was sized for.

        A method rather than inline in open(), so the decision can be
        tested without a broker connection. Everything about it is a
        refusal or a string.
        """
        tif = (order.tif or self.entry_tif or "DAY").upper()
        if tif not in self.ENTRY_TIFS:
            # Refused here, not sent. An order IBKR rejects for a bad TIF is
            # a position that silently never opens.
            raise AdapterError(
                f"{tif!r} is not a time in force this adapter will place on an "
                f"entry (accepts {', '.join(self.ENTRY_TIFS)})"
            )
        entry.tif = tif
        if tif != "GTD":
            return entry

        spec = order.good_till if order.good_till is not None else self.entry_good_till
        try:
            deadline = resolve_good_till(spec)
        except OrderTimingError as exc:
            raise AdapterError(
                f"GTD entry for {order.symbol} has no usable deadline "
                f"({spec!r}): {exc}"
            ) from exc
        if not deadline:
            raise AdapterError(
                f"GTD entry for {order.symbol} names no deadline. Set "
                f"good_till on the route -- a session boundary such as "
                f"'rth_close', or a number of seconds -- or use GTC if the "
                f"entry is genuinely meant to rest until cancelled"
            )
        entry.goodTillDate = deadline
        logger.info(
            "IBKRAdapter[%s] %s entry is GTD until %s (from %r)",
            self.slave_id, order.symbol, deadline, spec,
        )
        return entry

    @staticmethod
    def _qualified_only(results):
        """Drop the Nones ib_async leaves in place of a contract it could
        not qualify.

        It returns a list the same length as the request, with None where
        IBKR refused, so `if not qualified` passes on [None] and the next
        line raises AttributeError instead. Preflight surfaced it: an
        unroutable MNG reported "'NoneType' object has no attribute
        'conId'" rather than that IBKR has never heard of the contract,
        which is the whole point of the message.
        """
        return [k for k in (results or []) if k is not None]

    @staticmethod
    def _tick_size(symbol: str) -> float:
        """The contract's minimum price variation, or a cent.

        Shared with _round_to_tick so the exit chase can guarantee it moves:
        an offset smaller than one tick rounds back to where it started, and
        a chase that does not move is not a chase.
        """
        spec = get_contract_spec(symbol)
        tick = spec.tick_size if spec else 0.01
        return tick if tick and tick > 0 else 0.01

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
        try:
            multiplier = float(contract.multiplier)
        except (TypeError, ValueError):
            # Equities report "" rather than 1, and nothing is asked of it
            # there: an ETF has no spec for it to disagree with.
            multiplier = None
        return ContractInfo(
            symbol=symbol,
            contract_id=contract.conId,
            local_symbol=contract.localSymbol,
            expiry=contract.lastTradeDateOrContractMonth,
            multiplier=multiplier,
        )

    async def get_working_orders(self) -> List[WorkingOrder]:
        """Entries still resting at IB, by the reference we stamped.

        Deliberately entries only. A protective stop or target is also a
        working order, and counting those would make every open position
        look like it had an unfilled entry -- so the two legs this adapter
        places alongside an entry are filtered out by their own action
        being opposite to the entry's. Simpler and more robust: the
        protective legs are placed as STP/LMT children, and only the
        PARENT carries no parentId.

        Reads the session's own view rather than asking IB again: ib_async
        keeps openTrades() current from the order stream, and a fresh
        reqAllOpenOrders on a reconnect that has not settled returns
        nothing, which would read as "no working orders" -- the exact
        wrong answer here.
        """
        # self.ib, NOT self._require_ib(). The latter returns the ib_async
        # MODULE, and this read `ib = self._require_ib()` followed by
        # `ib.openTrades()` -- an AttributeError on the module, raised on
        # the reconciliation path that every engine start runs through.
        # Both IBKR slaves failed to start because of it on 2026-11-15
        # 18:04, with real positions at the broker and the market opening:
        # "the hub is running WITHOUT it".
        #
        # The name was the whole bug. Everywhere else in this file calls
        # the module `ib_mod` and the client `self.ib`; calling the module
        # `ib` here made the wrong line look exactly right.
        if self.ib is None:
            raise AdapterConnectionError(
                "not connected, so IB's working orders cannot be read"
            )
        out: List[WorkingOrder] = []
        for trade in self.ib.openTrades():
            order = getattr(trade, "order", None)
            status = getattr(getattr(trade, "orderStatus", None), "status", "") or ""
            if order is None:
                continue
            # Anything terminal is not working, whatever the list says.
            if status in ("Filled", "Cancelled", "ApiCancelled", "Inactive"):
                continue
            # A child leg (stop/target) hangs off a parent; an entry does
            # not. Without this every protected position reports a working
            # entry it does not have.
            if getattr(order, "parentId", 0):
                continue
            contract = getattr(trade, "contract", None)
            out.append(WorkingOrder(
                order_ref=(getattr(order, "orderRef", "") or None),
                symbol=(getattr(contract, "localSymbol", "")
                        or getattr(contract, "symbol", "") or ""),
                broker_order_id=(str(getattr(order, "permId", "") or "") or None),
                status=status,
            ))
        return out

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
        self._require_connected()
        contract = await self._contract_for_request(symbol, month_slot, contract_id)
        ticker = self.ib.reqMktData(contract, "", False, False)
        await asyncio.sleep(self.quote_timeout)
        volume = ticker.volume
        self.ib.cancelMktData(contract)
        if volume != volume or volume is None or volume < 0:   # NaN / absent
            return None
        return float(volume)

    async def get_open_positions(self, account_type: str) -> List[Position]:
        self._require_connected()
        results = []
        for pos in self.ib.positions(self.account):
            if pos.position == 0:
                continue
            results.append(
                Position(symbol=pos.contract.symbol, quantity=pos.position, average_price=pos.avgCost)
            )
        return results
