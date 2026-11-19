"""A close that fills nothing must not pass as a close.

_finish_close marks the registry row CLOSED when the ADAPTER ACCEPTS the
closing order, before any fill, and the fill price is collected
afterwards out of band. When that fill never arrives, _capture_fill used
to `return` on the reasoning that an uncaptured fill "simply leaves the
column NULL".

For an entry that is true, and _abandon_unfilled_entry handles it. For
an exit it is a live position nobody is managing: the row says CLOSED,
so nothing trails it, nothing re-closes it, and no screen shows it as
open.

MEASURED 2026-11-18 on ibkr-etf-1. The close went out at 19:51 ET as a
DAY market order with outsideRth unset, and IBKR answered:

    Warning 399, reqId 2768: SELL 789 ETHT ARCA Warning: Your order will
    not be placed at the exchange until 2026-11-19 09:30:00 US/Eastern.

Status PreSubmitted, filled 0.0 of 789. The engine logged "closed
deferred ETHT slave_ticket=1485 after 93109s and 1055 attempt(s)",
wrote CLOSED with a null close price, and said nothing more. 789 shares
with no stop and nothing watching them.

It says so now, and deliberately places nothing: the closing order is
usually still live -- that is why it has not filled -- and a second one
would sell the position twice at the next open.
"""
import asyncio

import pytest


class _Adapter:
    """Just enough for _capture_fill: an exit awaiter that reports no fill."""

    def __init__(self, result=(None, None)):
        self.result = result
        self.closes = []

    async def await_exit_fill(self, slave_ticket):
        return self.result

    async def await_entry_fill(self, slave_ticket):
        # _capture_fill looks for await_<which>_fill and does nothing at
        # all when it is absent, so an adapter missing this would make the
        # entry test pass for the wrong reason.
        return self.result

    async def close(self, slave_ticket):
        self.closes.append(slave_ticket)
        return None


def engine(adapter):
    from app.autonomous_copy_engine import AutonomousCopyEngine

    e = AutonomousCopyEngine.__new__(AutonomousCopyEngine)
    e.slave_id = "ibkr-etf-1"
    e.adapter = adapter
    e._fill_tasks = set()
    e._positions = {}
    e.emitted = []

    async def _emit(event_type, signal=None, **fields):
        e.emitted.append((event_type, fields))

    e._emit = _emit
    return e


async def capture(e, which="exit"):
    e._capture_fill(which, "44964870", 383168127, "ETHT", 1485)
    # the capture runs as a task; let it finish
    await asyncio.sleep(0)
    for task in list(e._fill_tasks):
        await task


def test_an_exit_that_filled_nothing_is_reported():
    """The whole point. Silence here is a position nobody can see."""
    a = _Adapter(result=(None, None))
    e = engine(a)
    asyncio.run(capture(e))

    types = [t for t, _ in e.emitted]
    assert "exit_never_filled" in types, (
        "an exit that filled nothing passed silently -- the registry row "
        "says CLOSED and the broker still holds the position"
    )


def test_it_carries_what_is_needed_to_find_the_position():
    """A warning nobody can act on is barely better than silence: the
    ticket and symbol are how it gets found at the broker."""
    e = engine(_Adapter(result=(None, None)))
    asyncio.run(capture(e))

    fields = dict(e.emitted[0][1])
    assert fields["slave_symbol"] == "ETHT"
    assert fields["slave_ticket"] == 1485
    assert fields["master_ticket"] == 383168127


def test_it_places_no_second_closing_order():
    """The closing order is usually STILL LIVE -- resting until the next
    open is exactly why it has not filled. Another one would sell the
    position twice."""
    a = _Adapter(result=(None, None))
    e = engine(a)
    asyncio.run(capture(e))
    assert a.closes == [], "a second closing order was placed"


def test_an_exit_that_did_fill_reports_nothing():
    """The guard against over-correcting: the ordinary path must stay
    quiet, or the alert means nothing."""
    import app.trade_registry as trade_registry

    recorded = []
    original = trade_registry.record_close
    trade_registry.record_close = lambda *a, **k: recorded.append((a, k))
    try:
        e = engine(_Adapter(result=(8.30, 789.0)))
        asyncio.run(capture(e))
    finally:
        trade_registry.record_close = original

    assert [t for t, _ in e.emitted] == []
    assert len(recorded) == 1


def test_an_entry_that_filled_nothing_still_goes_the_entry_way():
    """Unchanged: an unfilled ENTRY leaves no position, and its handler
    cancels the order and the protective legs. The two are not the same
    failure and must not share a path."""
    e = engine(_Adapter(result=(None, None)))
    called = []

    async def _abandon(account, master_ticket, slave_symbol, slave_ticket):
        called.append(slave_ticket)

    e._abandon_unfilled_entry = _abandon
    asyncio.run(capture(e, which="entry"))

    assert called == [1485]
    assert [t for t, _ in e.emitted] == []
