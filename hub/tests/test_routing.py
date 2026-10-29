"""Signal -> slave target routing, keyed on the master strategy's magic number.

The point of all this: on a netting broker two strategies long the same
contract merge into one position, and the autonomous engine's per-position
stops then fight each other. Each strategy gets its own contract month
instead.
"""
import pytest

from app.models import Signal
from app.routing import Route, describe_refusal, resolve_routes, routing_is_strict


def _sig(symbol="XAUUSD", magic=None):
    return Signal(ticket=1, symbol=symbol, action="OPEN", direction="BUY",
                  lots=0.1, price=4300.0, sl=4200.0, atr=18.0, magic=magic)


ROUTED = {
    "routes": [
        {"magic": 2389, "symbol": "XAUUSD", "target": "GC", "month_slot": 0,
         "sizing": {"mode": "fixed_contracts", "contracts": 1}},
        {"magic": 7001, "symbol": "XAUUSD", "target": "GC", "month_slot": 1},
        {"magic": 7002, "symbol": "XAUUSD", "target": "GC", "month_slot": 2},
    ]
}
LEGACY = {"symbol_map": {"XAUUSD": ["MGC", "GLD"]}}


# --- strict mode -----------------------------------------------------------

@pytest.mark.parametrize("magic,slot", [(2389, 0), (7001, 1), (7002, 2)])
def test_each_strategy_gets_its_own_month(magic, slot):
    routes = resolve_routes(ROUTED, _sig(magic=magic))
    assert len(routes) == 1
    assert routes[0].target == "GC"
    assert routes[0].month_slot == slot


def test_strategies_never_share_a_slot():
    slots = [resolve_routes(ROUTED, _sig(magic=m))[0].month_slot
             for m in (2389, 7001, 7002)]
    assert len(set(slots)) == 3, "two strategies sharing a slot would net"


def test_unknown_magic_is_refused():
    assert resolve_routes(ROUTED, _sig(magic=9999)) == []


def test_signal_with_no_magic_is_refused_in_strict_mode():
    """A pre-magic bridge EA against a routed slave must not silently land on
    slot 0 and net against a real strategy."""
    assert resolve_routes(ROUTED, _sig(magic=None)) == []


def test_unknown_symbol_is_refused():
    assert resolve_routes(ROUTED, _sig(symbol="EURUSD", magic=2389)) == []


def test_refusal_message_lists_what_is_configured():
    msg = describe_refusal(ROUTED, _sig(magic=9999))
    assert "9999" in msg and "XAUUSD" in msg


def test_per_route_sizing_is_carried():
    assert resolve_routes(ROUTED, _sig(magic=2389))[0].sizing["contracts"] == 1
    assert resolve_routes(ROUTED, _sig(magic=7001))[0].sizing is None


def test_catch_all_route_matches_any_magic():
    cfg = {"routes": [{"symbol": "XAUUSD", "target": "GC", "month_slot": 0}]}
    assert resolve_routes(cfg, _sig(magic=4242))[0].month_slot == 0
    assert resolve_routes(cfg, _sig(magic=None))[0].month_slot == 0


def test_one_route_can_fan_out_to_several_targets():
    cfg = {"routes": [{"magic": 1, "symbol": "XAUUSD", "targets": ["GC", "GLD"]}]}
    assert [r.target for r in resolve_routes(cfg, _sig(magic=1))] == ["GC", "GLD"]


# --- legacy mode -----------------------------------------------------------

def test_symbol_map_still_works_when_no_routes_configured():
    routes = resolve_routes(LEGACY, _sig(magic=2389))
    assert [r.target for r in routes] == ["MGC", "GLD"]
    assert all(r.month_slot == 0 for r in routes)


def test_legacy_ignores_magic_entirely():
    assert len(resolve_routes(LEGACY, _sig(magic=None))) == 2


def test_strictness_is_decided_by_the_routes_key():
    assert routing_is_strict(ROUTED) is True
    assert routing_is_strict(LEGACY) is False
    assert routing_is_strict({}) is False
