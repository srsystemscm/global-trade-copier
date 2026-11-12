"""A limit exit that rests has to be chased, not left.

Found live at 10:02 on 2026-11-12. A US500 signal copied to SPXL, and the
exit went out as a SELL limit at 268.14 while the market was 267.71 -- a
sell priced above the market, which simply sits there. The arithmetic was
right (a SELL prices through at quote * (1 - offset)); the quote was not.
The ETF slave runs on delayed market data, so marketPrice() reported
268.68, about 0.36% stale, and 0.2% "through" a quote that wrong is not
through anything. The entry escaped by luck: a BUY priced off a stale-high
quote is aggressive.

Nothing was stranded -- the order filled later in the session -- but the
exit price was never captured. slave_trades row 47 has a null
slave_close_price, which is precisely the variance data this slave exists
to collect.

The chase re-prices from the PREVIOUS LIMIT rather than from a fresh quote,
because an unfilled sell at 268.14 is itself evidence that the market is
below 268.14, and that is evidence the stale quote does not have.
"""
import asyncio

from app.adapters import create_adapter
from app.slaves import SlaveConfig

MARKET = 267.71          # where SPXL actually was
STALE_LIMIT = 268.14     # where the exit was priced, off a stale quote


class _Order:
    def __init__(self, action="SELL", limit=STALE_LIMIT, order_type="LMT"):
        self.orderId = 35
        self.action = action
        self.orderType = order_type
        self.lmtPrice = limit
        self.totalQuantity = 18.0
        self.tif = "DAY"
        self.outsideRth = True
        self.account = None


class _Status:
    def __init__(self):
        self.status = "Submitted"
        self.filled = 0.0
        self.remaining = 18.0
        self.avgFillPrice = 0.0


class _Contract:
    symbol = "SPXL"
    localSymbol = "SPXL"


class _Exit:
    """Fills only once its limit is on the right side of the market."""

    def __init__(self, action="SELL", limit=STALE_LIMIT, order_type="LMT",
                 market=MARKET, fillable=True):
        self.order = _Order(action, limit, order_type)
        self.orderStatus = _Status()
        self.contract = _Contract()
        self.fills = []
        self.market = market
        self.fillable = fillable

    def poll(self):
        if self.orderStatus.filled or not self.fillable:
            return
        crossed = (self.order.lmtPrice <= self.market
                   if self.order.action == "SELL"
                   else self.order.lmtPrice >= self.market)
        if crossed:
            self.orderStatus.status = "Filled"
            self.orderStatus.filled = self.order.totalQuantity
            self.orderStatus.remaining = 0.0
            self.orderStatus.avgFillPrice = self.market
            self.fills = ["fill"]


class _FakeIB:
    """Records every placement, keeping the same Trade and order id."""

    def __init__(self, trade):
        self.trade = trade
        self.prices = []

    def placeOrder(self, contract, order):
        self.prices.append(round(float(order.lmtPrice), 4))
        return self.trade

    def cancelOrder(self, order):
        raise AssertionError("the chase must not cancel a live exit")

    def trades(self):
        return [self.trade]

    def isConnected(self):
        return True


def _adapter(**cfg):
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002,
            "client_id": 2, "fill_timeout": 0.2}
    base.update(cfg)
    return create_adapter(SlaveConfig(id="ibkr-etf-1", name="etf",
                                      broker_type="ibkr", enabled=True,
                                      paused=False, config=base))


def _chase(adapter, trade, offset=0.002, ticket=32):
    """Run await_exit_fill, advancing the trade the way the reader would."""
    adapter.ib = _FakeIB(trade)
    adapter._exit_trades[ticket] = (trade, offset)

    async def ticker():
        for _ in range(400):
            trade.poll()
            await asyncio.sleep(0.005)

    async def both():
        t = asyncio.create_task(ticker())
        try:
            return await adapter.await_exit_fill(ticket)
        finally:
            t.cancel()

    return asyncio.run(both())


# ---------------- the live failure ---------------------------------------

def test_the_spxl_exit_gets_filled_instead_of_resting():
    """The regression itself: 268.14 against a 267.71 market."""
    a = _adapter()
    trade = _Exit()
    price, size = _chase(a, trade)
    assert price == MARKET, "the exit rested instead of being chased down"
    assert size == 18.0


def test_the_exit_price_reaches_the_caller_so_the_registry_gets_it():
    """The damage the resting order did was a null slave_close_price."""
    a = _adapter()
    price, _ = _chase(a, _Exit())
    assert price is not None


# ---------------- which way, and from what anchor ------------------------

def test_a_sell_is_re_priced_downwards():
    a = _adapter()
    trade = _Exit()
    _chase(a, trade)
    assert a.ib.prices, "nothing was re-priced"
    assert a.ib.prices[0] < STALE_LIMIT


def test_a_buy_is_re_priced_upwards():
    a = _adapter()
    # a BUY exit (closing a short) priced below a market that has run up
    trade = _Exit(action="BUY", limit=267.00, market=267.71)
    price, _ = _chase(a, trade)
    assert price == 267.71
    assert a.ib.prices[0] > 267.00


def test_each_step_anchors_on_the_previous_limit_not_on_a_quote():
    """The whole point: the quote is the thing that was wrong. Steps walk
    one offset at a time from where the order actually is."""
    a = _adapter(exit_chase_steps=3)
    trade = _Exit(fillable=False)          # never crosses, so all steps run
    _chase(a, trade)
    assert len(a.ib.prices) == 3
    first = round(STALE_LIMIT * 0.998, 2)
    second = round(first * 0.998, 2)
    assert a.ib.prices == [first, second, round(second * 0.998, 2)]


def test_the_step_comes_from_the_routes_own_offset():
    a = _adapter(exit_chase_steps=1)
    trade = _Exit(fillable=False)
    _chase(a, trade, offset=0.01)
    assert a.ib.prices == [round(STALE_LIMIT * 0.99, 2)]


def test_a_route_without_an_offset_falls_back_to_the_configured_step():
    a = _adapter(exit_chase_steps=1, exit_chase_pct=0.005)
    trade = _Exit(fillable=False)
    _chase(a, trade, offset=0.0)
    assert a.ib.prices == [round(STALE_LIMIT * 0.995, 2)]


def test_a_step_smaller_than_a_tick_still_moves():
    """A chase that rounds back to where it started is not a chase."""
    a = _adapter(exit_chase_steps=2)
    trade = _Exit(limit=0.50, market=0.40, fillable=False)
    _chase(a, trade, offset=0.000001)
    assert a.ib.prices == [0.49, 0.48]


# ---------------- what it must not do ------------------------------------

def test_a_market_exit_is_never_re_priced():
    """A market order that has not filled is not a pricing problem."""
    a = _adapter()
    trade = _Exit(order_type="MKT", fillable=False)
    price, _ = _chase(a, trade)
    assert price is None
    assert a.ib.prices == [], "re-priced a market order"


def test_an_exit_that_fills_first_time_is_left_alone():
    a = _adapter()
    trade = _Exit(limit=267.00)            # already through the market
    price, _ = _chase(a, trade)
    assert price == MARKET
    assert a.ib.prices == [], "re-priced an order that had already filled"


def test_the_walk_is_bounded():
    """A bad quote should cost a known number of ticks, not an open-ended
    slide down the book."""
    a = _adapter(exit_chase_steps=2)
    trade = _Exit(fillable=False)
    price, _ = _chase(a, trade)
    assert price is None
    assert len(a.ib.prices) == 2


def test_giving_up_leaves_the_order_live():
    """The position is still open at the broker. Cancelling the only thing
    working to close it would turn a slow exit into no exit."""
    a = _adapter(exit_chase_steps=1)
    trade = _Exit(fillable=False)
    _chase(a, trade)                        # _FakeIB.cancelOrder asserts
    assert trade.orderStatus.status == "Submitted"


def test_a_cancelled_exit_stops_the_chase():
    a = _adapter(exit_chase_steps=4)
    trade = _Exit(fillable=False)
    trade.orderStatus.status = "Cancelled"
    _chase(a, trade)
    assert a.ib.prices == []


def test_no_exit_trade_is_not_an_error():
    a = _adapter()
    a.ib = _FakeIB(_Exit())
    assert asyncio.run(a.await_exit_fill(999)) == (None, None)
