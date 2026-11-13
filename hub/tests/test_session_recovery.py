"""IB Gateway force-restarts daily, which kills the hub's IBKR session.

Before this, nothing rebuilt it: the watchdog noticed and alerted, and the
slave then stayed dark until someone restarted the hub by hand -- every day.
These cover the recovery path and the guard that keeps signals off a dead
adapter in the meantime.
"""
import asyncio
import time

from app.adapters.base import BrokerAdapter
from app.copy_engine import CopyEngine
from app.events import EventBus
from app.models import Signal
from app.notifier import Notifier
from app.watchdog import SlaveWatchdog


class _Adapter:
    """Reports whatever `ok` says. `recover()` flips it back, like a real
    reconnect would."""

    def __init__(self, ok=True):
        self.ok = ok
        self.status_calls = 0

    async def get_status(self):
        self.status_calls += 1
        return self.ok

    async def get_account_summary(self, account_type):
        raise NotImplementedError


def _watchdog(adapter, recover=None, on_session_state=None, max_attempts=3, paused=None):
    return SlaveWatchdog(
        "s1", adapter, EventBus(), Notifier(),
        account_types=[None], poll_interval=0.01, drawdown_alert_pct=0.05,
        is_paused=paused or (lambda: False),
        recover=recover,
        on_session_state=on_session_state,
        recovery_max_attempts=max_attempts,
        recovery_base_delay=0.01,
        recovery_max_delay=0.01,
    )


def _drive(watchdog, until=None, seconds=0.25, timeout=5.0):
    """Run the watchdog, then stop it and return what it emitted.

    `until` waits for a CONDITION with a deadline; `seconds` sleeps for a
    fixed span. Which one is right depends on what is being asserted, and
    the difference matters:

    - a test asserting something HAPPENED must wait for it. The recovery
      loop is driven by a real clock, and how many passes fit into a fixed
      span depends on the machine -- Windows' loop granularity is ~15ms,
      and a shared CI runner is slower than a desktop. These tests were
      written with fixed sleeps and one of them duly failed on CI under a
      newer dependency set, having escalated correctly and simply run out
      of wall clock before recovery completed. That is the test being
      wrong, not the watchdog.
    - a test asserting something did NOT happen cannot wait for it, so it
      keeps a fixed window.

    `until` is called with the events emitted so far, and may close over
    whatever else the test is watching.
    """
    async def go():
        emitted = []
        watchdog.events.emit = lambda e: _collect(emitted, e)  # type: ignore[assignment]
        watchdog.start()
        if until is None:
            await asyncio.sleep(seconds)
        else:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and not until(emitted):
                await asyncio.sleep(0.005)
        await watchdog.stop()
        return emitted
    return asyncio.run(go())


def _saw(event_type):
    """A condition for `until`: this event type has been emitted."""
    return lambda emitted: any(e.get("type") == event_type for e in emitted)


def _counted(event_type, n):
    return lambda emitted: sum(
        1 for e in emitted if e.get("type") == event_type) >= n


def _collect(sink, event):
    sink.append(event)

    async def noop():
        return None
    return noop()


def _types(emitted):
    return [e["type"] for e in emitted]


# --- recovery ----------------------------------------------------------

def test_disconnect_triggers_recovery():
    adapter = _Adapter(ok=False)
    attempts = []

    async def recover():
        attempts.append(1)
        adapter.ok = True

    w = _watchdog(adapter, recover=recover)
    w.connected = True  # was up, so the next poll is a disconnect transition
    types = _types(_drive(w, until=_saw("slave_session_recovered")))

    assert attempts, "recovery was never attempted"
    assert "slave_disconnected" in types
    assert "slave_session_recovered" in types
    assert w.connected is True


def test_recovery_retries_until_it_works():
    """Gateway takes a minute or two to come back, so the first attempts
    failing is the normal case, not an error."""
    adapter = _Adapter(ok=False)
    calls = {"n": 0}

    async def recover():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionRefusedError("gateway still restarting")
        adapter.ok = True

    w = _watchdog(adapter, recover=recover, max_attempts=10)
    w.connected = True
    types = _types(_drive(w, until=_saw("slave_session_recovered")))

    assert calls["n"] == 3
    assert "slave_session_recovered" in types
    assert "slave_recovery_failed" not in types


def test_recovery_escalates_once_then_keeps_trying():
    """max_attempts is when a human gets told, not when we stop. Stopping
    would strand the hub for any outage longer than the schedule -- the
    weekly Sunday 2FA login can sit unacknowledged for hours."""
    adapter = _Adapter(ok=False)
    calls = {"n": 0}

    async def recover():
        calls["n"] += 1
        raise ConnectionRefusedError("gateway down")

    w = _watchdog(adapter, recover=recover, max_attempts=2)
    w.connected = True
    # Both halves of the claim: it has alerted, AND it has carried on past
    # the cap. Waiting for only the alert would stop before the retries
    # that are the actual point.
    types = _types(_drive(w, until=lambda emitted: (
        _counted("slave_recovery_failed", 1)(emitted) and calls["n"] > 2)))

    assert types.count("slave_recovery_failed") == 1, "alerted more than once"
    assert calls["n"] > 2, "gave up at the cap instead of continuing to retry"


def test_recovery_after_escalation_still_reports_success():
    """A Gateway that comes back an hour later must be picked up without a
    hub restart."""
    adapter = _Adapter(ok=False)
    calls = {"n": 0}

    async def recover():
        calls["n"] += 1
        if calls["n"] < 4:
            raise ConnectionRefusedError("gateway down")
        adapter.ok = True

    w = _watchdog(adapter, recover=recover, max_attempts=2)
    w.connected = True
    types = _types(_drive(w, until=_saw("slave_session_recovered")))

    assert types.count("slave_recovery_failed") == 1
    assert "slave_session_recovered" in types
    assert w.connected is True


def test_adapters_without_recovery_are_untouched():
    """MT4 rebuilds its REQ socket on timeout and Schwab is stateless HTTP,
    so neither should gain a recovery loop."""
    adapter = _Adapter(ok=False)
    w = _watchdog(adapter, recover=None)
    w.connected = True
    # Waiting for the disconnect is enough to make the absences below
    # meaningful: with no `recover` there is no recovery loop to emit them
    # at all, so a longer window could not change the answer.
    types = _types(_drive(w, until=_saw("slave_disconnected")))

    assert "slave_disconnected" in types
    assert "slave_session_recovered" not in types
    assert "slave_recovery_failed" not in types


def test_pause_during_recovery_stands_down():
    """An operator who pauses mid-recovery wants the slave idle, and the
    next poll after resume should re-baseline rather than fire a
    transition for deliberately idle time."""
    adapter = _Adapter(ok=False)
    flag = {"paused": False}

    async def recover():
        flag["paused"] = True
        raise ConnectionRefusedError("gateway down")

    w = _watchdog(adapter, recover=recover, paused=lambda: flag["paused"], max_attempts=10)
    w.connected = True
    _drive(w, until=lambda _emitted: w.connected is None)

    assert w.connected is None


def test_engine_is_told_the_session_went_down_and_came_back():
    adapter = _Adapter(ok=False)
    states = []

    async def recover():
        adapter.ok = True

    w = _watchdog(adapter, recover=recover, on_session_state=states.append)
    w.connected = True
    _drive(w, until=lambda _emitted: states and states[-1] is True)

    assert states[0] is False, "engine was not told the session dropped"
    assert states[-1] is True, "engine was never cleared to trade again"


# --- the session gate on the engine ------------------------------------

class _CountingAdapter:
    def __init__(self):
        self.opens = 0
        self.closes = 0

    async def open(self, order):
        self.opens += 1
        from app.adapters.base import OrderResult
        return OrderResult(slave_ticket=1)

    async def close(self, slave_ticket):
        self.closes += 1

    async def modify(self, slave_ticket, sl, tp):
        pass


class _RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def notify(self, kind, message, **fields):
        self.sent.append((kind, message))


def _engine(session_up=True):
    adapter = _CountingAdapter()
    notifier = _RecordingNotifier()
    queue: asyncio.Queue = asyncio.Queue()
    engine = CopyEngine("s1", adapter, queue, events=None, notifier=notifier)
    engine.set_session_up(session_up)
    return engine, adapter, notifier, queue


def _pump(engine, queue, signals, seconds=0.05):
    async def go():
        engine.start()
        for s in signals:
            await queue.put(s)
        await asyncio.sleep(seconds)
        await engine.stop()
    asyncio.run(go())


def test_open_is_refused_while_the_session_is_down():
    engine, adapter, _, queue = _engine(session_up=False)
    _pump(engine, queue, [Signal(ticket=1, symbol="XAUUSD", action="OPEN",
                                 direction="BUY", lots=0.1, price=4050.0)])
    assert adapter.opens == 0


def test_refused_close_is_escalated():
    """Dropping a CLOSE leaves the slave holding a position the master has
    exited, and recovery's reconcile pass cannot find that drift -- the
    registry says OPEN and the broker agrees. Only a human can fix it, so
    only a human can be told."""
    engine, adapter, notifier, queue = _engine(session_up=False)
    _pump(engine, queue, [Signal(ticket=7, symbol="XAUUSD", action="CLOSE")])

    assert adapter.closes == 0
    assert any(kind == "close_refused" for kind, _ in notifier.sent)


def test_a_dead_session_does_not_kill_the_engine():
    """The original failure mode: an adapter asserting "not connected"
    raises AssertionError, which `except AdapterError` doesn't catch, so it
    escaped the consume loop and ended the engine task for the life of the
    process. The signal must be refused instead, and the engine must still
    be running when the session returns."""
    engine, adapter, _, queue = _engine(session_up=False)

    async def go():
        engine.start()
        await queue.put(Signal(ticket=1, symbol="XAUUSD", action="OPEN",
                               direction="BUY", lots=0.1, price=4050.0))
        await asyncio.sleep(0.05)
        engine.set_session_up(True)
        await queue.put(Signal(ticket=2, symbol="XAUUSD", action="OPEN",
                               direction="BUY", lots=0.1, price=4050.0))
        await asyncio.sleep(0.05)
        still_running = engine._task is not None and not engine._task.done()
        await engine.stop()
        return still_running

    still_running = asyncio.run(go())
    assert still_running, "engine task died on a signal received while down"
    assert adapter.opens == 1, "the signal after recovery was not copied"


def test_engines_start_cleared_to_trade():
    """start_engine_for_slave() connects the adapter before building the
    engine, so a live session is the normal case."""
    engine, _, _, _ = _engine()
    assert engine.session_up is True


# --- which adapters opt in ---------------------------------------------

def test_only_stateful_sessions_opt_into_recovery():
    from app.adapters.ibkr import IBKRAdapter
    from app.adapters.mt4 import MT4Adapter

    assert IBKRAdapter.requires_session_recovery is True
    assert MT4Adapter.requires_session_recovery is False
    assert BrokerAdapter.requires_session_recovery is False
