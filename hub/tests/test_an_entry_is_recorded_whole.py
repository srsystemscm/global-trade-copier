"""An entry that fills in pieces is still one position.

MEASURED. hub.log, 2026-11-17, ibkr-paper-1, MET 2953 -- an order for
831 contracts:

    opened BUY MET x831.0 -> ticket=2953
    opened MET master_ticket=383009450 -> slave_ticket=2953 size=831.0
    exit fill for MET slave_ticket=2953: 831.0 @ 1819.862816

and the registry row: slave_lots 810.

The fill clock expired with 810 filled, the other 21 landed moments
later, and nothing went back for them. The row said the position opened
at 810 and closed at 831 -- disagreeing with itself about how big it had
been.

THE FOURTH AND LAST OF ITS FAMILY, all found on one day:

  close() took its reading before abandon_unfilled_entry settled
  the exit recorded what had filled when its clock ran out
  the split entry's clock started before its last slice was placed
  and this one, the plain entry

Each measured something too early and wrote the answer down as final.

WHY IT IS A WARNING AND NOT AN ALARM, unlike the exit: an entry left
part-filled is simply a smaller position, and close() sizes from what
actually filled. An exit left part-filled is stock still held under a
row that says closed. Same shape, different consequence, and the
messages say so.
"""
import asyncio
import logging

import pytest

from app.adapters.ibkr import IBKRAdapter


class _Status:
    def __init__(self, filled, avg, remaining, status="Submitted"):
        self.filled, self.avgFillPrice = filled, avg
        self.remaining, self.status = remaining, status


class _Order:
    def __init__(self, qty):
        self.orderId, self.totalQuantity = 2953, qty
        self.orderType, self.action, self.lmtPrice = "MKT", "BUY", 0.0


class _Trade:
    """An entry for `want`, part-filled, still working."""
    def __init__(self, want, filled, avg, status="Submitted"):
        self.order = _Order(want)
        self.orderStatus = _Status(filled, avg, want - filled, status)
        self.fills = [object()] if filled else []
        self.contract = object()
        self.log = []

    def complete(self, filled, avg, status="Filled"):
        self.orderStatus.filled = filled
        self.orderStatus.avgFillPrice = avg
        self.orderStatus.remaining = self.order.totalQuantity - filled
        self.orderStatus.status = status
        return self


def _adapter(timeout=2.0):
    a = object.__new__(IBKRAdapter)
    a.slave_id = "ibkr-paper-1"
    a.entry_remainder_timeout = timeout
    return a


def _complete(adapter, trade, price, size):
    return asyncio.run(adapter._complete_entry_fill(trade, price, size))


# ------------------------------------------------- the bug --------------

def test_the_remainder_is_waited_out_and_the_whole_entry_reported():
    """THE BUG, in the numbers it happened in: 810 reported when the
    other 21 were moments away."""
    trade = _Trade(831.0, 810.0, 1823.331481)

    async def go():
        a = _adapter()
        task = asyncio.create_task(a._complete_entry_fill(trade, 1823.331481, 810.0))
        await asyncio.sleep(0.05)
        trade.complete(831.0, 1823.1)
        return await task

    price, size = asyncio.run(go())
    assert size == 831.0, f"the entry was recorded as {size} of 831.0"
    assert price == 1823.1, "the entry price is still the partial's average"


def test_a_complete_fill_is_returned_untouched():
    """The ordinary path must not grow a wait it does not need."""
    trade = _Trade(831.0, 831.0, 1823.1, status="Filled")
    assert _complete(_adapter(), trade, 1823.1, 831.0) == (1823.1, 831.0)


def test_nothing_known_about_the_size_is_left_alone():
    trade = _Trade(0.0, 0.0, 0.0)
    trade.order.totalQuantity = 0
    assert _complete(_adapter(), trade, 1823.1, 500.0) == (1823.1, 500.0)


# ------------------------------------------------- not an alarm ---------

def test_a_cancelled_part_filled_entry_is_ordinary(caplog):
    """Where this differs from the exit. A partially filled entry is a
    smaller position, not stranded stock -- close() sizes from what
    filled. So it records the real number and says so calmly."""
    trade = _Trade(831.0, 810.0, 1823.331481)
    trade.orderStatus.status = "Cancelled"

    with caplog.at_level(logging.DEBUG):
        price, size = _complete(_adapter(), trade, 1823.331481, 810.0)

    assert (price, size) == (1823.331481, 810.0)
    assert "the position is 810" in caplog.text
    assert "STILL HOLDING" not in caplog.text, (
        "a part-filled ENTRY was reported as though stock had been stranded"
    )


def test_an_entry_that_never_completes_says_the_row_may_under_report(caplog):
    trade = _Trade(831.0, 810.0, 1823.331481)

    with caplog.at_level(logging.WARNING):
        price, size = _complete(_adapter(timeout=0.2), trade, 1823.331481, 810.0)

    assert (price, size) == (1823.331481, 810.0)
    assert "larger than the row says it opened at" in caplog.text


def test_progress_short_of_the_whole_records_the_progress():
    trade = _Trade(831.0, 810.0, 1823.331481)

    async def go():
        a = _adapter(timeout=0.3)
        task = asyncio.create_task(a._complete_entry_fill(trade, 1823.331481, 810.0))
        await asyncio.sleep(0.05)
        trade.orderStatus.filled = 825.0
        trade.orderStatus.remaining = 6.0
        trade.orderStatus.avgFillPrice = 1823.2
        return await task

    price, size = asyncio.run(go())
    assert size == 825.0
    assert price == 1823.2


# ------------------------------------------------- it is wired in -------

def test_both_unsplit_entry_waits_go_through_the_completion():
    """await_entry_fill has three paths. The split one completes itself,
    by waiting for every slice to go terminal; the plain wait and the
    chase each need this, and only one of them being guarded is the same
    bug with extra steps."""
    import inspect

    src = inspect.getsource(IBKRAdapter.await_entry_fill)
    assert src.count("_complete_entry_fill") == 2, (
        "the plain wait and the entry chase must both complete the fill; "
        f"found {src.count('_complete_entry_fill')} call(s)"
    )
    assert "_await_split_fill" in src, "the split path should still be separate"
