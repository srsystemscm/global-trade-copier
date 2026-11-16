"""A resting entry follows the quote instead of waiting on a stale one.

The ETF legs price a limit through a quote and then never touch it. At
20:00-04:00 ET IBKR does not quote UGL on this line at all, so the limit
is anchored to the PREVIOUS CLOSE -- 39.16 on 2026-11-15, giving 39.55
-- and from then on it has exactly two outcomes: it fills instantly if
the session opens below it, or it never fills. Neither tracks the
signal, and the second is what Chris saw: "orders reading as
cancelled/no fill".

Chris: "cross reference we're using the right type of limit, price can
follow the quote in order to fill."

RE-PRICED FROM A FRESH QUOTE, which is the opposite of the exit chase
and deliberately so. An unfilled SELL exit is evidence the market is
below it, so that walk steps down from its own last price. An unfilled
ENTRY priced through a stale close is evidence only that the close was
stale -- the market may be either side of it. Stepping blindly from the
old limit would walk away from the market as readily as toward it.

THREE PROPERTIES THAT MATTER MORE THAN THE CHASE ITSELF:

  a slice with no usable quote spends no step
      overnight that is every slice, and burning the budget against a
      shut line leaves nothing for 04:00 when a quote appears

  it only ever moves toward the market
      a "better" price for us is never taken, because the order would
      have filled if the market were there

  it never drifts past entry_chase_max_pct of the original
      the stop is already fixed in absolute terms, so filling higher
      than sizing assumed means risking MORE than the position was
      sized for. The exit chase needs no such bound; this one does.
"""
import asyncio

import pytest

from app.adapters.base import AdapterError
from app.slaves import SlaveConfig


def _adapter(**cfg):
    from app.adapters import create_adapter
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002, "client_id": 1}
    base.update(cfg)
    return create_adapter(SlaveConfig(id="i", name="i", broker_type="ibkr",
                                      enabled=True, paused=False, config=base))


class _Status:
    def __init__(self):
        self.status = "Submitted"
        self.filled = 0.0
        self.remaining = 100.0
        self.avgFillPrice = 0.0


class _Order:
    def __init__(self, action="BUY", lmt=39.55):
        self.orderId = 501
        self.action = action
        self.lmtPrice = lmt
        self.orderType = "LMT"
        self._gtc_offset = 0.01


class _Contract:
    symbol = "UGL"
    localSymbol = "UGL"


class _Trade:
    def __init__(self, action="BUY", lmt=39.55):
        self.order = _Order(action, lmt)
        self.orderStatus = _Status()
        self.contract = _Contract()


def _prepare(adapter, trade, quotes):
    """Drive the chase with a scripted sequence of quote outcomes.

    An AdapterError stands for "the line is not quoting", which is the
    overnight case.
    """
    adapter.fill_timeout = 0.0
    adapter.entry_chase_interval = 0.0
    adapter._entry_trades[501] = trade
    adapter.placed = []
    seq = list(quotes)

    async def _quote(symbol, **kw):
        if not seq:
            raise AdapterError("no more scripted quotes")
        nxt = seq.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def _place(contract, order):
        adapter.placed.append(float(order.lmtPrice))
        return trade

    adapter.get_quote = _quote
    adapter._place = _place
    adapter._tick_size = lambda s: 0.01
    adapter._round_to_tick = lambda p, s: round(float(p), 2)
    return adapter


def _run(adapter, trade, timeout=1.0):
    return asyncio.run(adapter._chase_entry(trade, timeout))


# ------------------------------------------------- it is off by default -

def test_the_chase_is_off_unless_configured():
    """Every slave must behave exactly as it does today until this is
    turned on. A resting entry that starts moving on its own is not a
    change anybody asked for implicitly."""
    assert _adapter().entry_chase_steps == 0


def test_await_entry_fill_only_chases_when_enabled():
    import inspect

    from app.adapters.ibkr import IBKRAdapter
    src = inspect.getsource(IBKRAdapter.await_entry_fill)
    assert "if is_limit and self.entry_chase_steps > 0:" in src
    assert "_chase_entry" in src


# ------------------------------------------------- the overnight case ---

def test_a_shut_line_spends_no_steps():
    """THE CASE THIS EXISTS FOR. Between 20:00 and 04:00 ET every attempt
    finds no quote. If those burned the budget, there would be nothing
    left at 04:00 when the market finally opens -- the order would sit at
    its stale price through the only session that could fill it."""
    a = _prepare(_adapter(entry_chase_steps=2), _Trade(),
                 [AdapterError("no market"), AdapterError("no market"),
                  AdapterError("no market"), AdapterError("no market")])
    trade = a._entry_trades[501]
    _run(a, trade, timeout=0.3)
    assert a.placed == [], "a shut line re-priced the order anyway"
    assert trade.order.lmtPrice == 39.55, "the limit moved with no quote"


def test_a_quote_arriving_later_still_gets_its_steps():
    """The shape of a real overnight: nothing, nothing, then the open."""
    a = _prepare(_adapter(entry_chase_steps=2), _Trade(),
                 [AdapterError("shut"), AdapterError("shut"), 40.00, 40.20])
    trade = a._entry_trades[501]
    _run(a, trade, timeout=1.0)
    assert a.placed, "the quote arrived and nothing was re-priced"
    # 40.00 x 1.01 is 40.40, but the ceiling is 1% above the original
    # 39.55 -- so it moves as far as it is allowed and no further. The
    # clamp is the point: the stop was fixed when the position was sized.
    assert a.placed[0] == pytest.approx(39.95)
    assert a.placed[0] < 40.40


# ------------------------------------------------- direction and bounds -

def test_it_moves_toward_the_market_only():
    """A quote BELOW our buy limit means the order would already have
    filled if the market were really there. Moving down on it would just
    walk away from a fill."""
    a = _prepare(_adapter(entry_chase_steps=2), _Trade(lmt=39.55),
                 [38.00, 38.00, 38.00])
    trade = a._entry_trades[501]
    _run(a, trade, timeout=1.0)
    assert a.placed == [], "the buy limit was walked DOWN, away from the market"


def test_it_never_drifts_past_the_ceiling():
    """The stop is fixed in absolute terms. Filling higher than sizing
    assumed means risking more than the position was sized for, so the
    walk stops at entry_chase_max_pct of where it started."""
    a = _prepare(_adapter(entry_chase_steps=3, entry_chase_max_pct=0.01),
                 _Trade(lmt=39.55), [45.0, 45.0, 45.0, 45.0])
    trade = a._entry_trades[501]
    _run(a, trade, timeout=1.0)
    # One tick of slack: the ceiling is rounded to a tradeable increment,
    # so it can land a fraction either side of an arbitrary percentage.
    # A price that is not on a tick is one IBKR rejects -- 39.9455 on a
    # penny stock -- which is worse than being 0.45c over a soft bound.
    ceiling = 39.55 * 1.01
    assert a.placed, "nothing was re-priced at all"
    for price in a.placed:
        assert price <= ceiling + 0.01, f"{price} is past the {ceiling:.2f} ceiling"
        assert round(price, 2) == price, f"{price} is not on a penny tick"


def test_a_sell_entry_walks_the_other_way():
    a = _prepare(_adapter(entry_chase_steps=2), _Trade(action="SELL", lmt=39.55),
                 [39.00, 39.00, 39.00])
    trade = a._entry_trades[501]
    _run(a, trade, timeout=1.0)
    assert a.placed, "a sell entry never re-priced"
    assert a.placed[0] < 39.55, "a sell entry walked UP, away from the market"
    # 39.00 x 0.99 is 38.61, below the 1% floor of 39.15. Clamped, like
    # the buy side, and in the direction that protects the sized risk.
    # Compared on the tick, because the floor is rounded to a tradeable
    # increment before it is applied.
    assert a.placed[0] == pytest.approx(39.15, abs=0.01)
    assert round(a.placed[0], 2) == a.placed[0], "not on a penny tick"


def test_the_step_budget_is_respected():
    a = _prepare(_adapter(entry_chase_steps=2, entry_chase_max_pct=0.50),
                 _Trade(), [40.0, 41.0, 42.0, 43.0, 44.0])
    trade = a._entry_trades[501]
    _run(a, trade, timeout=1.0)
    assert len(a.placed) <= 2


# ------------------------------------------------- it stops on terminal -

def test_a_cancelled_order_is_not_re_priced():
    """Re-placing a cancelled order would resurrect it -- a position the
    engine has already decided against."""
    a = _prepare(_adapter(entry_chase_steps=3), _Trade(), [40.0, 40.0, 40.0])
    trade = a._entry_trades[501]
    trade.orderStatus.status = "Cancelled"
    _run(a, trade, timeout=1.0)
    assert a.placed == []


def test_a_fill_during_the_wait_returns_it():
    a = _prepare(_adapter(entry_chase_steps=3), _Trade(), [40.0])
    trade = a._entry_trades[501]
    trade.orderStatus.filled = 100.0
    trade.orderStatus.remaining = 0.0
    trade.orderStatus.avgFillPrice = 39.50
    price, size = _run(a, trade, timeout=1.0)
    assert price == pytest.approx(39.50) and size == 100.0
    assert a.placed == [], "it re-priced an order that had already filled"


# ------------------------------------------------- the offset survives --

def test_the_route_offset_reaches_the_chase():
    """The chase recomputes `quote x (1 +/- offset)` long after open()
    has returned and the route is gone, so the offset has to travel on
    the order itself."""
    import inspect

    from app.adapters.ibkr import IBKRAdapter
    assert "_gtc_offset" in inspect.getsource(IBKRAdapter.open)
    assert "_gtc_offset" in inspect.getsource(IBKRAdapter._chase_entry)
