"""Open positions must survive a hub restart.

The Trade Registry records *that* a position opened, but everything needed to
manage it afterwards -- ATR multiples, the breakeven flag, the concrete
contract, the bracket order ids -- lived only in memory. A restart therefore
orphaned every open position: the trailing monitor stopped managing it, and
close() raised "no tracked position" when the master finally closed, leaving a
live position the copier could no longer touch.

That matters on a VPS, where IB Gateway forces a daily restart.
"""
import pytest

from app import position_store
from app.db import db_cursor, init_db


@pytest.fixture(autouse=True)
def _clean():
    init_db()
    with db_cursor() as cur:
        cur.execute("DELETE FROM engine_positions WHERE slave_id LIKE 'test-%'")
    yield
    with db_cursor() as cur:
        cur.execute("DELETE FROM engine_positions WHERE slave_id LIKE 'test-%'")


def _pos(**kw):
    base = dict(slave_ticket=18, direction="BUY", size=1.0, entry_price=4040.7,
                atr=18.5, sl=3953.6, tp=None, sl_mult=4.7, tp_mult=None,
                breakeven_done=False, month_slot=0, contract_id=462941472,
                contract_local="GCZ6", contract_expiry="20261229",
                sl_order_id=19, tp_order_id=None)
    base.update(kw)
    return base


def test_a_saved_position_comes_back_intact():
    position_store.save("test-1", "", 379792233, "GC", _pos())
    (key, got), = position_store.load_all("test-1")
    assert key == ("", 379792233, "GC")
    assert got["slave_ticket"] == 18
    assert got["contract_local"] == "GCZ6"
    assert got["contract_id"] == 462941472


def test_atr_multiples_survive_so_a_roll_can_rederive_stops():
    position_store.save("test-1", "", 1, "GC", _pos(sl_mult=4.7, tp_mult=2.5))
    (_, got), = position_store.load_all("test-1")
    assert (got["sl_mult"], got["tp_mult"]) == (4.7, 2.5)


def test_bracket_order_ids_survive_so_the_stop_can_be_replaced():
    position_store.save("test-1", "", 1, "GC", _pos(sl_order_id=19, tp_order_id=20))
    (_, got), = position_store.load_all("test-1")
    assert (got["sl_order_id"], got["tp_order_id"]) == (19, 20)


def test_breakeven_flag_round_trips_as_a_bool():
    """Stored as an int; a truthy 0 would silently re-arm breakeven."""
    position_store.save("test-1", "", 1, "GC", _pos(breakeven_done=True))
    (_, got), = position_store.load_all("test-1")
    assert got["breakeven_done"] is True

    position_store.save("test-1", "", 2, "GC", _pos(breakeven_done=False))
    got2 = dict(position_store.load_all("test-1"))[("", 2, "GC")]
    assert got2["breakeven_done"] is False


def test_saving_again_updates_rather_than_duplicates():
    """The trailing monitor saves on every stop move."""
    position_store.save("test-1", "", 1, "GC", _pos(sl=3953.6))
    position_store.save("test-1", "", 1, "GC", _pos(sl=3990.0, breakeven_done=True))
    rows = position_store.load_all("test-1")
    assert len(rows) == 1
    assert rows[0][1]["sl"] == 3990.0


def test_a_roll_updates_the_contract_in_place():
    position_store.save("test-1", "", 1, "GC", _pos())
    position_store.save("test-1", "", 1, "GC",
                        _pos(slave_ticket=31, contract_id=765079322, contract_local="GCG7",
                             contract_expiry="20270224"))
    (_, got), = position_store.load_all("test-1")
    assert (got["contract_local"], got["slave_ticket"]) == ("GCG7", 31)


def test_closing_forgets_it():
    position_store.save("test-1", "", 1, "GC", _pos())
    position_store.delete("test-1", "", 1, "GC")
    assert position_store.load_all("test-1") == []


def test_slaves_do_not_see_each_others_positions():
    position_store.save("test-1", "", 1, "GC", _pos())
    position_store.save("test-2", "", 1, "GC", _pos())
    assert len(position_store.load_all("test-1")) == 1
    assert len(position_store.load_all("test-2")) == 1


def test_two_strategies_on_one_symbol_are_separate_rows():
    """Different master tickets, different contract months -- the whole point
    of the multi-strategy design."""
    position_store.save("test-1", "", 111, "GC", _pos(month_slot=0, contract_local="GCZ6"))
    position_store.save("test-1", "", 222, "GC", _pos(month_slot=1, contract_local="GCG7"))
    rows = dict(position_store.load_all("test-1"))
    assert rows[("", 111, "GC")]["contract_local"] == "GCZ6"
    assert rows[("", 222, "GC")]["contract_local"] == "GCG7"


def test_nothing_stored_means_nothing_restored():
    assert position_store.load_all("test-empty") == []


# --- reconciliation must clear stored state too ----------------------------

def test_reconciliation_clears_the_stored_position():
    """Otherwise a position closed outside the hub gets restored on the next
    start, and the trailing monitor places a stop for something the broker
    doesn't hold -- which on a flat account opens a position instead of
    protecting one."""
    import asyncio
    import inspect

    from app import reconciliation, trade_registry

    src = inspect.getsource(reconciliation.reconcile_slave)
    assert "position_store.delete" in src, "stale engine state would survive reconciliation"

    position_store.save("test-recon", "", 555, "GC", _pos())
    trade_registry.record_open("test-recon", "", 555, "GC", 18)

    class _FlatBroker:
        async def get_open_positions(self, account_type):
            return []

    class _Events:
        async def emit(self, e):
            pass

    asyncio.run(reconciliation.reconcile_slave("test-recon", _FlatBroker(), [None], _Events()))
    assert position_store.load_all("test-recon") == []
    with db_cursor() as cur:
        cur.execute("DELETE FROM slave_trades WHERE slave_id = 'test-recon'")
