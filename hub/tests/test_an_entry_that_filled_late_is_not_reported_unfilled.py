"""An entry that filled inside the cancel round trip is a position.

MEASURED, not supposed. hub.log, 2026-11-16 13:09:13, ibkr-paper-1,
MGC order 1189 for 35 contracts:

    IBKRAdapter[ibkr-paper-1] cancelled unfilled entry 1189 and its
      protective legs
    Slave ibkr-paper-1 never filled MGC for master_ticket=382921948; the
      order and its protective legs have been cancelled. The master IS in
      this trade and this leg is NOT.
    IBKRAdapter[ibkr-paper-1] entry 1189 filled inside the cancel round
      trip (2.0 @ 3846.0); keeping the position and its protective legs
      rather than recording a cancel that did not happen
    slave=ibkr-paper-1 entry 1189 for MGC never filled; cancelled it and
      its protective legs instead of placing a closing order against a
      position that does not exist

Those last two lines are one second apart and they contradict each
other. abandon_unfilled_entry looked again, found the fill, and kept the
position on purpose -- and close() threw its return value away and
raised EntryNeverFilled regardless. The engine's handler for that then
marked the row CANCELLED, dropped the position from memory and from
position_store, and moved on.

2 MGC were left at the broker with nothing managing them, under a row
saying the entry never happened. The master was already out. Nothing on
any screen would have said so, and reconciliation reads CANCELLED as
"there was never a position here".

It is also the last thing ibkr-paper-1 logged before 37 minutes of
silence that only a hub restart ended.
"""
import asyncio

import pytest

from app.adapters.base import EntryNeverFilled
from app.adapters.ibkr import IBKRAdapter


class _Status:
    def __init__(self, filled=0.0, avg=0.0):
        self.filled = filled
        self.avgFillPrice = avg


class _Order:
    orderId = 7001


class _Trade:
    """An entry order the broker has not filled, as it reads locally."""
    def __init__(self):
        self.fills = []
        self.orderStatus = _Status()
        self.order = _Order()


def _adapter(abandon_result, closed):
    a = object.__new__(IBKRAdapter)
    a.ib = object()  # past the connection guard; nothing here touches it
    a.slave_id = "ibkr-paper-1"
    a.account = "DUR314653"
    a.entry_tif = "DAY"
    a._positions = {
        1189: {"symbol": "MGC", "direction": "BUY", "size": 35.0,
               "sl_order_id": 7002, "tp_order_id": 7003,
               "contract": "MGCZ6"},
    }
    a._entry_trades = {1189: _Trade()}
    # One slice, which is what an unsplit entry is. close() reads this
    # rather than _entry_trades, so that a split entry whose second slice
    # filled is not read as an entry that never filled.
    a._entry_slices = {1189: [a._entry_trades[1189]]}
    a._split_tasks = {}
    a._slices_placed = {}
    a._split_windows = {}
    a._entry_waits = {}
    a._exit_trades = {}

    a._cancel_tracked_order = lambda *_a, **_k: None

    async def _abandon(slave_ticket):
        return abandon_result

    async def _closing_order(ib_mod, pos, action):
        # What the close would actually be sent for.
        closed.append((pos["symbol"], action, pos["size"]))

        class _O:
            tif = None
        return _O()

    a.abandon_unfilled_entry = _abandon
    a._closing_order = _closing_order
    a._require_ib = lambda: object()
    a._require_connected = lambda: a.ib
    a._place = lambda contract, order: object()
    return a


def _close(adapter):
    return asyncio.run(adapter.close(1189))


# ------------------------------------------------- the bug --------------

def test_a_late_fill_is_closed_not_reported_as_never_filled():
    """THE BUG. abandon_unfilled_entry said the entry filled and kept the
    position; close() must close it, not raise."""
    closed = []
    adapter = _adapter(abandon_result=(3846.0, 2.0), closed=closed)
    try:
        _close(adapter)
    except EntryNeverFilled as exc:
        pytest.fail(
            "close() reported MGC as never filled although the entry had "
            f"already filled 2.0 @ 3846.0 ({exc}) -- the engine then marks "
            "the row CANCELLED and the position stays at the broker, "
            "unmanaged, with the master already out"
        )
    assert closed, "nothing was sent to close the position that was held"


def test_the_close_is_sized_to_what_actually_filled():
    """1189 was an order for 35 and a fill of 2. Closing 35 would sell
    the part that never filled -- which on a flat account is a new short
    position, the exact failure the never-filled check exists to stop."""
    closed = []
    adapter = _adapter(abandon_result=(3846.0, 2.0), closed=closed)
    _close(adapter)
    assert closed[0][2] == 2.0, (
        f"the closing order was sized {closed[0][2]}, not the 2.0 actually "
        "held; the order itself was for 35.0"
    )
    assert closed[0][1] == "SELL", "a long position closes with a SELL"


# ------------------------------------------------- still refusing -------

def test_a_genuinely_unfilled_entry_still_raises():
    """The branch has to keep working. This is the live case it was
    written for -- UGL, BITX and ETHA, where closing an entry that never
    filled opened the opposite position."""
    closed = []
    adapter = _adapter(abandon_result=(None, None), closed=closed)
    with pytest.raises(EntryNeverFilled):
        _close(adapter)
    assert not closed, "a closing order went out for a position that never existed"


def test_a_price_with_no_size_is_treated_as_unfilled():
    """A price and no quantity is not a position, and must not become a
    zero-quantity closing order."""
    closed = []
    adapter = _adapter(abandon_result=(3846.0, 0.0), closed=closed)
    with pytest.raises(EntryNeverFilled):
        _close(adapter)
    assert not closed
