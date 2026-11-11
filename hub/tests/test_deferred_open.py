"""An open the broker cannot accept yet is held, not dropped.

CME halts its whole complex 17:00-18:00 ET, and an MT4 server on GMT+2/+3
opens its daily bar right on that boundary -- so a D1 strategy's entry
lands inside the halt roughly once a day. Dropped, it fills on the CFD legs
and silently does not exist on the futures one, with no registry row for
reconciliation to notice.
"""
import asyncio

from app.adapters.base import AdapterRejectedError
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from app.notifier import Notifier
from tests.conftest_fakes import FakeBroker

SLAVE = "test-defer"
ACCT = "ICM-1"


class _ClosedMarketBroker(FakeBroker):
    """Refuses opens until `closed_for` of them have been attempted."""

    def __init__(self, closed_for=1, **kw):
        super().__init__(**kw)
        self.closed_for = closed_for
        self.attempts = 0

    async def open(self, order):
        self.attempts += 1
        if self.attempts <= self.closed_for:
            raise AdapterRejectedError("market is not open for trading")
        return await super().open(order)


class _RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def notify(self, kind, message, **fields):
        self.sent.append((kind, message))


def _engine(broker, notifier=None, **defer):
    cfg = {
        "mode": "autonomous", "atr_period": 14,
        "routes": [{"symbol": "XAUUSD", "magic": 2389, "target": "GC",
                    "sizing": {"mode": "fixed_contracts", "contracts": 1}}],
        "risk_management": {"poll_interval_seconds": 3600},
        "defer_unavailable": {"retry_seconds": 0.01, "poll_seconds": 0.01,
                              "max_seconds": 5, **defer},
    }
    return AutonomousCopyEngine(SLAVE, broker, asyncio.Queue(), cfg, notifier=notifier)


def _sig(**kw):
    base = dict(ticket=5001, symbol="XAUUSD", action="OPEN", direction="BUY",
                lots=2.0, price=4029.32, sl=3937.0, atr=18.0, magic=2389,
                account=ACCT)
    base.update(kw)
    return Signal(**base)


async def _until(pred, timeout=3.0):
    """Wait for a condition rather than for a duration.

    Sleeping a fixed span and hoping is how these tests go flaky: the
    retry loop is driven by a real clock whose granularity on Windows is
    ~15ms, so "sleep 300ms" buys an unpredictable number of passes."""
    import time as _t
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def _drive(engine, signals, until=None, timeout=3.0):
    async def go():
        engine.start()
        for s in signals:
            await engine.queue.put(s)
        if until is not None:
            await _until(until, timeout)
        else:
            await asyncio.sleep(0.2)
        await engine.stop()
    asyncio.run(go())


# --- the point of the thing -----------------------------------------------

def test_a_refused_open_is_retried_until_it_fills():
    b = _ClosedMarketBroker(closed_for=3)
    _drive(_engine(b), [_sig()], until=lambda: b.opened)

    assert len(b.opened) == 1, "the entry was dropped instead of retried"
    assert b.attempts > 3


def test_without_deferral_the_open_is_simply_lost():
    """The behaviour this replaces, kept reachable for a slave that would
    rather fail fast than carry an entry across a halt."""
    b = _ClosedMarketBroker(closed_for=99)
    _drive(_engine(b, enabled=False), [_sig()])

    assert b.opened == []
    assert b.attempts == 1, "it retried despite deferral being off"


def test_it_gives_up_eventually_and_says_so_loudly():
    """Silence here would mean believing a leg is on when it never opened."""
    n = _RecordingNotifier()
    b = _ClosedMarketBroker(closed_for=999)
    _drive(_engine(b, n, max_seconds=0.05), [_sig()], until=lambda: n.sent)

    assert b.opened == []
    assert any(k == "deferred_open_abandoned" for k, _ in n.sent)
    msg = [m for k, m in n.sent if k == "deferred_open_abandoned"][0]
    assert "master IS in this trade" in msg


def test_a_master_close_cancels_a_pending_open():
    """The dangerous case. A deferred entry that outlives the trade it
    belongs to would open a position the master is no longer in -- the
    copier putting on risk by itself, which is worse than the missed fill."""
    b = _ClosedMarketBroker(closed_for=999)
    e = _engine(b)

    async def go():
        e.start()
        await e.queue.put(_sig())
        await asyncio.sleep(0.05)
        assert e._deferred, "nothing was deferred, so the test proves nothing"
        await e.queue.put(_sig(action="CLOSE"))
        await asyncio.sleep(0.1)
        still_deferred = dict(e._deferred)
        await e.stop()
        return still_deferred

    assert asyncio.run(go()) == {}, "a closed trade was still waiting to open"
    assert b.opened == [], "it opened a position the master had already exited"


def test_the_same_signal_is_not_deferred_twice():
    b = _ClosedMarketBroker(closed_for=999)
    e = _engine(b)

    async def go():
        e.start()
        await e.queue.put(_sig())
        await e.queue.put(_sig())          # a replay
        await asyncio.sleep(0.08)
        n = len(e._deferred)
        await e.stop()
        return n

    assert asyncio.run(go()) == 1


def test_the_retry_reprices_rather_than_reusing_halt_data():
    """What carries over is the master's intent -- the ATR multiple. The
    contract itself is re-priced and re-sized when it actually fills, not
    from whatever could be read while the market was shut."""
    b = _ClosedMarketBroker(closed_for=2)
    _drive(_engine(b), [_sig()], until=lambda: b.opened)

    quotes = [c for c in b.calls if c[0] == "quote"]
    assert len(quotes) >= 3, "it reused the first quote instead of re-pricing"
