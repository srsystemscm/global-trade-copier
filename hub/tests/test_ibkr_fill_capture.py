"""The IBKR leg has to report what it actually filled at.

Seen live on 2026-11-11: an MGC round trip copied from an XAUUSD master
filled at 3988.40 and closed at 3988.20, and the registry stored NULL for
both. The interface was already there -- OrderResult carries price/lots and
base.close() is declared -> Optional[float], which the MT4/MT5 adapters
honour -- but the IBKR adapter returned as soon as the order was
acknowledged, about two seconds before the fill arrived.

That made variance capture blind on the futures leg specifically, which is
the leg it exists to measure: the whole point of copying a CFD to a future
is to learn what the basis and the slippage do, and a NULL says nothing.

Also pinned here: the entry's time-in-force. Left unset, IBKR's account
preset supplies DAY and reports it as "Error 10349", cancelling the order and
re-creating it before it fills -- so the order passes through Cancelled on a
perfectly good trade.
"""
import asyncio

import pytest

from app.adapters import create_adapter
from app.adapters.base import OrderRequest
from app.slaves import SlaveConfig


class _Status:
    def __init__(self, avg=0.0, filled=0.0, status="PreSubmitted", remaining=None):
        self.avgFillPrice = avg
        self.filled = filled
        self.status = status
        # A large market order fills in pieces, so "has it finished" is
        # remaining == 0, not "has anything filled". Defaults to done when
        # a quantity filled, which is what the simple fakes mean.
        self.remaining = 0.0 if remaining is None else remaining


class _Order:
    def __init__(self, order_id=1):
        self.orderId = order_id
        self.totalQuantity = 0.0
        self.tif = None
        self.outsideRth = False
        self.account = None
        self.ocaGroup = None
        self.ocaType = None


class _Trade:
    """A trade that fills after `fills_after` polls, like ib_async's does."""

    def __init__(self, price=100.0, size=1.0, fills_after=2, order_id=1):
        self.order = _Order(order_id)
        self.orderStatus = _Status()
        self.fills = []
        self._price, self._size = price, size
        self._left = fills_after

    def poll(self):
        if self._left > 0:
            self._left -= 1
            if self._left == 0:
                self.fills = ["fill"]
                self.orderStatus = _Status(self._price, self._size, "Filled")

    def on_cancel(self):
        """What the broker does with a cancel REQUEST.

        An order that has already filled refuses it -- that is IBKR error
        10148, "OrderId N that needs to be cancelled cannot be cancelled,
        state: Filled" -- and stays Filled. Anything else goes Cancelled.

        The fake did not model this at all before, which is exactly why the
        stray-position bug had passing tests over it: cancelOrder recorded
        the request and nothing ever answered, so every test agreed with
        the code's assumption that asking is the same as it happening.
        """
        if self.fills:
            return
        self.orderStatus = _Status(0.0, 0.0, "Cancelled")


class _NeverFills(_Trade):
    def __init__(self):
        super().__init__(fills_after=10**9)


class _FillsOnCancel(_Trade):
    """The 10148 case: the fill lands INSIDE the cancel round trip.

    Live, this produced 1889 UGL and 641 TQQQ held against registry rows
    that read CANCELLED.
    """

    def __init__(self, price=39.10, size=1889.0, order_id=1):
        super().__init__(price=price, size=size, fills_after=10**9,
                         order_id=order_id)
        self._fill_price, self._fill_size = price, size

    def on_cancel(self):
        self.fills = ["fill"]
        self.orderStatus = _Status(self._fill_price, self._fill_size, "Filled")


class _PartialThenCancelled(_Trade):
    """Some of it filled, and then the cancel really did take.

    Those units are as real as a full fill. This is where the stray M2K
    and MBT contracts came from.
    """

    def __init__(self, price=2784.3, part=10.0, total=21.0, order_id=1):
        super().__init__(price=price, size=total, fills_after=10**9,
                         order_id=order_id)
        self._fill_price, self._part, self._total = price, part, total

    def on_cancel(self):
        self.fills = ["fill"]
        self.orderStatus = _Status(self._fill_price, self._part, "Cancelled",
                                   remaining=self._total - self._part)


def _adapter(**cfg):
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002,
            "client_id": 1, "fill_timeout": 0.5}
    base.update(cfg)
    return create_adapter(SlaveConfig(id="i", name="i", broker_type="ibkr",
                                      enabled=True, paused=False, config=base))


def _drive(adapter, trade, coro):
    """Run `coro`, advancing `trade` the way the broker's reader would."""

    async def ticker():
        for _ in range(200):
            trade.poll()
            await asyncio.sleep(0.01)

    async def both():
        t = asyncio.create_task(ticker())
        try:
            return await coro
        finally:
            t.cancel()

    return asyncio.run(both())


# ---------------- the wait itself ----------------------------------------

def test_await_fill_returns_price_and_size():
    a = _adapter()
    trade = _Trade(price=3988.4, size=1.0)
    price, size = _drive(a, trade, a._await_fill(trade, "entry"))
    assert price == 3988.4
    assert size == 1.0


def test_await_fill_gives_up_without_claiming_failure():
    """A timeout means 'no price captured', never 'no position' -- the order
    is live at the broker, so raising here would unwind a trade that exists."""
    a = _adapter(fill_timeout=0.1)
    trade = _NeverFills()
    price, size = _drive(a, trade, a._await_fill(trade, "entry"))
    assert price is None
    assert size is None


def test_await_fill_ignores_a_transient_cancel():
    """Error 10349 cancels the order and re-creates it, so status passes
    through Cancelled on a good trade. Keying on the fill, not the status,
    is what makes that a non-event."""
    a = _adapter()
    trade = _Trade(price=3988.4)
    trade.orderStatus = _Status(0.0, 0.0, "Cancelled")
    price, _ = _drive(a, trade, a._await_fill(trade, "entry"))
    assert price == 3988.4


def test_await_fill_does_not_trust_a_fill_without_a_price():
    """status.filled can be set a beat before avgFillPrice lands; reporting
    0.0 as the fill price would poison variance capture far more quietly
    than reporting nothing."""
    a = _adapter(fill_timeout=0.1)
    trade = _NeverFills()
    trade.fills = ["fill"]
    trade.orderStatus = _Status(0.0, 1.0, "Filled")
    price, _ = _drive(a, trade, a._await_fill(trade, "entry"))
    assert price is None


# ---------------- time in force ------------------------------------------

def test_entry_tif_defaults_to_day():
    assert _adapter().entry_tif == "DAY"
    assert _adapter(entry_tif="GTC").entry_tif == "GTC"


def test_entry_and_close_state_a_tif():
    """Both placements, not just the entry: the closing order hit 10349 too."""
    import inspect
    import re
    from app.adapters.ibkr import IBKRAdapter

    open_src = re.sub(r"\s+", " ", inspect.getsource(IBKRAdapter.open))
    close_src = re.sub(r"\s+", " ", inspect.getsource(IBKRAdapter.close))
    assert "entry.tif = self.entry_tif" in open_src
    assert "closing_order.tif = self.entry_tif" in close_src


def test_brackets_keep_their_own_tif():
    """The entry expires with the session; a stop must not."""
    a = _adapter()
    assert a.bracket_tif == "GTC"
    assert a.entry_tif != a.bracket_tif


# ---------------- open()/close() surface it ------------------------------

class _FakeIB:
    """Hands out one Trade per placeOrder, numbered the way IBKR numbers."""

    def __init__(self, trades):
        self._queue = list(trades)
        self.placed = []
        self.cancelled = []
        self._all = []

    def placeOrder(self, contract, order):
        self.placed.append(order)
        order.orderId = 46 + len(self.placed)  # entry 47, then the legs
        t = self._queue.pop(0) if self._queue else _NeverFills()
        t.order = order
        self._all.append(t)
        return t

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)
        trade = next((t for t in self._all
                      if t.order.orderId == order.orderId), None)
        if trade is not None:
            trade.on_cancel()

    def trades(self):
        return list(self._all)

    def isConnected(self):
        return True


def _prepared(adapter, *trades):
    """Give the adapter just enough to run open()/close() offline."""
    adapter.ib = _FakeIB(trades)
    adapter.bracket_check_delay = 0
    contract = object()
    adapter._contracts[("MGC", 0)] = contract

    async def _contract(symbol, month_slot, contract_id=None):
        return contract

    adapter._contract_for_request = _contract
    adapter._round_to_tick = lambda p, s: p
    return adapter


def test_open_does_not_wait_for_the_fill():
    """The MODIFY and CLOSE of this very ticket queue behind open(), so it
    must not hold the signal path while the broker works."""
    import time as _time

    a = _prepared(_adapter(fill_timeout=30), _NeverFills(), _NeverFills())
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=None, tp=None,
                       master_ticket=382160920)
    asyncio.run(a.open(req))  # warm the imports, so the timing below is open()

    started = _time.monotonic()
    res = asyncio.run(a.open(req))
    elapsed = _time.monotonic() - started
    # Generous against a loaded CI box, yet nowhere near fill_timeout: the
    # failure this guards is a wait of tens of seconds, not milliseconds.
    assert elapsed < 2.0, "open() waited %.1fs with fill_timeout=30" % elapsed
    assert res.price is None  # not captured *yet*, which is not a failure
    assert a.ib.placed[0].tif == "DAY"


def test_entry_fill_is_collected_after_open_returns():
    a = _prepared(_adapter(), _Trade(price=3988.4, size=1.0, fills_after=2))
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=None, tp=None,
                       master_ticket=382160920)
    res = asyncio.run(a.open(req))
    trade = a.ib._all[0]
    price, size = _drive(a, trade, a.await_entry_fill(res.slave_ticket))
    assert (price, size) == (3988.4, 1.0)


def test_await_entry_fill_watches_the_entry_not_a_bracket_leg():
    """Regression: the bracket-verification loop rebound `trade`, so the wait
    watched the stop leg -- which never fills while the position is open, so
    it timed out every single time while the entry had filled in a second."""
    entry = _Trade(price=3988.4, size=1.0, fills_after=2)
    a = _prepared(_adapter(), entry, _NeverFills())  # entry, then the stop
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=3876.5,
                       tp=None, master_ticket=382160920)
    res = asyncio.run(a.open(req))
    assert len(a.ib.placed) == 2, "expected an entry and a stop"
    assert a._entry_trades[res.slave_ticket] is entry
    price, _ = _drive(a, entry, a.await_entry_fill(res.slave_ticket))
    assert price == 3988.4


def test_close_defers_the_exit_price_too():
    a = _prepared(_adapter(), _Trade(price=3988.2, size=1.0, fills_after=2))
    a._positions[35] = {"direction": "BUY", "size": 1.0,
                        "contract": object(), "sl_order_id": None,
                        "tp_order_id": None}
    assert asyncio.run(a.close(35)) is None
    trade = a.ib._all[0]
    price, _ = _drive(a, trade, a.await_exit_fill(35))
    assert price == 3988.2
    assert a.ib.placed[0].tif == "DAY"


def test_collected_trades_are_not_retained():
    """A long-resting order holds one entry; nothing accumulates."""
    a = _prepared(_adapter(), _Trade(price=1.0, fills_after=1))
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=None, tp=None,
                       master_ticket=1)
    res = asyncio.run(a.open(req))
    assert res.slave_ticket in a._entry_trades
    _drive(a, a.ib._all[0], a.await_entry_fill(res.slave_ticket))
    assert a._entry_trades == {}
    # Collecting twice is harmless -- reconciliation may ask again.
    assert asyncio.run(a.await_entry_fill(res.slave_ticket)) == (None, None)


def test_a_second_close_places_no_second_order():
    """Closing twice must not send another closing order -- that would open
    an unintended short on a flat account."""
    a = _prepared(_adapter(), _Trade(price=1.0, fills_after=1))
    a._positions[35] = {"direction": "BUY", "size": 1.0,
                        "contract": object(), "sl_order_id": None,
                        "tp_order_id": None}

    async def two_closes():
        await a.close(35)
        with pytest.raises(Exception):
            await a.close(35)

    asyncio.run(two_closes())
    assert len(a.ib.placed) == 1


# ---------------- an entry that never fills ------------------------------
#
# A limit placed into a thin evening book may simply not fill. IBKR accepts
# it, so nothing is rejected and the deferral -- which retries rejections --
# never fires. What must not be left behind is the protective legs: they are
# placed alongside the entry and carry GTC, so an unfilled entry would leave
# a live stop and target with no position behind them, either able to
# execute later and OPEN a position nobody asked for.

def test_a_resting_limit_gets_a_longer_clock_than_a_market_order():
    """Observing a market fill takes seconds; letting a limit rest in the
    evening book is meant to take minutes. Different questions, so the
    timeout actually applied has to follow the order type."""
    a = _prepared(_adapter(fill_timeout=0.05, entry_timeout=5.0), _NeverFills())
    used = {}

    async def _record(trade, label, timeout=None):
        used["timeout"] = timeout
        return None, None

    a._await_fill = _record

    market = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=None,
                          tp=None, master_ticket=1)
    res = asyncio.run(a.open(market))
    asyncio.run(a.await_entry_fill(res.slave_ticket))
    assert used["timeout"] == 0.05, "a market order waited on the limit clock"

    a2 = _prepared(_adapter(fill_timeout=0.05, entry_timeout=5.0), _NeverFills())
    a2._await_fill = _record
    limit = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=None,
                         tp=None, master_ticket=2,
                         order_type="limit", limit_price=41.60)
    res2 = asyncio.run(a2.open(limit))
    asyncio.run(a2.await_entry_fill(res2.slave_ticket))
    assert used["timeout"] == 5.0, "a resting limit was cut off at fill_timeout"


def test_timeout_leaves_the_trade_reachable_for_cancelling():
    """await_entry_fill() must not discard the Trade on a timeout, or there
    is no way left to cancel the order it refers to."""
    a = _prepared(_adapter(fill_timeout=0.1, entry_timeout=0.1), _NeverFills())
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=None, tp=None,
                       master_ticket=1)
    res = asyncio.run(a.open(req))
    price, _ = _drive(a, a.ib._all[0], a.await_entry_fill(res.slave_ticket))
    assert price is None
    assert res.slave_ticket in a._entry_trades, "the Trade was thrown away"


def test_abandon_cancels_the_entry_and_both_legs():
    a = _prepared(_adapter(fill_timeout=0.1, entry_timeout=0.1),
                  _NeverFills(), _NeverFills(), _NeverFills())
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0,
                       sl=3876.5, tp=4100.0, master_ticket=1)
    res = asyncio.run(a.open(req))
    entry_id = res.slave_ticket
    assert len(a.ib.placed) == 3, "expected entry + stop + target"

    price, _ = asyncio.run(a.abandon_unfilled_entry(entry_id))
    assert price is None
    assert set(a.ib.cancelled) == {entry_id, res.sl_order_id, res.tp_order_id}
    assert entry_id not in a._positions
    assert entry_id not in a._entry_trades


def test_abandon_keeps_a_position_that_filled_in_the_gap():
    """The fill can land between the timeout and the cancel. Cancelling then
    would strand a real position with no stop at all."""
    entry = _Trade(price=3988.4, size=1.0, fills_after=1)
    a = _prepared(_adapter(), entry, _NeverFills())
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=3876.5,
                       tp=None, master_ticket=1)
    res = asyncio.run(a.open(req))
    entry.poll()  # the fill arrives late, before abandon is called

    price, size = asyncio.run(a.abandon_unfilled_entry(res.slave_ticket))
    assert (price, size) == (3988.4, 1.0)
    assert a.ib.cancelled == [], "cancelled a position that had filled"
    assert res.slave_ticket in a._positions


def test_abandon_is_safe_when_there_is_nothing_to_cancel():
    a = _prepared(_adapter(), _NeverFills())
    assert asyncio.run(a.abandon_unfilled_entry(999)) == (None, None)


# ---------------- closing an entry that never filled --------------------
#
# Seen live on 2026-11-11. Three ETF entry limits were placed into a closed
# market, never filled, and then the master's CLOSE arrived -- so close()
# placed a market SELL for a position that did not exist and cancelled the
# protective legs. Six orders were left resting against a flat account, the
# entry limits able to fill from 04:00 and the market SELLs from 09:30. A
# limit that failed to fill while its SELL went through is a naked short
# with no stop, created entirely by the copier.

def test_closing_an_unfilled_entry_cancels_it_instead_of_selling():
    from app.adapters.base import EntryNeverFilled

    a = _prepared(_adapter(), _NeverFills(), _NeverFills(), _NeverFills())
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=3876.5,
                       tp=4100.0, master_ticket=1)
    res = asyncio.run(a.open(req))
    placed_on_open = len(a.ib.placed)

    with pytest.raises(EntryNeverFilled):
        asyncio.run(a.close(res.slave_ticket))

    assert len(a.ib.placed) == placed_on_open, "placed a closing order anyway"
    assert set(a.ib.cancelled) == {res.slave_ticket, res.sl_order_id, res.tp_order_id}
    assert res.slave_ticket not in a._positions


def test_a_filled_entry_still_closes_normally():
    entry = _Trade(price=3988.4, size=1.0, fills_after=1)
    a = _prepared(_adapter(), entry, _Trade(price=1.0, fills_after=1))
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=None, tp=None,
                       master_ticket=1)
    res = asyncio.run(a.open(req))
    entry.poll()                      # the entry fills
    asyncio.run(a.close(res.slave_ticket))
    assert len(a.ib.placed) == 2      # entry + the closing order
    assert a.ib.placed[1].action == "SELL"


def test_a_partial_fill_closes_only_what_exists():
    """Closing the full requested size would short the unfilled remainder."""
    entry = _Trade(price=100.0, size=40.0, fills_after=1)
    a = _prepared(_adapter(), entry, _Trade(price=1.0, fills_after=1))
    req = OrderRequest(symbol="MGC", direction="BUY", size=120.0, sl=None,
                       tp=None, master_ticket=1)
    res = asyncio.run(a.open(req))
    entry.poll()                      # 40 of 120
    asyncio.run(a.close(res.slave_ticket))
    assert a.ib.placed[1].totalQuantity == 40.0
    assert res.slave_ticket in a.ib.cancelled, "left the remainder able to fill"


# ---------------- the exit mirrors the entry ----------------------------

def test_a_limit_entry_is_closed_with_a_limit_in_the_same_session():
    """A market order is refused outside regular hours, and an exit without
    outsideRth waits for the 09:30 open -- the live bug showed DAY/rth on the
    SELL against DAY/outRTH on the entry."""
    a = _prepared(_adapter(), _Trade(price=41.60, size=120.0, fills_after=1),
                  _Trade(price=1.0, fills_after=1))

    async def _quote(symbol, month_slot=0, contract_id=None):
        return 41.50

    a.get_quote = _quote
    req = OrderRequest(symbol="UGL", direction="BUY", size=120.0, sl=None,
                       tp=None, master_ticket=1, order_type="limit",
                       limit_price=41.68, outside_rth=True,
                       limit_offset_pct=0.002)
    res = asyncio.run(a.open(req))
    a.ib._all[0].poll()
    asyncio.run(a.close(res.slave_ticket))

    exit_order = a.ib.placed[1]
    assert exit_order.orderType == "LMT"
    assert exit_order.outsideRth is True
    # priced THROUGH the quote downward for a sale, so it actually trades
    assert exit_order.lmtPrice < 41.50


def test_a_market_entry_is_still_closed_at_market():
    a = _prepared(_adapter(), _Trade(price=3988.4, fills_after=1),
                  _Trade(price=1.0, fills_after=1))
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=None,
                       tp=None, master_ticket=1)
    res = asyncio.run(a.open(req))
    a.ib._all[0].poll()
    asyncio.run(a.close(res.slave_ticket))
    assert a.ib.placed[1].orderType == "MKT"
    assert a.ib.placed[1].outsideRth is False


def test_no_quote_falls_back_to_market_rather_than_leaving_it_open():
    a = _prepared(_adapter(), _Trade(price=41.60, size=120.0, fills_after=1),
                  _Trade(price=1.0, fills_after=1))

    async def _no_quote(symbol, month_slot=0, contract_id=None):
        raise RuntimeError("no market data")

    a.get_quote = _no_quote
    req = OrderRequest(symbol="UGL", direction="BUY", size=120.0, sl=None,
                       tp=None, master_ticket=1, order_type="limit",
                       limit_price=41.68, outside_rth=True)
    res = asyncio.run(a.open(req))
    a.ib._all[0].poll()
    asyncio.run(a.close(res.slave_ticket))
    assert a.ib.placed[1].orderType == "MKT"


# ---------------- broker brackets can be declined -----------------------

def test_an_exit_driven_route_rests_no_stop_or_target():
    """Stops and targets do not execute in extended hours, so resting them
    achieves nothing while leaving GTC orders to strand."""
    a = _prepared(_adapter(), _NeverFills())
    req = OrderRequest(symbol="UGL", direction="BUY", size=120.0, sl=39.07,
                       tp=45.0, master_ticket=1, order_type="limit",
                       limit_price=41.68, outside_rth=True,
                       broker_brackets=False)
    res = asyncio.run(a.open(req))
    assert len(a.ib.placed) == 1, "rested a bracket on an exit-driven route"
    assert res.sl_order_id is None
    assert res.tp_order_id is None


def test_brackets_are_still_the_default():
    a = _prepared(_adapter(), _NeverFills(), _NeverFills())
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=3876.5,
                       tp=None, master_ticket=1)
    res = asyncio.run(a.open(req))
    assert len(a.ib.placed) == 2
    assert res.sl_order_id is not None


# ---------------- partial fills ------------------------------------------
#
# Seen live on 2026-11-12 across six futures routes: a 47-lot M2K market
# order reported 1 filled, MCL ordered 32 and reported 24, and the exits
# reported 4 of 47, 5 of 9 and 5 of 32. The broker executed every one
# correctly and the account ended flat -- the recording was wrong, which
# corrupts exactly the variance data this exists to collect. close() also
# compares that number against the tracked size to detect a partial fill,
# so a first-piece reading could have cancelled a real remainder.

class _PiecewiseTrade(_Trade):
    """Fills in pieces, like a large market order does."""

    def __init__(self, total, pieces, price=100.0):
        super().__init__(price=price, size=total, fills_after=10**9)
        self._total = float(total)
        self._pieces = list(pieces)
        self._done = 0.0

    def poll(self):
        if not self._pieces:
            return
        self._done += self._pieces.pop(0)
        self.fills = ["fill"] * int(self._done)
        self.orderStatus = _Status(
            self._price, self._done,
            "Filled" if self._done >= self._total else "PreSubmitted",
            remaining=self._total - self._done,
        )


def test_the_first_piece_of_a_fill_is_not_the_fill():
    """A 47-lot order reported 1. The wait must run to completion."""
    a = _adapter(fill_timeout=5.0)
    trade = _PiecewiseTrade(47, [1, 6, 20, 20], price=2850.9)
    price, size = _drive(a, trade, a._await_fill(trade, "entry"))
    assert size == 47.0, f"reported {size} of 47"
    assert price == 2850.9


def test_a_partial_that_never_completes_is_reported_honestly():
    """Not None: that much really did fill, and close() needs the true
    number so it does not sell more than exists."""
    a = _adapter(fill_timeout=0.3)
    trade = _PiecewiseTrade(47, [12])
    price, size = _drive(a, trade, a._await_fill(trade, "entry"))
    assert size == 12.0
    assert price is not None


def test_closing_a_partially_filled_entry_sells_only_what_filled():
    a = _prepared(_adapter(fill_timeout=0.3), _PiecewiseTrade(47, [12]),
                  _Trade(price=1.0, fills_after=1))
    req = OrderRequest(symbol="M2K", direction="BUY", size=47.0, sl=None,
                       tp=None, master_ticket=1)
    res = asyncio.run(a.open(req))
    a.ib._all[0].poll()                      # 12 of 47 fill
    asyncio.run(a.close(res.slave_ticket))
    assert a.ib.placed[1].totalQuantity == 12.0
    assert res.slave_ticket in a.ib.cancelled, "left 35 able to fill later"


# ---------------- a cancel is a request, not an outcome -----------------
#
# Seen live on 2026-11-13. Entry limits timed out, the hub cancelled them,
# and IBKR answered 10148 -- "cannot be cancelled, state: Filled". The hub
# wrote CANCELLED rows and walked away from 1889 UGL and 641 TQQQ. Nothing
# downstream can ever find that volume: reconciliation works from open
# rows, and a cancelled row is not one.

def test_an_entry_that_fills_inside_the_cancel_round_trip_is_kept():
    entry = _FillsOnCancel(price=39.10, size=1889.0)
    a = _prepared(_adapter(fill_timeout=0.1, entry_timeout=0.1), entry)
    req = OrderRequest(symbol="MGC", direction="BUY", size=1889.0, sl=None,
                       tp=None, master_ticket=1)
    res = asyncio.run(a.open(req))

    price, size = asyncio.run(a.abandon_unfilled_entry(res.slave_ticket))

    assert (price, size) == (39.10, 1889.0), (
        "the broker refused the cancel because it had filled; recording a "
        "cancel here is what strands the position")
    assert res.slave_ticket in a._positions, "the position was dropped"


def test_the_protective_legs_survive_a_fill_inside_the_cancel():
    """Order of operations. Tearing the legs down and THEN finding out the
    entry filled leaves a live position with no stop -- worse than either
    outcome the method is choosing between."""
    entry = _FillsOnCancel(price=3988.4, size=1.0)
    a = _prepared(_adapter(fill_timeout=0.1, entry_timeout=0.1),
                  entry, _NeverFills(), _NeverFills())
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=3876.5,
                       tp=4100.0, master_ticket=1)
    res = asyncio.run(a.open(req))

    price, _ = asyncio.run(a.abandon_unfilled_entry(res.slave_ticket))

    assert price == 3988.4
    assert res.sl_order_id not in a.ib.cancelled, "stop cancelled under a live position"
    assert res.tp_order_id not in a.ib.cancelled, "target cancelled under a live position"


def test_a_partial_fill_before_a_real_cancel_is_still_a_position():
    entry = _PartialThenCancelled(price=2784.3, part=10.0, total=21.0)
    a = _prepared(_adapter(fill_timeout=0.1, entry_timeout=0.1), entry)
    req = OrderRequest(symbol="MGC", direction="BUY", size=21.0, sl=None,
                       tp=None, master_ticket=1)
    res = asyncio.run(a.open(req))

    price, size = asyncio.run(a.abandon_unfilled_entry(res.slave_ticket))

    assert (price, size) == (2784.3, 10.0), (
        "10 contracts really did fill; calling that a cancel leaves them "
        "untracked at the broker")


def test_a_cancel_that_really_took_is_still_a_cancel():
    """The fix must not turn every abandon into a kept position."""
    a = _prepared(_adapter(fill_timeout=0.1, entry_timeout=0.1),
                  _NeverFills(), _NeverFills(), _NeverFills())
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=3876.5,
                       tp=4100.0, master_ticket=1)
    res = asyncio.run(a.open(req))

    price, size = asyncio.run(a.abandon_unfilled_entry(res.slave_ticket))

    assert (price, size) == (None, None)
    assert set(a.ib.cancelled) == {res.slave_ticket, res.sl_order_id,
                                   res.tp_order_id}
    assert res.slave_ticket not in a._positions
