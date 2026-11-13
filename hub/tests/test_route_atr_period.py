"""ATR period is a property of the strategy, not of the slave account.

Two strategies can run on one slave with different ATR periods -- the
fxDreema EAs carry ATR_LONG per project -- and a slave-wide setting
silently applies one of them to both, translating the other's stop against
an ATR it never meant. Routes already carry `sizing` and `risk_management`
per strategy; this makes `atr_period` follow the same path.
"""
import asyncio

from app import position_store
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from app.routing import resolve_routes
from tests.conftest_fakes import FakeBroker

SLAVE = "test-route-atr"


def _engine(broker, routes, atr_period=14):
    config = {"mode": "autonomous", "atr_period": atr_period, "routes": routes,
              "risk_management": {"poll_interval_seconds": 3600}}
    return AutonomousCopyEngine(SLAVE, broker, asyncio.Queue(), config)


def _sig(**kw):
    base = dict(ticket=1, symbol="XAUUSD", action="OPEN", direction="BUY",
                lots=2.0, price=4029.32, sl=3937.0, atr=18.0, magic=2389,
                account="ICM-1")
    base.update(kw)
    return Signal(**base)


def _periods_requested(broker):
    """FakeBroker records ("history", symbol, month_slot, contract_id) -- the
    period is what get_price_history was asked for, so read it off the call."""
    return [c for c in broker.calls if c[0] == "history"]


# --- resolving ------------------------------------------------------------

def test_a_route_can_declare_its_own_atr_period():
    cfg = {"routes": [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                       "atr_period": 9}]}
    (route,) = resolve_routes(cfg, _sig())
    assert route.atr_period == 9


def test_a_route_without_one_falls_back_to_the_slave():
    cfg = {"routes": [{"symbol": "XAUUSD", "magic": 2389, "target": "GC"}]}
    (route,) = resolve_routes(cfg, _sig())
    assert route.atr_period is None, "None is what lets the slave default apply"


# --- using it -------------------------------------------------------------

def test_the_open_measures_with_the_routes_period():
    b = FakeBroker()
    e = _engine(b, [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                     "atr_period": 9,
                     "sizing": {"mode": "fixed_contracts", "contracts": 1}}],
                atr_period=14)
    asyncio.run(e._handle_open(_sig()))

    rows = position_store.load_all(SLAVE)
    assert len(rows) == 1
    (_key, pos), = rows
    assert pos["atr_period"] == 9, "the slave default overrode the strategy's"


def test_the_slave_default_still_applies_when_the_route_is_silent():
    b = FakeBroker()
    e = _engine(b, [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                     "sizing": {"mode": "fixed_contracts", "contracts": 1}}],
                atr_period=21)
    asyncio.run(e._handle_open(_sig()))

    (_key, pos), = position_store.load_all(SLAVE)
    assert pos["atr_period"] == 21


def test_two_strategies_on_one_slave_keep_their_own_periods():
    """The case a slave-wide setting cannot express at all."""
    b = FakeBroker()
    e = _engine(b, [
        {"symbol": "XAUUSD", "magic": 2389, "target": "GC", "month_slot": 0,
         "atr_period": 9, "sizing": {"mode": "fixed_contracts", "contracts": 1}},
        {"symbol": "XAUUSD", "magic": 7001, "target": "GC", "month_slot": 1,
         "atr_period": 20, "sizing": {"mode": "fixed_contracts", "contracts": 1}},
    ], atr_period=14)

    asyncio.run(e._handle_open(_sig(ticket=1, magic=2389)))
    asyncio.run(e._handle_open(_sig(ticket=2, magic=7001)))

    by_ticket = {k[1]: p["atr_period"] for k, p in position_store.load_all(SLAVE)}
    assert by_ticket == {1: 9, 2: 20}


def test_a_roll_re_measures_with_the_period_that_opened_the_position():
    """Not with whatever the slave default happens to be by then -- the roll
    re-derives stops from the stored multiples, so it has to use the same
    ruler that produced them."""
    b = FakeBroker()
    e = _engine(b, [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                     "atr_period": 9,
                     "sizing": {"mode": "fixed_contracts", "contracts": 1}}],
                atr_period=14)
    asyncio.run(e._handle_open(_sig()))

    (key, pos), = list(e._positions.items())
    b.calls.clear()
    asyncio.run(e._roll_one(key, pos, "test"))

    assert pos["atr_period"] == 9
    assert _periods_requested(b), "the roll fetched no history at all"


def test_the_period_survives_a_restart():
    """position_store is what the engine restores from, so a field that
    isn't stored is a field the roll loses."""
    b = FakeBroker()
    e = _engine(b, [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                     "atr_period": 9,
                     "sizing": {"mode": "fixed_contracts", "contracts": 1}}])
    asyncio.run(e._handle_open(_sig()))

    fresh = _engine(FakeBroker(), [], atr_period=14)
    asyncio.run(fresh.restore())

    (_key, pos), = list(fresh._positions.items())
    assert pos["atr_period"] == 9


# --- the bars that period counts ------------------------------------------

def test_a_route_can_declare_its_bar_size():
    """A strategy measuring ATR on H4 and a slave measuring the same multiple
    on daily bars are comparing two different volatilities."""
    cfg = {"routes": [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                       "bar_size": "4 hours"}]}
    (route,) = resolve_routes(cfg, _sig())
    assert route.bar_size == "4 hours"


def test_the_open_requests_the_routes_bar_size():
    b = FakeBroker()
    e = _engine(b, [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                     "atr_period": 9, "bar_size": "4 hours",
                     "sizing": {"mode": "fixed_contracts", "contracts": 1}}])
    asyncio.run(e._handle_open(_sig()))

    assert b.history_calls[0]["bar_size"] == "4 hours"
    assert b.history_calls[0]["period"] == 9


def test_no_bar_size_leaves_the_adapter_default():
    """None, not a string -- the adapter owns the default, so that existing
    configs keep asking for exactly what they asked for before."""
    b = FakeBroker()
    e = _engine(b, [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                     "sizing": {"mode": "fixed_contracts", "contracts": 1}}])
    asyncio.run(e._handle_open(_sig()))

    assert b.history_calls[0]["bar_size"] is None


def test_a_roll_re_measures_on_the_same_bars_as_the_entry():
    b = FakeBroker()
    e = _engine(b, [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                     "atr_period": 9, "bar_size": "4 hours",
                     "sizing": {"mode": "fixed_contracts", "contracts": 1}}])
    asyncio.run(e._handle_open(_sig()))

    (key, pos), = list(e._positions.items())
    b.history_calls.clear()
    asyncio.run(e._roll_one(key, pos, "test"))

    assert b.history_calls[0]["bar_size"] == "4 hours"
    assert b.history_calls[0]["period"] == 9


def test_bar_size_survives_a_restart():
    b = FakeBroker()
    e = _engine(b, [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                     "atr_period": 9, "bar_size": "4 hours",
                     "sizing": {"mode": "fixed_contracts", "contracts": 1}}])
    asyncio.run(e._handle_open(_sig()))

    fresh = _engine(FakeBroker(), [])
    asyncio.run(fresh.restore())
    (_key, pos), = list(fresh._positions.items())
    assert pos["bar_size"] == "4 hours"


# --- how much history to ask IBKR for -------------------------------------

def test_duration_scales_with_the_bar_size():
    """A fixed '30 D' was fine while every caller wanted daily bars. It is
    wrong both ways once the size varies: 30 days of 1-minute bars is tens of
    thousands and IBKR refuses outright, while 30 days of weekly bars is four
    and compute_atr() would reject it."""
    from app.adapters.ibkr import _duration_for

    assert _duration_for(14, "1 day") == "30 D"        # unchanged default
    assert _duration_for(60, "1 day") == "66 D"        # long period needs more

    weekly = int(_duration_for(14, "1 week").split()[0])
    assert weekly >= 133, "19 weekly bars need more than a month of history"

    intraday = int(_duration_for(14, "15 mins").split()[0])
    assert 1 <= intraday <= 5, "19 fifteen-minute bars is days, not a month"


def test_an_unrecognised_bar_size_is_refused_not_guessed():
    """Passing nonsense through would surface as an opaque IBKR error at the
    moment an entry was being sized."""
    import pytest as _pytest
    from app.adapters.base import AdapterRejectedError
    from app.adapters.ibkr import _duration_for

    with _pytest.raises(AdapterRejectedError) as exc:
        _duration_for(14, "1 fortnight")
    assert "1 fortnight" in str(exc.value)
