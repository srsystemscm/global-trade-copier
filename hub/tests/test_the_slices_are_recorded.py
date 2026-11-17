"""What each order of a split entry filled.

The position is ONE row and that is what everything trades from. These
rows are a measurement and nothing else.

Why they exist: the reason to spread a 33-lot entry over a window is
market impact, and the position's single average price is the number the
spreading was meant to improve -- so it cannot be the number that says
whether it worked. Slice 1 against slice 3 can. A later slice filling
worse is impact the split did not avoid; all three filling at the same
price is a book that would have taken the whole order anyway.

Chris, after watching three split runs work: "just cant see the split
orders anywhere."
"""
import asyncio

import pytest

from app import trade_registry
from app.hub_reporter import rows as leg_rows


SLICES = [
    {"n": 1, "broker_order_id": "991", "size": 11.0, "filled": 11.0,
     "fill_price": 3876.5, "placed_at": 1000.0},
    {"n": 2, "broker_order_id": "992", "size": 11.0, "filled": 11.0,
     "fill_price": 3876.5, "placed_at": 1005.0},
    {"n": 3, "broker_order_id": "993", "size": 11.0, "filled": 11.0,
     "fill_price": 3876.6, "placed_at": 1010.0},
]


def _record(slices=None):
    trade_registry.record_open(
        "ibkr-paper-1", "44964870", 382971639, "MGC", 2080, slave_lots=33.0)
    trade_registry.record_entry_slices(
        "ibkr-paper-1", "44964870", 382971639, "MGC", 2080,
        SLICES if slices is None else slices)


def _read():
    return trade_registry.entry_slices_for(
        "ibkr-paper-1", "44964870", 382971639, "MGC")


# ------------------------------------------------- the record -----------

def test_every_slice_is_kept_in_the_order_it_went_out():
    _record()
    got = _read()
    assert [r["n"] for r in got] == [1, 2, 3]
    assert [r["fill_price"] for r in got] == [3876.5, 3876.5, 3876.6]


def test_the_slices_add_up_to_the_position():
    """If they do not, one of the two records is wrong -- and the
    position's is the one anything trades from."""
    _record()
    assert sum(r["filled"] for r in _read()) == 33.0


def test_re_reporting_corrects_rather_than_duplicates():
    """The fill capture can run again after a reconnect, and three rows
    becoming six would make the sum above meaningless."""
    _record()
    trade_registry.record_entry_slices(
        "ibkr-paper-1", "44964870", 382971639, "MGC", 2080,
        [{**SLICES[2], "filled": 10.0, "fill_price": 3877.0}])
    got = _read()
    assert len(got) == 3, f"re-reporting made {len(got)} rows"
    assert got[2]["filled"] == 10.0, "the correction did not land"


def test_a_slice_that_filled_nothing_still_gets_a_row():
    """"Placed 11, filled none" is a measurement too, and it is how a
    window cut short by the master's CLOSE reads afterwards."""
    _record([{"n": 1, "size": 11.0, "filled": 11.0, "fill_price": 3876.5},
             {"n": 2, "size": 11.0, "filled": 0.0, "fill_price": None}])
    got = _read()
    assert len(got) == 2
    assert got[1]["filled"] == 0.0
    assert got[1]["fill_price"] is None, (
        "a slice that filled nothing was given a price"
    )


def test_nothing_to_record_writes_nothing():
    trade_registry.record_entry_slices(
        "ibkr-paper-1", "44964870", 382971639, "MGC", 2080, None)
    assert _read() == []


# ------------------------------------------------- the adapter ----------

class _Status:
    def __init__(self, filled=0.0, avg=0.0, remaining=0.0):
        self.filled, self.avgFillPrice, self.remaining = filled, avg, remaining
        self.status = "Filled"


class _Order:
    def __init__(self, oid, qty, perm=None):
        self.orderId, self.totalQuantity = oid, qty
        self.permId = perm
        self.orderType, self.action, self.lmtPrice = "MKT", "BUY", 0.0


class _Trade:
    def __init__(self, oid, qty, filled, avg, perm=None):
        self.order = _Order(oid, qty, perm)
        self.orderStatus = _Status(filled, avg, qty - filled)
        self.fills = [object()] if filled else []
        self.contract = object()
        self.log = []


def _adapter_with(detail, slices):
    from app.adapters.ibkr import IBKRAdapter

    a = object.__new__(IBKRAdapter)
    a.slave_id = "ibkr-paper-1"
    a._slice_detail = {2080: detail}
    a._entry_slices = {2080: slices}
    return a


def test_the_adapter_reads_each_slices_fill_off_its_own_trade():
    """The last moment the Trades are in hand. After await_entry_fill
    drops them there is no other record of what any single slice did."""
    detail = [{"n": 1, "size": 11.0}, {"n": 2, "size": 11.0}]
    a = _adapter_with(detail, [
        _Trade(991, 11, 11, 3876.5, perm=12345),
        _Trade(992, 11, 11, 3876.6),
    ])
    a._stamp_slice_fills(2080)
    assert detail[0]["filled"] == 11.0 and detail[0]["fill_price"] == 3876.5
    assert detail[1]["fill_price"] == 3876.6
    assert detail[0]["broker_order_id"] == "12345", (
        "the broker's own id is assigned after placing and was not picked up"
    )


def test_an_unfilled_slice_gets_no_price():
    detail = [{"n": 1, "size": 11.0}]
    a = _adapter_with(detail, [_Trade(991, 11, 0, 0.0)])
    a._stamp_slice_fills(2080)
    assert detail[0]["filled"] == 0.0
    assert detail[0]["fill_price"] is None


def test_the_detail_is_popped_so_nothing_accumulates():
    a = _adapter_with([{"n": 1}], [_Trade(991, 11, 11, 3876.5)])
    assert a.entry_slice_detail(2080) == [{"n": 1}]
    assert a.entry_slice_detail(2080) is None


# ------------------------------------------------- what goes out --------

class _Facts:
    def slave_account(self, _):
        return "DUR314653"

    def is_autonomous(self, _):
        return True

    def size_unit(self, *_):
        return "contracts"


def _leg_row(entry_slices):
    return {
        "slave_id": "ibkr-paper-1", "master_account": "44964870",
        "master_ticket": 382971639, "slave_symbol": "MGC",
        "slave_ticket": 2080, "status": "CLOSED", "opened_at": 1.0,
        "closed_at": 2.0, "requested_at": 1.0, "filled_at": 1.5,
        "master_price": 3871.08, "slave_price": 3876.5333, "slave_lots": 33.0,
        "master_close_price": 3870.0, "slave_close_price": 3868.89,
        "broker_order_id": "99", "broker_order_ref": "ref",
        "entry_slices": entry_slices,
    }


def test_the_ops_hub_leg_carries_the_slices():
    _record()
    leg = leg_rows._leg(_leg_row(3), _Facts())
    assert leg["entry_slices"] == 3
    assert [s["fill_price"] for s in leg["slices"]] == [3876.5, 3876.5, 3876.6]
    # The position's own figures are unchanged and are still the position's.
    assert leg["size"] == 33.0


def test_an_unsplit_leg_carries_no_slice_list():
    """None, not []. A one-element list under every leg on the page
    reads as though something were going on."""
    _record()
    assert leg_rows._leg(_leg_row(1), _Facts())["slices"] is None
    assert leg_rows._leg(_leg_row(None), _Facts())["slices"] is None


def test_the_copier_tab_gets_them_too():
    """The tab reads list_trades_with_copies, not the reporter."""
    import time as _t

    _record()
    trade_registry.record_fill(
        "ibkr-paper-1", "44964870", 382971639, "MGC",
        slave_price=3876.53, slave_lots=33.0, filled_at=_t.time(),
        entry_slices=3)

    # The master signal the leg hangs off, written the way SignalBus
    # writes it so the query under test sees a realistic row.
    import json

    import app.db as db
    with db.db_cursor() as cur:
        cur.execute(
            "INSERT OR IGNORE INTO trades (master_ticket, master_account,"
            " symbol, action, direction, lots, price, sl, tp, signal_ts,"
            " received_at, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (382971639, "44964870", "XAUUSD", "OPEN", "BUY", 0.85, 3871.08,
             3827.4, None, _t.time(), _t.time(), json.dumps({})))

    rows = trade_registry.list_trades_with_copies(limit=10)
    legs = [c for t in rows for c in t["slave_copies"]
            if c["slave_ticket"] == 2080]
    assert legs, "the leg did not come back at all"
    assert len(legs[0]["slices"]) == 3, (
        "the Copier tab's query does not carry the slices, so the tab "
        "cannot show the fills however the ops hub is told"
    )
