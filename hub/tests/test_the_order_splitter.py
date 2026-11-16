"""One entry, several orders, ONE position.

Chris: "something that sits in the signal like (when selected on) will
split the order into X pieces over X seconds/min", and on why it is not
N positions: "one instrument easier to track and roll".

That sentence is the whole specification, and it is the hard part. N
positions would be easy -- N orders, N registry rows, done. One position
means the fill is a sum across slices, the cancel has to reach every
slice, the close has to settle every slice, and the protective legs have
to cover what is actually working at each moment rather than the size
the position is heading for. A stop for 35 while 9 is working is a
26-lot short waiting for a touch, and it would be a position the copier
opened entirely by itself.

Partly motivated by a real partial: MGC on H1 filled 2 of 35 on
2026-11-16, in one print, into a book that could not take it.
"""
import asyncio

import pytest

from app.adapters.base import EntryNeverFilled, OrderRequest
from app.adapters.ibkr import IBKRAdapter
from app.models import Signal
from app.order_split import resolve_split, split_sizes
from app.routing import resolve_routes


# =================================================== the arithmetic =====

class TestSplitSizes:
    def test_the_slices_sum_to_exactly_the_whole(self):
        """The size was risk-sized. Slices that add up to a hair under it
        put on less risk than was decided, and nothing downstream would
        ever notice."""
        for total, n in [(35, 4), (10, 3), (7, 7), (100, 6), (0.13, 4), (1.0, 2)]:
            sizes = split_sizes(total, n)
            assert sum(sizes) == pytest.approx(total), (
                f"{total} split {n} ways summed to {sum(sizes)}: {sizes}"
            )

    def test_contracts_split_into_whole_contracts(self):
        """IBKR refuses 8.75 of a contract outright, so a split that
        produces one is not a smaller order -- it is no order."""
        sizes = split_sizes(35, 4)
        assert sizes == [9.0, 9.0, 9.0, 8.0], sizes
        assert all(float(x).is_integer() for x in sizes)

    def test_the_larger_slices_go_first(self):
        """The window can be cut short -- the master closes, the session
        ends, the hub restarts. Front-loading means more of the intended
        size is already working when that happens."""
        sizes = split_sizes(35, 4)
        assert sizes == sorted(sizes, reverse=True), sizes

    def test_lots_split_in_hundredths(self):
        """An MT account trades in hundredths of a lot. 0.13 in four is
        0.04/0.03/0.03/0.03, not 0.0325 four times."""
        sizes = split_sizes(0.13, 4)
        assert sum(sizes) == pytest.approx(0.13)
        assert sizes == [0.04, 0.03, 0.03, 0.03], sizes

    def test_a_total_too_small_to_go_round_uses_fewer_slices(self):
        """3 contracts asked to be split five ways is three orders of one.
        A zero-quantity order is a rejection, and splitting is not worth a
        rejection."""
        sizes = split_sizes(3, 5)
        assert sizes == [1.0, 1.0, 1.0], sizes
        assert 0 not in sizes

    def test_one_slice_or_none_is_the_whole_order(self):
        assert split_sizes(35, 1) == [35.0]
        assert split_sizes(35, 0) == [35.0]


class TestResolveSplit:
    def test_off_by_default(self):
        """Every slave behaves exactly as it did before this existed."""
        assert resolve_split(None, None, 0, 0.0) == (0, 0.0)

    def test_a_route_overrides_the_slave(self):
        assert resolve_split(4, 30.0, 2, 300.0) == (4, 30.0)

    def test_the_slave_is_the_fallback(self):
        assert resolve_split(None, None, 3, 60.0) == (3, 60.0)

    def test_slices_with_no_interval_is_off(self):
        """Slices with no time between them is one order sent in pieces:
        commission per piece and nothing gained. Far likelier to be an
        unfinished config than an intention."""
        assert resolve_split(4, 0.0, 0, 0.0) == (0, 0.0)
        assert resolve_split(4, None, 0, 0.0) == (0, 0.0)

    def test_one_slice_is_off(self):
        assert resolve_split(1, 60.0, 0, 0.0) == (0, 0.0)


# =================================================== the config =========

def _signal(magic=1001):
    return Signal(ticket=1, symbol="XAUUSD", action="OPEN", direction="BUY",
                  lots=1.0, price=3846.0, sl=3800.0, atr=1.0, magic=magic,
                  account="44964870")


class TestRouteConfig:
    def test_a_route_can_ask_for_a_split(self):
        routes = resolve_routes(
            {"routes": [{"magic": 1001, "symbol": "XAUUSD", "target": "MGC",
                         "split_slices": 4, "split_interval": 30}]},
            _signal(),
        )
        assert routes, "the route did not resolve at all"
        assert routes[0].split_slices == 4
        assert routes[0].split_interval == 30.0

    def test_a_route_that_says_nothing_asks_for_nothing(self):
        """None, not 0: None means "fall back to the slave", and 0 would
        mean "off" -- which would stop a slave-level default reaching any
        route that had not restated it."""
        routes = resolve_routes(
            {"routes": [{"magic": 1001, "symbol": "XAUUSD", "target": "MGC"}]},
            _signal(),
        )
        assert routes[0].split_slices is None
        assert routes[0].split_interval is None


# =================================================== the adapter =======

class _Status:
    def __init__(self, filled=0.0, avg=0.0, remaining=0.0):
        self.filled = filled
        self.avgFillPrice = avg
        self.remaining = remaining
        self.status = "Submitted"


class _Order:
    def __init__(self, order_id, qty):
        self.orderId = order_id
        self.totalQuantity = qty
        self.orderType = "MKT"
        self.action = "BUY"
        self.lmtPrice = 0.0


class _Trade:
    def __init__(self, order_id, qty, filled=0.0, avg=0.0, remaining=None):
        self.order = _Order(order_id, qty)
        self.orderStatus = _Status(
            filled, avg, qty if remaining is None else remaining)
        self.fills = [object()] if filled else []
        self.contract = object()
        self.log = []

    def fill(self, size, price):
        self.orderStatus.filled = size
        self.orderStatus.avgFillPrice = price
        self.orderStatus.remaining = self.order.totalQuantity - size
        self.fills = [object()]
        return self


class _IbMod:
    """Just enough of ib_async to build orders."""
    @staticmethod
    def MarketOrder(action, qty):
        class O:
            pass
        o = O()
        o.action, o.totalQuantity, o.orderType = action, qty, "MKT"
        return o

    @staticmethod
    def LimitOrder(action, qty, price):
        o = _IbMod.MarketOrder(action, qty)
        o.orderType, o.lmtPrice = "LMT", price
        return o

    @staticmethod
    def StopOrder(action, qty, price):
        o = _IbMod.MarketOrder(action, qty)
        o.orderType, o.auxPrice = "STP", price
        return o


def _adapter(**kw):
    a = object.__new__(IBKRAdapter)
    a.ib = object()
    a.slave_id = "ibkr-paper-1"
    a.account = "DUR314653"
    a.entry_tif = "DAY"
    a.bracket_tif = "GTC"
    a.bracket_check_delay = 0.0
    a.fill_timeout = 0.2
    a.entry_timeout = 0.2
    a.entry_chase_steps = kw.pop("entry_chase_steps", 0)
    a.entry_chase_interval = 300.0
    a.entry_chase_max_pct = 0.01
    a.entry_split_slices = kw.pop("entry_split_slices", 0)
    a.entry_split_interval = kw.pop("entry_split_interval", 0.0)
    a.entry_good_till = None
    a._contracts = {}
    a._positions = {}
    a._entry_trades = {}
    a._entry_slices = {}
    a._split_tasks = {}
    a._split_windows = {}
    a._entry_waits = {}
    a._exit_trades = {}

    a.placed = []
    a._next_id = [7000]

    def _place(contract, order):
        a._next_id[0] += 1
        qty = float(getattr(order, "totalQuantity", 0.0))
        trade = _Trade(a._next_id[0], qty)
        a.placed.append((getattr(order, "orderType", "?"), getattr(order, "action", "?"), qty, trade))
        return trade

    a._place = _place
    a._require_ib = lambda: _IbMod
    a._require_connected = lambda: a.ib
    a._round_to_tick = lambda v, _sym=None: v
    a._protective = lambda order, outside=False: order
    a._cancel_tracked_order = lambda *_a, **_k: None
    a._stamp_entry_timing = lambda *_a, **_k: None

    async def _contract_for_request(symbol, month_slot=0, contract_id=None):
        return object()

    a._contract_for_request = _contract_for_request
    a.ib = type("IB", (), {"trades": staticmethod(lambda: [])})()
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _order(**kw):
    base = dict(symbol="MGC", direction="BUY", size=35.0, sl=3800.0,
                order_ref="ref-1", order_type="market")
    base.update(kw)
    return OrderRequest(**base)


def _entries(adapter):
    """Only the entry orders, not the protective legs."""
    return [p for p in adapter.placed if p[1] == "BUY"]


# ---------------------------------------------------- placement --------

def test_an_unsplit_entry_is_one_order_for_the_whole_size():
    """The default path. Nothing changes for a slave that has not asked."""
    a = _adapter()
    asyncio.run(a.open(_order()))
    assert len(_entries(a)) == 1
    assert _entries(a)[0][2] == 35.0


def test_the_first_slice_goes_out_immediately_and_the_rest_do_not():
    """open() is called from the consume loop. Waiting out the window
    there would queue every later signal for this slave behind it -- the
    MODIFY and the CLOSE of this very ticket included."""
    a = _adapter()
    asyncio.run(a.open(_order(split_slices=4, split_interval=30.0)))
    assert len(_entries(a)) == 1, "open() placed more than the first slice"
    assert _entries(a)[0][2] == 9.0, "the first slice is not 9 of 35"


def test_every_slice_lands_on_one_position():
    """THE SPECIFICATION. Four orders, one slave_ticket, one row to
    track, one thing to roll."""
    async def go():
        a = _adapter()
        result = await a.open(_order(split_slices=4, split_interval=0.01))
        await asyncio.sleep(0.1)
        return a, result

    a, result = asyncio.run(go())
    assert len(_entries(a)) == 4, f"placed {len(_entries(a))} entry orders"
    assert list(a._positions) == [result.slave_ticket], (
        f"the slices made {len(a._positions)} positions, not one"
    )
    assert len(a._entry_slices[result.slave_ticket]) == 4


def test_the_slices_add_up_to_the_size_that_was_sized():
    async def go():
        a = _adapter()
        await a.open(_order(split_slices=4, split_interval=0.01))
        await asyncio.sleep(0.1)
        return a

    a = asyncio.run(go())
    assert sum(p[2] for p in _entries(a)) == 35.0


def test_the_position_size_grows_as_the_slices_go_out():
    """close() sizes the exit from pos["size"] and modify() sizes the legs
    from it. A close against the first slice alone would leave the rest
    held, with the master already out."""
    async def go():
        a = _adapter()
        result = await a.open(_order(split_slices=4, split_interval=0.03))
        first = a._positions[result.slave_ticket]["size"]
        await asyncio.sleep(0.05)
        middle = a._positions[result.slave_ticket]["size"]
        await asyncio.sleep(0.12)
        return first, middle, a._positions[result.slave_ticket]

    first, middle, pos = asyncio.run(go())
    assert first == 9.0
    assert middle > first, "the position did not grow as slices were placed"
    assert pos["size"] == 35.0
    assert pos["intended_size"] == 35.0


# ---------------------------------------------------- the legs ---------

def test_the_protective_legs_cover_only_what_is_working():
    """A stop for the intended 35 while 9 is working is a 26-lot short
    waiting for a touch: a position the copier would open by itself."""
    a = _adapter()
    asyncio.run(a.open(_order(split_slices=4, split_interval=30.0)))
    stops = [p for p in a.placed if p[1] == "SELL"]
    assert stops, "no protective leg was placed at all"
    assert stops[0][2] == 9.0, (
        f"the stop was placed for {stops[0][2]} while 9.0 was working"
    )


def test_the_legs_are_regrown_to_match_the_grown_position():
    """Unprotected is worse. Each slice re-places the legs at the new
    size, which is the cancel-and-replace modify() already does."""
    async def go():
        a = _adapter()
        result = await a.open(_order(split_slices=4, split_interval=0.01))
        await asyncio.sleep(0.1)
        return a, result

    a, result = asyncio.run(go())
    stops = [p for p in a.placed if p[1] == "SELL"]
    assert stops[-1][2] == 35.0, (
        f"the last stop covers {stops[-1][2]} of a 35.0 position"
    )
    assert a._positions[result.slave_ticket]["sl_order_id"] == stops[-1][3].order.orderId


def test_a_route_without_broker_brackets_gets_no_legs_at_any_size():
    """ETF legs rest no stop or target -- those do not execute in
    extended hours. The regrow must not quietly introduce one."""
    async def go():
        a = _adapter()
        await a.open(_order(split_slices=3, split_interval=0.01,
                            broker_brackets=False))
        await asyncio.sleep(0.08)
        return a

    a = asyncio.run(go())
    assert not [p for p in a.placed if p[1] == "SELL"]


# ---------------------------------------------------- the fill ---------

def test_the_fill_is_the_sum_across_slices():
    """One position has one entry price and one size. Reading the first
    slice would report a third of the trade."""
    a = _adapter()
    slices = [
        _Trade(1, 9).fill(9, 100.0),
        _Trade(2, 9).fill(9, 102.0),
        _Trade(3, 9).fill(9, 104.0),
        _Trade(4, 8).fill(8, 106.0),
    ]
    price, size = a._filled_across(slices)
    assert size == 35.0
    # Size-weighted, not a plain mean of the four prices.
    assert price == pytest.approx((9 * 100 + 9 * 102 + 9 * 104 + 8 * 106) / 35)


def test_nothing_filled_reports_nothing():
    a = _adapter()
    price, size = a._filled_across([_Trade(1, 9), _Trade(2, 9)])
    assert price is None and size == 0.0


def test_a_slice_that_filled_is_a_position_even_if_the_first_did_not():
    """The case that would abandon a real position: slice one rests
    unfilled while slices two and three fill."""
    a = _adapter()
    slices = [_Trade(1, 9), _Trade(2, 9).fill(9, 102.0), _Trade(3, 9)]
    price, size = a._filled_across(slices)
    assert size == 9.0 and price == 102.0


# ---------------------------------------------------- close ------------

def test_a_close_mid_window_calls_off_the_remaining_slices():
    """The master is out. A slice landing after that is a position the
    copier opened on its own, for a trade that has already finished."""
    async def go():
        a = _adapter()
        result = await a.open(_order(split_slices=4, split_interval=0.05))
        ticket = result.slave_ticket
        # One slice is working and unfilled, so close() takes the
        # never-filled path -- which must still stop the schedule.
        with pytest.raises(EntryNeverFilled):
            await a.close(ticket)
        placed_at_close = len(_entries(a))
        await asyncio.sleep(0.2)
        return placed_at_close, len(_entries(a)), a._split_pending(ticket)

    at_close, after, pending = asyncio.run(go())
    assert after == at_close, (
        f"{after - at_close} more slice(s) went out after the close"
    )
    assert not pending


def test_abandoning_an_entry_cancels_every_slice():
    """One position, several orders. A slice left behind is a live order
    under a row the hub has just marked CANCELLED -- which is where the
    stray MGC, M2K and MBT contracts came from."""
    async def go():
        a = _adapter()
        cancelled = []
        a._cancel_tracked_order = lambda oid: cancelled.append(oid)
        result = await a.open(_order(split_slices=3, split_interval=0.01))
        await asyncio.sleep(0.06)
        ticket = result.slave_ticket
        slice_ids = [t.order.orderId for t in a._entry_slices[ticket]]
        price, size = await a.abandon_unfilled_entry(ticket)
        return slice_ids, cancelled, price, size, a._positions

    slice_ids, cancelled, price, size, positions = asyncio.run(go())
    assert len(slice_ids) == 3
    for oid in slice_ids:
        assert oid in cancelled, f"slice {oid} was never cancelled"
    assert price is None and size is None
    assert not positions, "the position was left behind"


def test_abandoning_keeps_a_position_that_filled_on_any_slice():
    """A fill on the third slice is as real as a fill on the first, and
    cancelling its protective legs would leave it naked."""
    async def go():
        a = _adapter()
        result = await a.open(_order(split_slices=3, split_interval=0.01))
        await asyncio.sleep(0.06)
        ticket = result.slave_ticket
        a._entry_slices[ticket][2].fill(9, 3846.0)
        return await a.abandon_unfilled_entry(ticket), a, ticket

    (price, size), a, ticket = asyncio.run(go())
    assert price == 3846.0 and size == 9.0
    assert ticket in a._positions, "a filled position was abandoned"
    assert ticket in a._entry_slices, "the slices were dropped from a live position"


# ---------------------------------------------------- interactions -----

def test_the_entry_chase_is_not_run_for_a_split_order():
    """Both re-price a resting entry and neither knows about the other.
    Chasing slice one while slice four is still to be placed walks the
    limit away from the price the rest were planned at. Logged rather
    than left as a config key that looks live and is not."""
    a = _adapter(entry_chase_steps=3)
    sizes = a._slices_for(_order(split_slices=4, split_interval=0.01))
    assert len(sizes) == 4  # the split still happens
    chased = []
    a._chase_entry = lambda *_a, **_k: chased.append(1)

    async def go():
        result = await a.open(_order(split_slices=4, split_interval=0.01))
        await a.await_entry_fill(result.slave_ticket)

    asyncio.run(go())
    assert not chased, "the entry chase ran against a split order"


def test_a_slave_level_default_applies_without_a_route_saying_so():
    a = _adapter(entry_split_slices=5, entry_split_interval=10.0)
    sizes = a._slices_for(_order())
    assert len(sizes) == 5
    assert sum(sizes) == 35.0


def test_a_route_can_turn_the_slave_default_off():
    """0 slices on the route means off, where None would mean "ask the
    slave". A slave-wide split that one route must not have needs a way
    to say so."""
    a = _adapter(entry_split_slices=5, entry_split_interval=10.0)
    assert a._slices_for(_order(split_slices=0)) == [35.0]


def test_a_regrown_leg_that_was_rejected_is_reported(caplog):
    """A regrow is a cancel and a re-place, so it can fail the same way
    the opening brackets can -- IBKR rejects asynchronously. The
    difference is that this happens with no signal in flight, on a
    position that WAS protected a moment earlier, so nothing else would
    be looking. Seen live on the opening legs: both rejected on Error 110
    while the entry filled."""
    import logging

    async def go():
        a = _adapter()
        a.bracket_check_delay = 0.0

        class _Rejected:
            def __init__(self, order_id):
                self.order = _Order(order_id, 1)
                self.orderStatus = _Status()
                self.orderStatus.status = "Cancelled"
                self.log = [type("L", (), {"message": "Error 110 price"})()]

        # Every leg comes back cancelled, which is what a rejection looks
        # like a moment after placeOrder returned happily.
        a.ib = type("IB", (), {
            "trades": staticmethod(
                lambda: [_Rejected(oid) for oid in range(7000, 7040)])
        })()
        await a.open(_order(split_slices=3, split_interval=0.01))
        await asyncio.sleep(0.08)

    with caplog.at_level(logging.ERROR):
        asyncio.run(go())
    assert "UNPROTECTED" in caplog.text, (
        "a rejected protective leg on a grown position was never reported"
    )


# =================================================== what it reports ====

class TestSplitSummary:
    """The splitter changes WHAT ARRIVES AT THE BROKER for a signal --
    three orders over ten seconds instead of one. A setting that does
    that while appearing on no screen is how a surprise gets built into a
    money-moving system."""

    @staticmethod
    def _summary(config):
        from app.api_routes import _split_summary
        return _split_summary(config)

    def test_nothing_configured_says_nothing(self):
        assert self._summary({"routes": [{"target": "MGC"}]}) is None
        assert self._summary({}) is None

    def test_a_preference_list_is_named_by_its_first_candidate(self):
        """target may be a preference list -- ["GC", "MGC"] is ONE
        position, first tradeable wins. Printing the list reads like two
        routes."""
        assert self._summary({"routes": [
            {"symbol": "XAUUSD", "target": ["GC", "MGC"],
             "split_slices": 3, "split_interval": 5},
        ]}) == "GC: 3 over 5s"

    def test_a_slave_wide_split_is_reported_once(self):
        assert self._summary({
            "entry_split_slices": 4, "entry_split_interval": 30,
        }) == "entries split into 4 over 30s"

    def test_a_route_matching_the_slave_default_is_not_repeated(self):
        """Saying it twice makes a reader look for a difference."""
        assert self._summary({
            "entry_split_slices": 4, "entry_split_interval": 30,
            "routes": [{"target": "MGC", "split_slices": 4,
                        "split_interval": 30}],
        }) == "entries split into 4 over 30s"

    def test_a_route_that_opted_out_is_not_reported_as_splitting(self):
        assert self._summary({
            "entry_split_slices": 4, "entry_split_interval": 30,
            "routes": [{"target": "UGL", "split_slices": 0}],
        }) == "entries split into 4 over 30s"
