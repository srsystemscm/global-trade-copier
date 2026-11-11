"""The autonomous engine's open and close paths.

These ran against a live IBKR account before they had any test at all. Every
bug the first real trade exposed -- a zero take-profit read as a price, a
stop that never reached the broker -- lived somewhere on this path.
"""
import asyncio

import pytest

from app import position_store, trade_registry
from app.db import db_cursor, init_db
from app.models import Signal
from tests.conftest_fakes import FakeBroker

from app.autonomous_copy_engine import AutonomousCopyEngine

SLAVE = "test-auto"


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    _wipe()
    yield
    _wipe()


def _wipe():
    with db_cursor() as cur:
        cur.execute("DELETE FROM engine_positions WHERE slave_id = ?", (SLAVE,))
        cur.execute("DELETE FROM slave_trades WHERE slave_id = ?", (SLAVE,))


def _engine(broker, **cfg):
    config = {
        "mode": "autonomous", "atr_period": 14,
        "routes": [
            {"magic": 2389, "symbol": "XAUUSD", "target": "GC", "month_slot": 0,
             "sizing": {"mode": "fixed_contracts", "contracts": 1}},
            {"magic": 7001, "symbol": "XAUUSD", "target": "GC", "month_slot": 1,
             "sizing": {"mode": "fixed_contracts", "contracts": 2}},
        ],
        "risk_management": {"breakeven_trigger_atr": 1.0, "trailing_atr": 1.5},
    }
    config.update(cfg)
    return AutonomousCopyEngine(SLAVE, broker, asyncio.Queue(), config)


def _sig(**kw):
    base = dict(ticket=1, symbol="XAUUSD", action="OPEN", direction="BUY",
                lots=2.0, price=4029.32, sl=3937.0, tp=0.0, atr=18.0, magic=2389)
    base.update(kw)
    return Signal(**base)


# --- routing on the way in -------------------------------------------------

def test_each_strategy_opens_on_its_own_contract_month():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(ticket=1, magic=2389)))
    asyncio.run(e._handle_open(_sig(ticket=2, magic=7001)))

    slots = [o["month_slot"] for o in b.opened]
    assert slots == [0, 1], "two strategies sharing a month would net"


def test_per_route_sizing_is_used():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(ticket=1, magic=2389)))
    asyncio.run(e._handle_open(_sig(ticket=2, magic=7001)))
    assert [o["size"] for o in b.opened] == [1, 2]


def test_an_unrouted_strategy_is_refused():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(magic=9999)))
    assert b.opened == [], "an unknown strategy must not land on another's contract"


def test_a_signal_with_no_magic_is_refused_when_routes_are_configured():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(magic=None)))
    assert b.opened == []


# --- the levels that reach the broker --------------------------------------

def test_zero_take_profit_does_not_become_a_target():
    """The live bug: tp=0.0 meant "no TP", but was read as a price of zero and
    produced a target at nearly twice the entry."""
    b = FakeBroker(quote=4050.0)
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(tp=0.0)))
    assert b.opened[0]["tp"] is None


def test_stop_is_derived_from_the_masters_atr_multiple():
    b = FakeBroker(quote=4050.0)
    e = _engine(b)
    sig = _sig(price=4029.32, sl=3937.0, atr=18.0)      # 5.13 ATR below entry
    asyncio.run(e._handle_open(sig))
    pos, = list(e._positions.values())
    expected_mult = abs(sig.price - sig.sl) / sig.atr
    assert b.opened[0]["sl"] == pytest.approx(4050.0 - expected_mult * pos["atr"], rel=0.02)


def test_a_sell_puts_its_stop_above_the_entry():
    b = FakeBroker(quote=4050.0)
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(direction="SELL", price=4029.32, sl=4120.0)))
    assert b.opened[0]["sl"] > 4050.0


def test_no_master_atr_means_no_trade():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(atr=0.0)))
    assert b.opened == []


def test_no_master_levels_means_no_trade():
    """Nothing to back-calculate a multiple from."""
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(sl=None, tp=None)))
    assert b.opened == []


# --- state written on the way out ------------------------------------------

def test_the_position_is_recorded_everywhere_it_needs_to_be():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(ticket=55)))

    assert ("", 55, "GC") in e._positions
    assert trade_registry.get_open_mappings(SLAVE, "", 55)
    (_, stored), = position_store.load_all(SLAVE)
    assert stored["contract_local"] == "GCZ6"
    assert stored["sl_mult"] is not None, "a roll could not re-derive stops without this"


def test_closing_clears_memory_registry_and_store():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(ticket=55)))
    asyncio.run(e._handle_close(_sig(ticket=55, action="CLOSE")))

    assert b.closed, "the broker position must actually be closed"
    assert ("", 55, "GC") not in e._positions
    assert position_store.load_all(SLAVE) == []
    assert not trade_registry.get_open_mappings(SLAVE, "", 55)


def test_close_for_an_unknown_ticket_places_no_orders():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_close(_sig(ticket=999, action="CLOSE")))
    assert b.closed == []


def test_restore_brings_a_position_back_after_a_restart():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._handle_open(_sig(ticket=55)))

    fresh = _engine(b)                      # a new process would start empty
    assert fresh._positions == {}
    asyncio.run(fresh.restore())
    assert ("", 55, "GC") in fresh._positions
    assert fresh._positions[("", 55, "GC")]["contract_local"] == "GCZ6"
