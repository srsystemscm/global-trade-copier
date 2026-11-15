"""An ETF entry has to be able to outlive the session it was placed in.

Asked for on 2026-11-15, after a test that was meant to open ETF legs
from a CFD signal at 18:00 ET and did not. Two things were baked into
the adapter rather than the route, and BOTH had to be, because either
alone changes nothing:

  entry_tif = "DAY"      a DAY order expires at that session's close, so
                         one placed at 18:00 ET can never reach Monday's
                         pre-market
  entry_timeout = 900    this engine cancels an unfilled limit after 15
                         minutes and pulls its brackets with it

So GTC alone would still have been cancelled by us a quarter of an hour
in, and a long timeout alone would have left the engine watching an
order the broker had already expired. They travel together.

`order_type`, `outside_rth` and `limit_offset_pct` were already
per-route. These two finish the set.
"""
import pytest


def _sig(symbol):
    return Signal(ticket=1, symbol=symbol, action="OPEN", direction="BUY",
                  lots=1.0, price=100.0, sl=98.0, magic=1001, account="44964870")

from app.adapters.base import OrderRequest
from app.models import Signal
from app.routing import Route, resolve_routes


def test_a_route_carries_its_own_time_in_force():
    routes = resolve_routes(
        {"routes": [{"symbol": "XAUUSD", "target": "UGL",
                     "order_type": "limit", "outside_rth": True,
                     "tif": "GTC", "entry_timeout": 57600}]},
        _sig("XAUUSD"),
    )
    assert routes, "the route did not resolve at all"
    assert routes[0].tif == "GTC"
    assert routes[0].entry_timeout == 57600


def test_a_lowercase_tif_is_accepted_as_written():
    """Config is typed by a person. "gtc" meaning something different
    from "GTC" is a trap with no upside."""
    routes = resolve_routes(
        {"routes": [{"symbol": "XAUUSD", "target": "UGL", "tif": "gtc"}]},
        _sig("XAUUSD"),
    )
    assert routes[0].tif == "GTC"


def test_a_route_that_says_nothing_still_says_nothing():
    """None has to mean "use the adapter's default", not "DAY" -- every
    existing route omits these, and futures legs must keep behaving
    exactly as they did."""
    routes = resolve_routes(
        {"routes": [{"symbol": "XAUUSD", "target": "MGC"}]},
        _sig("XAUUSD"),
    )
    assert routes[0].tif is None
    assert routes[0].entry_timeout is None


def test_the_order_carries_both_to_the_adapter():
    order = OrderRequest(symbol="UGL", direction="BUY", size=10,
                         tif="GTC", entry_timeout=57600)
    assert order.tif == "GTC"
    assert order.entry_timeout == 57600
    plain = OrderRequest(symbol="MGC", direction="BUY", size=1)
    assert plain.tif is None and plain.entry_timeout is None


def test_the_adapter_refuses_a_time_in_force_it_cannot_place():
    """A bad TIF rejected by IBKR is a position that silently never opens,
    and the whole point of the setting is that nobody is watching at the
    moment it would matter."""
    from app.adapters.ibkr import IBKRAdapter
    assert "GTC" in IBKRAdapter.ENTRY_TIFS
    assert "DAY" in IBKRAdapter.ENTRY_TIFS
    # Meaningless for an entry this engine places -- accepting them would
    # let a route be written that never fills.
    assert "OPG" not in IBKRAdapter.ENTRY_TIFS
    assert "FOK" not in IBKRAdapter.ENTRY_TIFS


def test_the_sunday_evening_etf_route_is_expressible():
    """The shape the whole change exists for: a limit, outside regular
    hours, resting past this session."""
    routes = resolve_routes(
        {"routes": [{"symbol": "SP500", "target": "SPXL", "direction": "BUY",
                     "order_type": "limit", "outside_rth": True,
                     "limit_offset_pct": 0.01,
                     "tif": "GTC", "entry_timeout": 57600}]},
        _sig("SP500"),
    )
    r = routes[0]
    assert r.order_type == "limit", "a market order is refused outside RTH"
    assert r.outside_rth, "a limit without this waits for the 09:30 open"
    assert r.tif == "GTC", "a DAY order cannot reach Monday"
    assert r.entry_timeout and r.entry_timeout > 3600, (
        "GTC with the default 900s clock is cancelled by this engine long "
        "before the broker would expire it"
    )
