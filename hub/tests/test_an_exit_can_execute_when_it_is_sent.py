"""An exit has to be an order that can trade in the session it is sent in.

Two things decided that, and both read the ENTRY:

    closing_order.outsideRth = pos.get("outside_rth", False)
    if pos.get("order_type") != "limit": return MarketOrder(...)

A position opened inside regular hours carries outside_rth False and
order_type market, so when the master closed it at 19:51 the copier sent
a market order that may not trade outside regular hours, with the flag
that says do not.

The code believed IBKR would refuse it -- "a market order is simply
refused outside regular hours" -- and acted as though a refusal would
surface. IBKR does not refuse. It ACCEPTS and PARKS:

    Warning 399, reqId 2768: SELL 789 ETHT ARCA Warning: Your order will
    not be placed at the exchange until 2026-11-19 09:30:00 US/Eastern.

PreSubmitted, filled 0 of 789. Nothing looked like a failure, so 789
ETHT sat unmanaged overnight under a registry row that already said
CLOSED.

Mirroring the entry is right about the order TYPE and wrong about the
SESSION, and the two failures are not symmetrical: an entry that does
not fill leaves no position, an exit that does not fill leaves one
nobody is managing.
"""
import asyncio

import pytest


class _Order:
    def __init__(self, kind, action, size, price=None):
        self.kind = kind
        self.action = action
        self.totalQuantity = size
        self.lmtPrice = price
        self.tif = None
        self.outsideRth = None
        self.orderId = 1


class _IbModule:
    @staticmethod
    def MarketOrder(action, size):
        return _Order("MKT", action, size)

    @staticmethod
    def LimitOrder(action, size, price):
        return _Order("LMT", action, size, price)


def adapter(quote=8.30, raises=False):
    from app.adapters.ibkr import IBKRAdapter

    a = IBKRAdapter.__new__(IBKRAdapter)
    a.slave_id = "ibkr-etf-1"
    a.exit_chase_pct = 0.002
    a._round_to_tick = lambda p, s: round(p, 4)

    async def _quote(symbol, month_slot=0, contract_id=None):
        if raises:
            raise RuntimeError("no market data line")
        return quote

    a.get_quote = _quote
    return a


def build(a, pos, action="SELL"):
    return asyncio.run(a._closing_order(_IbModule, pos, action))


MARKET_ENTRY = {"size": 789.0, "symbol": "ETHT", "order_type": "market",
                "outside_rth": False}


# --- the order type --------------------------------------------------

def test_an_equity_market_entry_is_closed_with_a_limit():
    """The defect. ETHT was opened in regular hours, so its exit was a
    market order -- the one order type its venue will not execute in the
    session the exit was actually sent in."""
    order = build(adapter(), MARKET_ENTRY)
    assert order.kind == "LMT", (
        "a market order outside regular hours is PARKED until the next "
        "open, not refused, so the position is left unmanaged with "
        "nothing looking like a failure"
    )


def test_a_future_is_still_closed_at_market():
    """THE LIMIT OF THE FIX, and the first version of it went past this.

    A future trades nearly round the clock and IBKR accepts a market
    order in its overnight session, so there is nothing to repair here
    beyond the outsideRth flag -- and real cost in pretending otherwise.
    await_exit_fill only re-prices a LMT, and on its own cadence, so a
    limit that misses a fast overnight move exits late and worse than a
    market order would have. For an equity outside regular hours a market
    order does not exit at all. That asymmetry is the venue's.
    """
    order = build(adapter(quote=3988.4),
                  {**MARKET_ENTRY, "symbol": "MGC", "size": 1.0})
    assert order.kind == "MKT"


def test_the_venue_decides_it_and_not_the_entry():
    """A future opened with a LIMIT still exits at market, so the branch
    is reading the instrument rather than mirroring the entry again."""
    order = build(adapter(quote=3988.4),
                  {**MARKET_ENTRY, "symbol": "MGC", "size": 1.0,
                   "order_type": "limit", "limit_offset_pct": 0.002})
    assert order.kind == "MKT"


def test_the_limit_is_priced_through_the_quote_to_get_out():
    """Through, not at: an exit priced at the quote waits for the market
    to come back to it, which is the opposite of exiting."""
    sell = build(adapter(quote=8.30), MARKET_ENTRY, action="SELL")
    assert sell.lmtPrice < 8.30

    buy = build(adapter(quote=8.30), {**MARKET_ENTRY, "direction": "SELL"}, action="BUY")
    assert buy.lmtPrice > 8.30


def test_the_entry_s_own_offset_wins_where_it_had_one():
    a = adapter(quote=100.0)
    order = build(a, {**MARKET_ENTRY, "limit_offset_pct": 0.01}, action="SELL")
    assert order.lmtPrice == pytest.approx(99.0)


def test_otherwise_it_uses_the_chase_s_step():
    """So the first price and the re-prices agree about what "through"
    means -- await_exit_fill falls back to exit_chase_pct the same way."""
    a = adapter(quote=100.0)
    a.exit_chase_pct = 0.002
    order = build(a, MARKET_ENTRY, action="SELL")
    assert order.lmtPrice == pytest.approx(99.8)


def test_no_quote_on_an_equity_still_falls_back_to_a_market_order():
    """It at least reaches the broker. This is now the one path that can
    still strand a position, and it logs that it took it."""
    order = build(adapter(raises=True), MARKET_ENTRY)
    assert order.kind == "MKT"


# --- the session -----------------------------------------------------

def test_the_exit_is_allowed_outside_regular_hours():
    """Read off close(), because the flag is set there rather than in
    the builder. The entry's own flag must not decide it."""
    import inspect

    from app.adapters import ibkr

    src = inspect.getsource(ibkr.IBKRAdapter.close)
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#"))
    assert "closing_order.outsideRth = True" in code
    assert 'outsideRth = pos.get("outside_rth"' not in code, (
        "the exit is taking its session from the entry again -- a position "
        "opened in regular hours cannot then be closed outside them"
    )


def test_the_entry_still_takes_its_own_session():
    """The guard against over-correcting. An entry that cannot fill is
    better off expiring than resting into a session nobody chose, and
    that decision belongs to the route."""
    import inspect

    from app.adapters import ibkr

    src = inspect.getsource(ibkr.IBKRAdapter)
    assert "entry.outsideRth = order.outside_rth" in src
