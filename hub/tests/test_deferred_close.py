"""An exit the broker cannot take yet is held, not dropped.

The asymmetry with a deferred OPEN is the whole point. A missed entry means
not being in a trade the master is in -- an opportunity cost. A missed exit
means still carrying risk the master has already dropped, unhedged and
unattended. So the position and its registry row must stay exactly as they
are until the close actually succeeds: recording it early would hide a live
position from reconciliation, which is the one thing reconciliation exists
to catch.

Timing here waits on conditions with deadlines rather than sleeping for
fixed durations. Windows' event loop granularity is around 15ms and these
tests ran flaky when written the obvious way.
"""
import asyncio
import time

import pytest

from app import position_store, trade_registry
from app.adapters.base import AdapterRejectedError, EntryNeverFilled
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from tests.conftest_fakes import FakeBroker

SLAVE = "ibkr-etf-1"
ACCT = "ICM-44964870"
TICKET = 9100


def _config(**over):
    cfg = {
        "mode": "autonomous",
        "atr_period": 14,
        "routes": [{"symbol": "ETHUSD", "target": "ETHA", "order_type": "limit",
                    "outside_rth": True, "broker_brackets": False,
                    "sizing": {"mode": "fixed_contracts", "contracts": 10,
                               "rounding": "floor"}}],
        "defer_close": {"retry_seconds": 0.02, "poll_seconds": 0.01,
                        "max_seconds": 60},
    }
    cfg["defer_close"].update(over.pop("defer_close", {}))
    cfg.update(over)
    return cfg


class _Broker(FakeBroker):
    """Refuses to close until told otherwise."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.close_error = AdapterRejectedError("market is closed")
        self.close_attempts = 0

    async def close(self, slave_ticket):
        self.close_attempts += 1
        if self.close_error is not None:
            raise self.close_error
        return 46.20


def _row():
    return trade_registry.get_open_mappings(SLAVE, ACCT, TICKET)


async def _until(predicate, timeout=4.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


def _signals():
    base = dict(ticket=TICKET, symbol="ETHUSD", direction="BUY", lots=10.0,
                price=2052.02, account=ACCT, atr=100.0)
    return (Signal(action="OPEN", sl=1889.45, **base),
            Signal(action="CLOSE", sl=1889.45, **base))


async def _run(broker, body, cfg=None):
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine(SLAVE, broker, queue, cfg or _config())
    engine.start()
    try:
        return await body(engine, queue)
    finally:
        await engine.stop()


# ---------------- holding the exit --------------------------------------

def test_a_refused_close_keeps_the_position_and_the_open_row():
    """Recording the close early would hide a position that is still live."""
    broker = _Broker()
    opened, closed = _signals()

    async def body(engine, queue):
        await queue.put(opened)
        assert await _until(lambda: broker.opened), "never opened"
        await queue.put(closed)
        assert await _until(lambda: broker.close_attempts >= 2), "never retried"

        assert engine._deferred_closes, "the exit was not held"
        assert engine._positions, "forgot a position that is still open"
        assert _row(), "marked the row closed while still holding"

    asyncio.run(_run(broker, body))


def test_the_exit_goes_through_once_the_market_takes_it():
    broker = _Broker()
    opened, closed = _signals()

    async def body(engine, queue):
        await queue.put(opened)
        assert await _until(lambda: broker.opened)
        await queue.put(closed)
        assert await _until(lambda: broker.close_attempts >= 1)

        broker.close_error = None          # the session opens
        assert await _until(lambda: not engine._deferred_closes), "never retried in"
        assert await _until(lambda: not _row()), "row left open after closing"
        assert engine._positions == {}
        assert position_store.load_all(SLAVE) == []

    asyncio.run(_run(broker, body))


def test_the_exit_price_is_recorded_when_it_finally_fills():
    broker = _Broker()
    opened, closed = _signals()

    async def body(engine, queue):
        await queue.put(opened)
        assert await _until(lambda: broker.opened)
        await queue.put(closed)
        assert await _until(lambda: broker.close_attempts >= 1)
        broker.close_error = None
        assert await _until(lambda: not engine._deferred_closes)

    asyncio.run(_run(broker, body))

    from app.db import db_cursor
    with db_cursor() as cur:
        r = cur.execute(
            "SELECT status, slave_close_price FROM slave_trades "
            "WHERE slave_id=? AND master_ticket=?", (SLAVE, TICKET)).fetchone()
    assert r["status"] == "CLOSED"
    assert r["slave_close_price"] == 46.20


def test_one_deferral_per_position_not_one_per_retry():
    broker = _Broker()
    opened, closed = _signals()

    async def body(engine, queue):
        await queue.put(opened)
        assert await _until(lambda: broker.opened)
        await queue.put(closed)
        assert await _until(lambda: broker.close_attempts >= 3)
        assert len(engine._deferred_closes) == 1

    asyncio.run(_run(broker, body))


# ---------------- giving up ----------------------------------------------

def test_giving_up_leaves_every_record_in_place():
    """Nothing has closed the position, so tidying the records away would
    turn something someone must deal with into something nobody can see."""
    broker = _Broker()
    opened, closed = _signals()
    cfg = _config(defer_close={"max_seconds": 0})

    async def body(engine, queue):
        await queue.put(opened)
        assert await _until(lambda: broker.opened)
        await queue.put(closed)
        assert await _until(lambda: not engine._deferred_closes), "never gave up"

        assert engine._positions, "dropped a position that is still open"
        assert _row(), "closed the row for a position still held"

    asyncio.run(_run(broker, body, cfg))


def test_giving_up_notifies():
    class _Notifier:
        def __init__(self):
            self.sent = []

        async def notify(self, event, message, **kw):
            self.sent.append((event, message))

    broker = _Broker()
    notifier = _Notifier()
    opened, closed = _signals()
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine(
        SLAVE, broker, queue, _config(defer_close={"max_seconds": 0}),
        notifier=notifier)

    async def go():
        engine.start()
        try:
            await queue.put(opened)
            assert await _until(lambda: broker.opened)
            await queue.put(closed)
            assert await _until(lambda: notifier.sent), "no alarm raised"
        finally:
            await engine.stop()

    asyncio.run(go())
    event, message = notifier.sent[0]
    assert event == "deferred_close_abandoned"
    assert "STILL IN IT" in message


# ---------------- interactions -------------------------------------------

def test_an_entry_cancelled_while_waiting_ends_the_deferral():
    """If the entry turns out never to have filled, there is nothing to
    exit -- the row becomes CANCELLED rather than waiting forever."""
    broker = _Broker()
    opened, closed = _signals()

    async def body(engine, queue):
        await queue.put(opened)
        assert await _until(lambda: broker.opened)
        await queue.put(closed)
        assert await _until(lambda: broker.close_attempts >= 1)

        broker.close_error = EntryNeverFilled("entry never filled")
        assert await _until(lambda: not engine._deferred_closes)
        assert engine._positions == {}

    asyncio.run(_run(broker, body))

    from app.db import db_cursor
    with db_cursor() as cur:
        r = cur.execute(
            "SELECT status FROM slave_trades WHERE slave_id=? AND master_ticket=?",
            (SLAVE, TICKET)).fetchone()
    assert r["status"] == "CANCELLED"


def test_deferral_can_be_switched_off():
    broker = _Broker()
    opened, closed = _signals()
    cfg = _config(defer_close={"enabled": False})

    async def body(engine, queue):
        await queue.put(opened)
        assert await _until(lambda: broker.opened)
        await queue.put(closed)
        assert await _until(lambda: broker.close_attempts >= 1)
        await asyncio.sleep(0.1)
        assert engine._deferred_closes == {}, "held it anyway"

    asyncio.run(_run(broker, body, cfg))


def test_the_close_ceiling_is_far_longer_than_the_open_one():
    """An entry missed is an opportunity cost; an exit missed is live risk.
    The default has to cover a weekend, because a Friday evening close
    legitimately waits for Monday's open."""
    engine = AutonomousCopyEngine(
        SLAVE, _Broker(), asyncio.Queue(), {"mode": "autonomous"})
    assert engine.defer_close_max_seconds >= 259200
    assert engine.defer_close_max_seconds > engine.defer_max_seconds
