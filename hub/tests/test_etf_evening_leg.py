"""Porting a CFD signal to an ETF that fills in the evening session.

Two things make an evening fill possible, and both are needed: IBKR refuses
a market order outside regular hours, and a limit order without outsideRth
simply waits for the open. The ETF proxies here are long-side only, which
is the other thing these pin down.
"""
import asyncio

from app.adapters.base import OrderRequest
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from app.routing import describe_refusal, resolve_routes
from tests.conftest_fakes import FakeBroker

SLAVE = "test-etf"

ETF_ROUTE = {
    "symbol": "ETHUSD", "target": "ETHA", "direction": "BUY",
    "order_type": "limit", "outside_rth": True, "limit_offset_pct": 0.002,
    "sizing": {"mode": "dollar_notional", "notional": 5000, "rounding": "floor"},
}


def _engine(broker, routes=None):
    cfg = {"mode": "autonomous", "atr_period": 14,
           "routes": routes if routes is not None else [ETF_ROUTE],
           "risk_management": {"poll_interval_seconds": 3600}}
    return AutonomousCopyEngine(SLAVE, broker, asyncio.Queue(), cfg)


def _sig(**kw):
    base = dict(ticket=1, symbol="ETHUSD", action="OPEN", direction="BUY",
                lots=10.0, price=2055.84, sl=1893.27, atr=108.0, magic=1001,
                account="44964870")
    base.update(kw)
    return Signal(**base)


# --- long only ------------------------------------------------------------

def test_a_long_signal_routes_to_the_etf():
    (route,) = resolve_routes({"routes": [ETF_ROUTE]}, _sig())
    assert route.target == "ETHA"


def test_a_short_signal_is_refused_rather_than_shorting_the_etf():
    """The dangerous default. These proxies have no inverse twin, so without
    a direction filter a SELL on the master would short a leveraged ETF --
    overnight, where borrow is scarce -- taking the opposite position to the
    one intended."""
    sig = _sig(direction="SELL")
    assert resolve_routes({"routes": [ETF_ROUTE]}, sig) == []
    assert "direction" in describe_refusal({"routes": [ETF_ROUTE]}, sig)


def test_a_route_without_a_direction_still_takes_both():
    """Futures legs short as easily as they go long; the filter is opt-in."""
    futures = {"symbol": "ETHUSD", "target": "MET",
               "sizing": {"mode": "fixed_contracts", "contracts": 1}}
    assert resolve_routes({"routes": [futures]}, _sig(direction="SELL"))


# --- the order that can actually fill -------------------------------------

def test_the_entry_is_a_limit_priced_through_the_quote():
    """Crossing the spread is the point. A limit resting at the bid in a thin
    evening book is a signal that quietly never becomes a position."""
    b = FakeBroker(quote=100.0)
    asyncio.run(_engine(b)._handle_open(_sig()))

    (opened,) = b.opened
    assert opened["order_type"] == "limit"
    assert opened["outside_rth"] is True
    assert opened["limit_price"] == 100.0 * 1.002


def test_a_short_route_prices_the_limit_the_other_way():
    route = {**ETF_ROUTE, "direction": "SELL"}
    b = FakeBroker(quote=100.0)
    asyncio.run(_engine(b, [route])._handle_open(_sig(direction="SELL")))

    (opened,) = b.opened
    assert opened["limit_price"] == 100.0 * 0.998, "a sell limit priced above the quote waits forever"


def test_a_futures_route_is_still_a_market_order():
    """The default has to be unchanged, or every existing route silently
    becomes a limit with no price."""
    futures = {"symbol": "ETHUSD", "target": "MET",
               "sizing": {"mode": "fixed_contracts", "contracts": 1}}
    b = FakeBroker(quote=100.0)
    asyncio.run(_engine(b, [futures])._handle_open(_sig()))

    (opened,) = b.opened
    assert opened["order_type"] == "market"
    assert opened["outside_rth"] is False
    assert opened["limit_price"] is None


def test_shares_are_sized_by_notional_and_floored():
    """$5,000 of a $100 ETF is 50 shares. Flooring keeps the position inside
    the notional rather than a share over it."""
    b = FakeBroker(quote=100.0)
    asyncio.run(_engine(b)._handle_open(_sig()))
    assert b.opened[0]["size"] == 50


def test_a_limit_order_with_no_price_is_refused_not_downgraded():
    """Sending it as a market order instead would be rejected outside
    regular hours anyway -- and filled at any price inside them."""
    import pytest
    from app.adapters.base import AdapterRejectedError
    from app.adapters.ibkr import IBKRAdapter

    a = IBKRAdapter("t", "127.0.0.1", 4002, client_id=9)
    a.ib = object()  # never reached; the guard fires first
    req = OrderRequest(symbol="ETHA", direction="BUY", size=50,
                       order_type="limit", limit_price=None)
    with pytest.raises(AdapterRejectedError) as exc:
        asyncio.run(a.open(req))
    assert "limit price" in str(exc.value)


# --- inverse ETFs: a short view, expressed long ---------------------------

SHORT_ROUTE = {
    "symbol": "ETHUSD", "target": "ETHD", "direction": "SELL",
    "target_direction": "BUY",
    "order_type": "limit", "outside_rth": True, "limit_offset_pct": 0.002,
    "sizing": {"mode": "dollar_notional", "notional": 5000, "rounding": "floor"},
}


def test_a_short_signal_buys_the_inverse_etf():
    """Inverse ETFs are long-only instruments: the short view is expressed
    by BUYING one. Selling ETHD would be a leveraged LONG on ether."""
    b = FakeBroker(quote=100.0)
    asyncio.run(_engine(b, [SHORT_ROUTE])._handle_open(_sig(direction="SELL")))

    (opened,) = b.opened
    assert opened["symbol"] == "ETHD"
    assert opened["direction"] == "BUY", "it shorted an inverse ETF"


def test_the_inverse_legs_stop_sits_below_its_own_entry():
    """The subtle one. The master is short with its stop ABOVE its entry;
    this leg is long, so its stop belongs BELOW its own. Deriving the sign
    from the master's direction instead of the traded one puts a long
    position's stop above its entry -- an order that fills instantly for a
    loss the moment it is accepted."""
    b = FakeBroker(quote=100.0)
    # master short at 2055.84 with its stop above, at 2218.41
    asyncio.run(_engine(b, [SHORT_ROUTE])._handle_open(
        _sig(direction="SELL", price=2055.84, sl=2218.41)))

    (opened,) = b.opened
    assert opened["direction"] == "BUY"
    assert opened["sl"] < 100.0, "a long ETF leg was given a stop above its entry"


def test_the_inverse_legs_limit_is_priced_up_not_down():
    """Priced from the traded side too: this is a buy, so the limit has to
    cross upward even though the master was selling."""
    b = FakeBroker(quote=100.0)
    asyncio.run(_engine(b, [SHORT_ROUTE])._handle_open(_sig(direction="SELL")))
    assert b.opened[0]["limit_price"] == 100.0 * 1.002


def test_the_stored_position_records_the_traded_side():
    """The trailing monitor and the MODIFY follow both read pos["direction"].
    Storing the master's side would have them manage a long position as if
    it were short, moving its stop the wrong way on every pass."""
    from app import position_store

    b = FakeBroker(quote=100.0)
    asyncio.run(_engine(b, [SHORT_ROUTE])._handle_open(_sig(direction="SELL")))

    (_key, pos), = position_store.load_all(SLAVE)
    assert pos["direction"] == "BUY"


def test_a_futures_route_still_shorts_the_contract():
    """target_direction is opt-in; omitting it must leave shorts alone."""
    futures = {"symbol": "ETHUSD", "target": "MET",
               "sizing": {"mode": "fixed_contracts", "contracts": 1}}
    b = FakeBroker(quote=100.0)
    asyncio.run(_engine(b, [futures])._handle_open(_sig(direction="SELL")))

    (opened,) = b.opened
    assert opened["direction"] == "SELL"
    assert opened["sl"] > 100.0, "a short futures leg needs its stop above entry"
