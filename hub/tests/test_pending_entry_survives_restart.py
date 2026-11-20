"""A resting entry must not be reconciled away while it is still alive.

THE BUG. reconcile_slave asked one question -- does the broker hold a
position for this row? -- and read "no" as closed. A limit entry that has
not filled answers no while being very much alive. So on every hub start
the row went to CLOSED, position_store was emptied, and when the order
later filled there was nothing to record it against: an untracked
position, the one outcome reconciliation exists to prevent.

That is how the copier came to hold 1889 UGL and 641 TQQQ it had recorded
as gone.

The window used to be 15 minutes, the adapter's own abandon timer. On
2026-11-15 the ETF routes were given `tif: GTC` and a 26-hour wait, which
is what an entry placed at 18:00 ET needs to reach the next session --
and that made the window a day, spanning IB Gateway's daily restart.

So there are three outcomes now, not two: matched, stuck, and PENDING.
"""
import asyncio

import pytest

from app import position_store, trade_registry
from app.adapters.base import AdapterError, Position, WorkingOrder
from app.events import EventBus
from app.reconciliation import _row_is_pending, reconcile_slave

SLAVE = "test-pending"
ACCOUNT = "44964870"
TICKET = 5551
SYMBOL = "UGL"


class _Broker:
    """Holds no position and one working order, which is the whole case."""

    def __init__(self, working=None, positions=None, orders_raise=None):
        self._working = working if working is not None else []
        self._positions = positions or []
        self._orders_raise = orders_raise

    async def get_open_positions(self, account_type=None):
        return list(self._positions)

    async def get_working_orders(self):
        if self._orders_raise is not None:
            raise self._orders_raise
        return list(self._working)


def _row():
    """A registry row for an entry that has been placed and not filled."""
    trade_registry.record_open(
        SLAVE, ACCOUNT, TICKET, SYMBOL, slave_ticket=99,
        broker_order_ref=trade_registry.order_ref(ACCOUNT, TICKET, SYMBOL),
    )


def _run(broker):
    async def go():
        bus = EventBus()
        q = bus.subscribe()
        await reconcile_slave(SLAVE, broker, [None], bus)
        seen = []
        while not q.empty():
            seen.append(q.get_nowait())
        return seen
    return asyncio.run(go())


def _summary(events):
    for e in events:
        if e.get("type") == "reconciliation_summary":
            return e
    raise AssertionError("no summary emitted")


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    """Each test gets its own registry, so one cannot see another's rows."""
    from app import db
    monkeypatch.setattr(db.settings, "db_path", tmp_path / "t.db", raising=False)
    db.init_db()
    yield


# ------------------------------------------------- the bug itself --------

def test_a_row_whose_entry_is_still_working_stays_open():
    _row()
    ref = trade_registry.order_ref(ACCOUNT, TICKET, SYMBOL)
    events = _run(_Broker(working=[WorkingOrder(order_ref=ref, symbol=SYMBOL,
                                                status="Submitted")]))

    still_open = trade_registry.get_all_open_for_slave(SLAVE)
    assert len(still_open) == 1, (
        "the row was closed while its entry was still resting at the broker -- "
        "the fill will land with nothing to record it against"
    )
    assert _summary(events)["pending"], "the pass did not report it as pending"


def test_the_engine_state_is_kept_too():
    """Closing the row also deleted position_store, so even a row restored
    by hand would have no state. Both or neither."""
    _row()
    position_store.save(SLAVE, ACCOUNT, TICKET, SYMBOL,
                        {"direction": "BUY", "size": 10, "sl": 1.0,
                         "entry_price": 2.0, "slave_ticket": 99,
                         "month_slot": 0, "breakeven_done": False})
    ref = trade_registry.order_ref(ACCOUNT, TICKET, SYMBOL)
    _run(_Broker(working=[WorkingOrder(order_ref=ref, symbol=SYMBOL)]))
    assert position_store.load_all(SLAVE), "engine state was dropped anyway"


def test_a_pending_row_is_not_counted_as_matched():
    """It would report a clean pass while an entry was in the air."""
    _row()
    ref = trade_registry.order_ref(ACCOUNT, TICKET, SYMBOL)
    summary = _summary(_run(_Broker(
        working=[WorkingOrder(order_ref=ref, symbol=SYMBOL)])))
    assert summary["checked"] == 1
    assert summary["matched"] == 0
    assert len(summary["pending"]) == 1
    assert summary["stuck"] == []


# ------------------------------------------------- still closes drift ----

def test_a_row_with_no_position_and_no_working_order_is_still_closed():
    """The guard against over-correcting. Genuine drift -- a position
    stopped out while the hub was down -- must still be corrected, or the
    trailing monitor places a stop for something the broker does not
    hold, which on a flat account OPENS a position."""
    _row()
    events = _run(_Broker(working=[]))
    assert trade_registry.get_all_open_for_slave(SLAVE) == []
    summary = _summary(events)
    assert len(summary["stuck"]) == 1
    assert summary["stuck"][0]["resolved"] == "marked_closed"
    assert summary["pending"] == []


def test_a_row_the_broker_holds_is_matched_as_before():
    _row()
    summary = _summary(_run(_Broker(
        positions=[Position(symbol=SYMBOL, quantity=10, average_price=2.0)])))
    assert summary["matched"] == 1
    assert summary["stuck"] == [] and summary["pending"] == []


# ------------------------------------------------- can't ask -------------

def test_an_adapter_that_cannot_answer_behaves_as_before():
    """Most brokers' entries are market orders and never rest. Those
    adapters raise NotImplementedError and reconciliation keeps its old
    behaviour rather than guessing."""
    class _Old(_Broker):
        async def get_working_orders(self):
            raise NotImplementedError("no")

    _row()
    _run(_Old(working=[]))
    assert trade_registry.get_all_open_for_slave(SLAVE) == [], (
        "an adapter that cannot report working orders now blocks ordinary "
        "drift correction"
    )


def test_a_failure_to_ASK_leaves_rows_alone():
    """Could not ask is not the same as none working, and treating it as
    empty reintroduces the bug wholesale on the one day the broker
    connection is unhappy.

    An extra OPEN row is corrected on the next pass. An untracked
    position is not."""
    _row()
    events = _run(_Broker(orders_raise=AdapterError("no answer from IB")))
    assert len(trade_registry.get_all_open_for_slave(SLAVE)) == 1, (
        "a row was closed on the strength of a question that errored"
    )
    assert _summary(events)["pending"], "nothing said why it was left"


# ------------------------------------------------- the matcher -----------

def test_the_match_is_by_our_own_reference_first():
    """order_ref is the string the copier chose, so it answers for THIS
    row rather than for the instrument."""
    _row()
    row = trade_registry.get_all_open_for_slave(SLAVE)[0]
    ref = trade_registry.order_ref(ACCOUNT, TICKET, SYMBOL)
    assert _row_is_pending(row, {ref}, set())
    assert not _row_is_pending(row, {"someone-elses-ref"}, set())


def test_symbol_is_a_fallback_for_an_unstamped_row():
    """A row written before stamping existed, or by a path that did not
    stamp. Looser on purpose: two strategies resting on one instrument
    each keep the other's row OPEN, and that is the right direction to be
    wrong in."""
    _row()
    row = trade_registry.get_all_open_for_slave(SLAVE)[0]
    assert _row_is_pending(row, set(), {SYMBOL})
    assert not _row_is_pending(row, set(), {"SOMETHING-ELSE"})
