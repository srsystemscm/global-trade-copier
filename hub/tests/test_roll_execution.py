"""The contract roll's execution path.

Until now only the pure decision helpers were tested. The part that actually
moves money -- close the expiring contract, reopen on the next one, re-derive
the stop, carry the risk state -- had no coverage and has never run against a
live account, because it won't fire until a front month dies.
"""
import asyncio

import pytest

from app import position_store, trade_registry
from app.db import db_cursor, init_db
from tests.conftest_fakes import FakeBroker

from app.autonomous_copy_engine import AutonomousCopyEngine

SLAVE = "test-roll"


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
        "routes": [{"symbol": "XAUUSD", "target": "GC", "month_slot": 0,
                    "sizing": {"mode": "fixed_contracts", "contracts": 1}}],
        "risk_management": {"breakeven_trigger_atr": 1.0, "trailing_atr": 1.5},
        "contract_roll": {"enabled": True, "min_volume_ratio": 1.0, "check_interval_seconds": 0},
    }
    config.update(cfg)
    return AutonomousCopyEngine(SLAVE, broker, asyncio.Queue(), config)


def _seed(engine, **over):
    pos = {
        "slave_ticket": 18, "entry_price": 4040.7, "direction": "BUY", "size": 1.0,
        "atr": 18.0, "sl": 3953.6, "tp": None, "sl_mult": 4.8, "tp_mult": None,
        "breakeven_done": False, "month_slot": 0,
        "contract_id": 462941472, "contract_local": "GCZ6", "contract_expiry": "20261229",
        "sl_order_id": 19, "tp_order_id": None,
    }
    pos.update(over)
    engine._positions[("", 379792233, "GC")] = pos
    trade_registry.record_open(SLAVE, "", 379792233, "GC", pos["slave_ticket"])
    return pos


# --- _roll_one -------------------------------------------------------------

def test_roll_closes_the_old_contract_and_opens_the_next():
    b = FakeBroker()
    e = _engine(b)
    pos = _seed(e)
    asyncio.run(e._roll_one(("", 379792233, "GC"), pos, "test"))

    assert b.closed == [18], "the expiring contract must be closed"
    assert len(b.opened) == 1
    assert b.opened[0]["contract_id"] == 765079322, "must open GCG7, the next liquid month"


def test_roll_preserves_direction_and_size():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._roll_one(("", 379792233, "GC"), _seed(e, direction="SELL", size=3.0), "t"))
    assert b.opened[0]["direction"] == "SELL"
    assert b.opened[0]["size"] == 3.0


def test_stop_is_rederived_not_transplanted():
    """The calendar spread is real money, so carrying an absolute level across
    contracts is simply wrong. The new stop comes from the stored ATR multiple
    applied to the NEW contract's own ATR and quote."""
    b = FakeBroker(quote=4120.0)
    e = _engine(b)
    pos = _seed(e, sl=3953.6, sl_mult=4.8)
    asyncio.run(e._roll_one(("", 379792233, "GC"), pos, "t"))

    new_sl = b.opened[0]["sl"]
    assert new_sl != 3953.6, "stop was transplanted instead of re-derived"
    assert new_sl < 4120.0, "a long's stop must sit below the new entry"
    assert new_sl == pytest.approx(4120.0 - 4.8 * pos["atr"], rel=0.02)


def test_history_and_quote_come_from_the_new_contract():
    b = FakeBroker()
    e = _engine(b)
    asyncio.run(e._roll_one(("", 379792233, "GC"), _seed(e), "t"))
    assert ("history", "GC", 0, 765079322) in b.calls
    assert ("quote", "GC", 0, 765079322) in b.calls


def test_breakeven_survives_as_the_new_entry():
    """A position already at breakeven keeps that protection -- but breakeven
    on a different contract is the NEW entry, not the old price."""
    b = FakeBroker(quote=4120.0)
    e = _engine(b)
    pos = _seed(e, breakeven_done=True)
    asyncio.run(e._roll_one(("", 379792233, "GC"), pos, "t"))
    assert b.opened[0]["sl"] == 4120.0


def test_registry_and_stored_state_follow_the_roll():
    b = FakeBroker()
    e = _engine(b)
    pos = _seed(e)
    asyncio.run(e._roll_one(("", 379792233, "GC"), pos, "t"))

    assert pos["contract_local"] == "GCG7"
    assert pos["slave_ticket"] == b.opened[0]["ticket"]
    (_, stored), = position_store.load_all(SLAVE)
    assert stored["contract_local"] == "GCG7"
    assert stored["slave_ticket"] == b.opened[0]["ticket"]

    mappings = trade_registry.get_open_mappings(SLAVE, "", 379792233)
    assert mappings[0].slave_ticket == b.opened[0]["ticket"]


def test_roll_stops_at_the_end_of_the_chain():
    b = FakeBroker()
    e = _engine(b)
    pos = _seed(e, contract_id=776689881, contract_local="GCJ7")  # last in chain
    asyncio.run(e._roll_one(("", 379792233, "GC"), pos, "t"))
    assert b.closed == [], "must not close a position it can't reopen"
    assert b.opened == []


# --- _check_rolls ----------------------------------------------------------

def test_no_roll_while_the_front_month_still_leads():
    b = FakeBroker(volumes={0: 30000.0, 1: 2000.0})
    e = _engine(b)
    _seed(e)
    asyncio.run(e._check_rolls())
    assert b.opened == []


def test_rolls_once_the_next_month_takes_over():
    b = FakeBroker(volumes={0: 2000.0, 1: 30000.0})
    e = _engine(b)
    _seed(e)
    asyncio.run(e._check_rolls())
    assert len(b.opened) == 1


def test_disabled_means_never():
    b = FakeBroker(volumes={0: 1.0, 1: 99999.0})
    e = _engine(b, contract_roll={"enabled": False})
    _seed(e)
    asyncio.run(e._check_rolls())
    assert b.opened == []


def test_outermost_contract_rolls_first():
    """Front-first would move a position into the month the next strategy
    still holds, and on a netting account they would merge."""
    b = FakeBroker(volumes={0: 2000.0, 1: 30000.0})
    e = _engine(b)
    e._positions[("", 111, "GC")] = {**_seed(e), "slave_ticket": 18,
                                 "contract_id": 462941472, "contract_local": "GCZ6",
                                 "contract_expiry": "20261229"}
    e._positions[("", 222, "GC")] = {**_seed(e), "slave_ticket": 28,
                                 "contract_id": 765079322, "contract_local": "GCG7",
                                 "contract_expiry": "20270224"}
    e._positions.pop(("", 379792233, "GC"), None)
    asyncio.run(e._check_rolls())
    assert b.closed[0] == 28, "the outer position (GCG7) must vacate first"


def test_a_failed_leg_halts_that_symbol_sweep():
    """Continuing would move the next position into a contract the previous
    one failed to vacate."""
    b = FakeBroker(volumes={0: 2000.0, 1: 30000.0})
    b.fail_open_on = 0
    e = _engine(b)
    e._positions[("", 111, "GC")] = {**_seed(e), "slave_ticket": 18,
                                 "contract_id": 462941472, "contract_local": "GCZ6",
                                 "contract_expiry": "20261229"}
    e._positions[("", 222, "GC")] = {**_seed(e), "slave_ticket": 28,
                                 "contract_id": 765079322, "contract_local": "GCG7",
                                 "contract_expiry": "20270224"}
    e._positions.pop(("", 379792233, "GC"), None)
    asyncio.run(e._check_rolls())
    assert len(b.opened) == 0
    assert len(b.closed) == 1, "swept on after a failure instead of halting"


def test_positions_without_a_contract_are_skipped():
    """MT4 and equity legs have no contract months."""
    b = FakeBroker(volumes={0: 2000.0, 1: 30000.0})
    e = _engine(b)
    _seed(e, contract_id=None)
    asyncio.run(e._check_rolls())
    assert b.opened == []
