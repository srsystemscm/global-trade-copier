"""A held open or close survives a hub restart.

Both deferral queues lived only in the engine's memory, and the comments
where they were built said so. That is a daily exposure rather than a rare
one: CME halts its whole complex for an hour every day, an MT4 server on
GMT+2/+3 opens its daily bar right on that boundary, and IB Gateway forces
a restart once a day that the hub restarts with.

The close side is the worse loss, and the asymmetry is the point. A lost
deferred OPEN means not being in a trade the master is in -- an
opportunity cost. A lost deferred CLOSE means still carrying risk the
master has already dropped, with nothing retrying it and nothing able to
notice: the registry says OPEN and the broker agrees, because nothing ever
recorded the master's exit.

Timing waits on conditions with deadlines rather than sleeping for fixed
durations; Windows' loop granularity is ~15ms and these go flaky written
the obvious way.
"""
import asyncio
import time

import pytest

from app import deferred_store, trade_registry
from app.adapters.base import AdapterRejectedError
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.db import db_cursor
from app.deferred_store import CLOSE, OPEN
from app.models import Signal
from tests.conftest_fakes import FakeBroker

SLAVE = "ibkr-paper-1"
ACCT = "ICM-44964870"


# --------------------------------------------------------------- harnesses


def config(target="GC", **over):
    cfg = {
        "mode": "autonomous", "atr_period": 14,
        "routes": [{"symbol": "XAUUSD", "magic": 2389, "target": target,
                    "sizing": {"mode": "fixed_contracts", "contracts": 1}}],
        "risk_management": {"poll_interval_seconds": 3600},
        "defer_unavailable": {"retry_seconds": 0.01, "poll_seconds": 0.01,
                              "max_seconds": 5},
        "defer_close": {"retry_seconds": 0.01, "poll_seconds": 0.01,
                        "max_seconds": 60},
    }
    cfg.update(over)
    return cfg


def engine(broker, cfg=None):
    return AutonomousCopyEngine(SLAVE, broker, asyncio.Queue(), cfg or config())


def sig(**kw):
    base = dict(ticket=5001, symbol="XAUUSD", action="OPEN", direction="BUY",
                lots=2.0, price=4029.32, sl=3937.0, atr=18.0, magic=2389,
                account=ACCT)
    base.update(kw)
    return Signal(**base)


class ClosedMarket(FakeBroker):
    """Refuses every open until `reopen_after` attempts have been made."""

    def __init__(self, reopen_after=10_000, **kw):
        super().__init__(**kw)
        self.reopen_after = reopen_after
        self.attempts = 0

    async def open(self, order):
        self.attempts += 1
        if self.attempts <= self.reopen_after:
            raise AdapterRejectedError("market is not open for trading")
        return await super().open(order)


class WontClose(FakeBroker):
    """Refuses every close until `reopen_after` attempts have been made."""

    def __init__(self, reopen_after=10_000, **kw):
        super().__init__(**kw)
        self.reopen_after = reopen_after
        self.close_attempts = 0

    async def close(self, slave_ticket):
        self.close_attempts += 1
        if self.close_attempts <= self.reopen_after:
            raise AdapterRejectedError("market is closed")
        return await super().close(slave_ticket)


async def until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def drive(eng, signals, predicate=None, timeout=3.0):
    """Start the engine, feed it, wait for a condition, stop it."""

    async def go():
        eng.start()
        for s in signals:
            await eng.queue.put(s)
        if predicate is not None:
            await until(predicate, timeout)
        else:
            await asyncio.sleep(0.15)
        await eng.stop()

    asyncio.run(go())


def stored(kind=None):
    return deferred_store.load_all(SLAVE, kind)


def hold_a_close(ticket=9100, symbol="GC", slave_ticket=101):
    """A registry row the engine can defer the close of."""
    trade_registry.record_open(SLAVE, ACCT, ticket, symbol, slave_ticket)
    return ticket


# ================================================== the open side, on disk


def test_a_deferred_open_is_written_to_disk():
    broker = ClosedMarket()
    eng = engine(broker)
    drive(eng, [sig()], lambda: bool(eng._deferred))

    rows = stored(OPEN)
    assert len(rows) == 1
    row = rows[0]
    assert row.slave_symbol == "GC"
    assert row.signal.ticket == 5001
    assert row.signal.magic == 2389
    assert row.signal.account == ACCT
    assert row.attempts >= 1
    assert "not open for trading" in row.last_error


def test_the_route_is_not_stored_only_the_signal_and_the_key():
    """So a Route that gains a field leaves no stale JSON to rebuild from."""
    broker = ClosedMarket()
    eng = engine(broker)
    drive(eng, [sig()], lambda: bool(eng._deferred))

    with db_cursor() as cur:
        row = cur.execute("SELECT signal_json FROM deferred_actions").fetchone()
    assert "sizing" not in row["signal_json"], "that is route config, not signal"
    assert "XAUUSD" in row["signal_json"]


def test_a_retry_rewrites_the_schedule_rather_than_restarting_the_clock():
    """Otherwise every restart would extend the ceiling by another full
    window, one restart at a time."""
    broker = ClosedMarket()
    eng = engine(broker)
    drive(eng, [sig()], lambda: broker.attempts >= 3)

    row = stored(OPEN)[0]
    assert row.attempts >= 2, "the attempt count followed it to disk"
    assert row.first_at < row.next_at
    # first_at is when it was FIRST deferred, and must not move.
    assert row.next_at - row.first_at < 5.0


def test_an_open_that_fills_leaves_nothing_behind():
    broker = ClosedMarket(reopen_after=1)
    eng = engine(broker)
    drive(eng, [sig()], lambda: bool(broker.opened))

    assert broker.opened, "it filled on the retry"
    assert stored(OPEN) == [], "and is no longer held anywhere"


def test_an_abandoned_open_leaves_nothing_behind():
    """The ceiling passed. It is reported loudly and then forgotten."""
    broker = ClosedMarket()
    cfg = config()
    cfg["defer_unavailable"]["max_seconds"] = 0.05
    eng = engine(broker, cfg)
    drive(eng, [sig()], lambda: not eng._deferred and broker.attempts >= 1)

    assert eng._deferred == {}
    assert stored(OPEN) == []


def test_the_master_closing_drops_the_held_open_from_disk_too():
    """A restart must not resurrect an entry for a trade the master has
    already closed -- that is the copier putting on risk by itself.

    Both phases run in ONE event loop. The engine's queue binds itself to
    the first loop it is awaited on, so driving the same engine from two
    asyncio.run() calls fails with "bound to a different event loop" --
    which says nothing about the behaviour under test.
    """
    broker = ClosedMarket()
    eng = engine(broker)

    async def go():
        eng.start()
        await eng.queue.put(sig())
        held = await until(lambda: bool(eng._deferred))
        on_disk = stored(OPEN)

        await eng.queue.put(sig(action="CLOSE"))
        cancelled = await until(lambda: not eng._deferred)
        await eng.stop()
        return held, on_disk, cancelled

    held, on_disk, cancelled = asyncio.run(go())

    assert held and on_disk, "held first"
    assert cancelled, "the master's CLOSE dropped it"
    assert eng._deferred == {}
    assert stored(OPEN) == [], "and gone from disk, not just from memory"


# ============================================== the open side, across a restart


def test_a_held_open_is_restored_into_a_fresh_engine():
    broker = ClosedMarket()
    first = engine(broker)
    drive(first, [sig()], lambda: bool(first._deferred))
    held_key = next(iter(first._deferred))

    # The restart: a new engine over the same database.
    second = engine(ClosedMarket())
    asyncio.run(second.restore())

    assert list(second._deferred) == [held_key]
    item = second._deferred[held_key]
    assert item["signal"].ticket == 5001
    assert item["route"].target == "GC", "the route was re-resolved from config"
    assert item["route"].sizing == {"mode": "fixed_contracts", "contracts": 1}
    assert item["attempts"] >= 1


def test_a_restored_open_fills_on_the_next_pass():
    """The whole journey: deferred, hub restarted, then it opens."""
    first = engine(ClosedMarket())
    drive(first, [sig()], lambda: bool(first._deferred))

    broker = ClosedMarket(reopen_after=0)   # the market has reopened
    second = engine(broker)
    asyncio.run(second.restore())
    assert second._deferred, "restored before starting"

    drive(second, [], lambda: bool(broker.opened))

    assert len(broker.opened) == 1
    assert broker.opened[0]["symbol"] == "GC"
    assert second._deferred == {}
    assert stored(OPEN) == [], "nothing left to replay a second time"


def test_a_held_open_whose_route_is_gone_is_dropped_not_opened():
    """A config edited while the hub was down wins. Opening into a route
    nobody has any more is acting on a withdrawn instruction."""
    first = engine(ClosedMarket())
    drive(first, [sig()], lambda: bool(first._deferred))
    assert stored(OPEN)

    # GC is no longer routed on this slave.
    second = engine(ClosedMarket(), config(target="MGC"))
    asyncio.run(second.restore())

    assert second._deferred == {}
    assert stored(OPEN) == [], "dropped from disk as well"


def test_a_restored_open_past_its_ceiling_is_abandoned_loudly():
    """Rather than filled hours late at a price nobody chose. Loud is the
    improvement -- before this it was silently lost."""
    first = engine(ClosedMarket())
    drive(first, [sig()], lambda: bool(first._deferred))

    # It was deferred a long time ago.
    with db_cursor() as cur:
        cur.execute("UPDATE deferred_actions SET first_at = ?, next_at = ?",
                    (time.time() - 10_000, time.time() - 10_000))

    broker = ClosedMarket(reopen_after=0)
    second = engine(broker)
    asyncio.run(second.restore())
    assert second._deferred, "restored, so the loop can judge it"

    drive(second, [], lambda: not second._deferred)

    assert broker.opened == [], "never opened"
    assert stored(OPEN) == []


# ============================================= the close side, the worse loss


def test_a_deferred_close_is_written_to_disk():
    ticket = hold_a_close()
    broker = WontClose()
    eng = engine(broker)
    drive(eng, [sig(ticket=ticket, action="CLOSE")],
          lambda: bool(eng._deferred_closes))

    rows = stored(CLOSE)
    assert len(rows) == 1
    assert rows[0].slave_symbol == "GC"
    assert rows[0].signal.ticket == ticket
    assert "market is closed" in rows[0].last_error


def test_a_held_close_is_restored_and_keeps_trying():
    """Losing one leaves the slave holding a position the master exited,
    with nothing retrying it and reconciliation unable to see it."""
    ticket = hold_a_close()
    first = engine(WontClose())
    drive(first, [sig(ticket=ticket, action="CLOSE")],
          lambda: bool(first._deferred_closes))
    held_key = next(iter(first._deferred_closes))

    broker = WontClose()
    second = engine(broker)
    asyncio.run(second.restore())

    assert list(second._deferred_closes) == [held_key]
    item = second._deferred_closes[held_key]
    assert item["mapping"].slave_symbol == "GC"
    assert item["mapping"].slave_ticket == 101, "re-read from the registry"

    drive(second, [], lambda: broker.close_attempts >= 1)
    assert broker.close_attempts >= 1, "it is still trying"


def test_a_restored_close_completes_when_the_market_opens():
    ticket = hold_a_close()
    first = engine(WontClose())
    drive(first, [sig(ticket=ticket, action="CLOSE")],
          lambda: bool(first._deferred_closes))

    broker = WontClose(reopen_after=0)
    second = engine(broker)
    asyncio.run(second.restore())
    drive(second, [], lambda: bool(broker.closed))

    assert broker.closed == [101]
    assert second._deferred_closes == {}
    assert stored(CLOSE) == []
    rows = trade_registry.get_open_mappings(SLAVE, ACCT, ticket)
    assert rows == [], "and the registry row is closed"


def test_a_held_close_already_resolved_is_dropped():
    """If the registry no longer holds it open, something else resolved it.
    Retrying would send a closing order for a position that is not there --
    which on a netting account opens a reverse position."""
    ticket = hold_a_close()
    first = engine(WontClose())
    drive(first, [sig(ticket=ticket, action="CLOSE")],
          lambda: bool(first._deferred_closes))
    assert stored(CLOSE)

    # Reconciliation got there first.
    trade_registry.record_close(SLAVE, ACCT, ticket, "GC")

    broker = WontClose(reopen_after=0)
    second = engine(broker)
    asyncio.run(second.restore())

    assert second._deferred_closes == {}
    assert stored(CLOSE) == []
    assert broker.closed == [], "no order was sent for a position that is gone"


# ====================================================== the store on its own


def test_the_two_kinds_do_not_collide():
    """One ticket can legitimately have a held open on one target and a
    held close on another."""
    deferred_store.save(SLAVE, OPEN, sig(), "GC", _item(), month_slot=1)
    deferred_store.save(SLAVE, CLOSE, sig(), "GC", _item())

    assert len(stored()) == 2
    assert {r.kind for r in stored()} == {OPEN, CLOSE}
    assert len(stored(OPEN)) == 1
    assert len(stored(CLOSE)) == 1


def test_saving_the_same_action_twice_updates_it():
    deferred_store.save(SLAVE, OPEN, sig(), "GC", _item(attempts=1, last_error="a"))
    deferred_store.save(SLAVE, OPEN, sig(), "GC", _item(attempts=7, last_error="b"))

    rows = stored(OPEN)
    assert len(rows) == 1, "upserted, not duplicated"
    assert rows[0].attempts == 7
    assert rows[0].last_error == "b"


def test_the_month_slot_is_part_of_the_key():
    """Two strategies on one symbol get different contract months, and both
    can be held at once."""
    deferred_store.save(SLAVE, OPEN, sig(), "GC", _item(), month_slot=0)
    deferred_store.save(SLAVE, OPEN, sig(), "GC", _item(), month_slot=1)
    assert sorted(r.month_slot for r in stored(OPEN)) == [0, 1]


def test_an_unreadable_row_is_dropped_not_fatal():
    """An engine that will not start is worse than one deferral lost."""
    deferred_store.save(SLAVE, OPEN, sig(), "GC", _item())
    with db_cursor() as cur:
        cur.execute("UPDATE deferred_actions SET signal_json = 'not json'")

    assert stored(OPEN) == []


def test_a_write_failure_does_not_raise_into_the_retry_loop(monkeypatch):
    """Durability for a retry loop must not be able to stop the retry."""
    import app.deferred_store as store

    def boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "db_cursor", boom)
    store.save(SLAVE, OPEN, sig(), "GC", _item())       # must not raise
    store.delete(SLAVE, OPEN, ACCT, 5001, "GC")         # must not raise
    assert store.delete_for_ticket(SLAVE, ACCT, 5001) == 0


def test_rows_come_back_oldest_first():
    """A restore should rebuild the queue in the order things were deferred."""
    deferred_store.save(SLAVE, OPEN, sig(ticket=3), "GC",
                        _item(first_at=300.0), month_slot=3)
    deferred_store.save(SLAVE, OPEN, sig(ticket=1), "GC",
                        _item(first_at=100.0), month_slot=1)
    deferred_store.save(SLAVE, OPEN, sig(ticket=2), "GC",
                        _item(first_at=200.0), month_slot=2)
    assert [r.month_slot for r in stored(OPEN)] == [1, 2, 3]


def test_another_slaves_rows_are_not_returned():
    deferred_store.save("other-slave", OPEN, sig(), "GC", _item())
    assert stored() == []


def _item(**over):
    item = {"first_at": 1000.0, "next_at": 1030.0, "attempts": 1,
            "last_error": "market is not open for trading",
            "sl_mult": 1.5, "tp_mult": None}
    item.update(over)
    return item
