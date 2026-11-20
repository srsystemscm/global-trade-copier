"""A closing order that fills in pieces is still one exit.

MEASURED. hub.log, 2026-11-16 19:44:24, ibkr-etf-1, UGL 1161 -- a
7842-share position:

    close order 1166 only partially filled within 10.0s
      (6542.0 of 7842.0, status=Submitted) -- recording the part that filled
    exit fill for UGL slave_ticket=1161: 6542.0 @ 38.71

_await_fill returns what filled when its clock runs out. That is right
for an ENTRY -- that much really is on, and close() needs the true
number so it does not sell more than exists. For an EXIT it is a trap:
the figure becomes the position's close price and the row is marked
CLOSED while the rest of the order is still working at the broker.

The other 1300 filled moments later. The account ended flat, so no stock
was stranded -- but the row books a close measured on 83% of the
position. That is a silent bias in exactly the variance numbers this
registry exists to produce, and it grows with size: the bigger the
position, the likelier it does not clear in ten seconds.

Waiting longer is free here. Exits are collected on a background task,
never on the consume loop.

AND THE CASE THAT IS NOT MERELY COSMETIC: a closing order that goes
terminal part-filled means the slave IS STILL HOLDING the rest, under a
row that reads CLOSED. Every stray position in this paper account
started as that.
"""
import asyncio
import logging

import pytest

from app.adapters.ibkr import IBKRAdapter


class _Status:
    def __init__(self, filled=0.0, avg=0.0, remaining=0.0, status="Submitted"):
        self.filled = filled
        self.avgFillPrice = avg
        self.remaining = remaining
        self.status = status


class _Order:
    def __init__(self, qty):
        self.orderId = 1166
        self.totalQuantity = qty
        self.orderType = "MKT"
        self.action = "SELL"
        self.lmtPrice = 0.0


class _Trade:
    """A closing order for `want`, part-filled, still working."""
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
    a.slave_id = "ibkr-etf-1"
    a.exit_remainder_timeout = timeout
    return a


def _complete(adapter, trade, price, size):
    return asyncio.run(adapter._complete_exit_fill(trade, price, size))


# ------------------------------------------------- the bug --------------

def test_the_remainder_is_waited_out_and_the_whole_exit_reported():
    """THE BUG. 6542 of 7842 reported when the other 1300 was seconds
    away. IBKR's avgFillPrice is the average across every fill of the
    order, so once it completes the correct weighted price is simply
    read back off it."""
    trade = _Trade(7842.0, 6542.0, 38.71)

    async def go():
        a = _adapter()
        task = asyncio.create_task(a._complete_exit_fill(trade, 38.71, 6542.0))
        await asyncio.sleep(0.05)
        trade.complete(7842.0, 38.705)
        return await task

    price, size = asyncio.run(go())
    assert size == 7842.0, (
        f"the exit was recorded as {size} of a 7842.0 position"
    )
    assert price == 38.705, "the close price is still the partial's average"


def test_a_complete_fill_is_returned_untouched():
    """The ordinary path must not grow a wait it does not need."""
    trade = _Trade(7842.0, 7842.0, 38.71, status="Filled")
    assert _complete(_adapter(), trade, 38.71, 7842.0) == (38.71, 7842.0)


def test_nothing_known_about_the_size_is_left_alone():
    """A broker that does not report totalQuantity cannot be reasoned
    about, and inventing a wait for it would hang the capture task."""
    trade = _Trade(0.0, 0.0, 0.0)
    trade.order.totalQuantity = 0
    assert _complete(_adapter(), trade, 38.71, 500.0) == (38.71, 500.0)


# ------------------------------------------------- still holding --------

def test_a_cancelled_part_filled_exit_is_an_error_not_a_shrug(caplog):
    """This is the one that leaves stock behind. The closing order is
    terminal with part of the position unsold, so nothing else is going
    to close it -- and the row is about to read CLOSED."""
    trade = _Trade(7842.0, 6542.0, 38.71)
    trade.orderStatus.status = "Cancelled"

    with caplog.at_level(logging.ERROR):
        price, size = _complete(_adapter(), trade, 38.71, 6542.0)

    assert "STILL HOLDING" in caplog.text, (
        f"a part-filled cancelled exit was not reported: {caplog.text}"
    )
    assert "1300" in caplog.text, "the alert does not say how much is left"
    # What filled is still recorded: a measurement of part of the position
    # beats none.
    assert (price, size) == (38.71, 6542.0)


def test_an_exit_that_never_completes_says_the_slave_may_still_hold_it(caplog):
    """The bound has to run out somewhere, and when it does the honest
    statement is that the position may be partly open."""
    trade = _Trade(7842.0, 6542.0, 38.71)

    with caplog.at_level(logging.ERROR):
        price, size = _complete(_adapter(timeout=0.2), trade, 38.71, 6542.0)

    assert "MAY STILL BE HOLDING" in caplog.text
    assert (price, size) == (38.71, 6542.0)


def test_progress_short_of_the_whole_records_the_progress(caplog):
    """Filled more than when the clock ran out, but not all of it. That
    much is real and recording it beats recording the older number."""
    trade = _Trade(7842.0, 6542.0, 38.71)

    async def go():
        a = _adapter(timeout=0.3)
        task = asyncio.create_task(a._complete_exit_fill(trade, 38.71, 6542.0))
        await asyncio.sleep(0.05)
        trade.orderStatus.filled = 7000.0
        trade.orderStatus.remaining = 842.0
        trade.orderStatus.avgFillPrice = 38.708
        return await task

    with caplog.at_level(logging.WARNING):
        price, size = asyncio.run(go())
    assert size == 7000.0, f"recorded {size}, not the 7000.0 that had filled"
    assert price == 38.708


# ------------------------------------------------- it is wired in -------

def test_both_exit_waits_go_through_the_completion():
    """await_exit_fill waits once, then re-prices and waits again per
    chase step. A partial can land on any of them, and only one of them
    being guarded is the same bug with extra steps."""
    import inspect

    src = inspect.getsource(IBKRAdapter.await_exit_fill)
    assert src.count("_complete_exit_fill") == 2, (
        "the first wait and the chase's wait must both complete the fill; "
        f"found {src.count('_complete_exit_fill')} call(s)"
    )
