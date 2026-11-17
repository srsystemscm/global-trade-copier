"""A stopped consume loop has to announce itself.

The fix in test_the_consume_loop_survives.py stops one class of
exception from ending a loop. It cannot stop every one -- a handler
wedged on a broker call with no timeout consumes nothing while its task
stays perfectly alive, and a future `_run` could still return.

So the stall needs a watcher as well as a fix. These tests pin what
ConsumeWatchdog says and, as much as it matters, what it does NOT say:
an alert that fires every fifteen seconds for a condition that will not
change is an alert that gets muted, and a muted alert is how four stalls
went unnoticed in one day.
"""
import asyncio
import time

from app.watchdog import ConsumeWatchdog


class _Events:
    def __init__(self):
        self.emitted = []

    async def emit(self, event):
        self.emitted.append(event)


class _Notifier:
    def __init__(self):
        self.sent = []

    async def notify(self, event_type, message, **details):
        self.sent.append((event_type, message))


class _State:
    def __init__(self, engines):
        self.engines = engines


class _Engine:
    def __init__(self, slave_id, depth=0, last_consumed_at=0.0, task=None):
        self.slave_id = slave_id
        self.queue = asyncio.Queue()
        for _ in range(depth):
            self.queue.put_nowait(object())
        self.last_consumed_at = last_consumed_at
        self._consume_task = task


def _watchdog(engines):
    events, notifier = _Events(), _Notifier()
    w = ConsumeWatchdog(_State(engines), events, notifier, poll_interval=0.01, stale_after=60.0)
    return w, events, notifier


async def _dead_task(exc):
    """A task that has already ended the way a stalled engine's did."""
    async def boom():
        raise exc

    t = asyncio.create_task(boom())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert t.done()
    return t


# ------------------------------------------------- the loop has ended ---

def test_an_ended_loop_is_reported():
    async def go():
        task = await _dead_task(ValueError("something nobody caught"))
        w, events, notifier = _watchdog([_Engine("ibkr-paper-1", depth=3, task=task)])
        await w._check_all()
        return events.emitted, notifier.sent

    emitted, sent = asyncio.run(go())
    assert len(emitted) == 1, "a dead consume loop produced no event"
    assert emitted[0]["type"] == "slave_consume_stalled"
    assert emitted[0]["slave_id"] == "ibkr-paper-1"
    assert len(sent) == 1, "a dead consume loop sent no notification"


def test_the_report_names_the_exception_that_killed_it():
    """The whole point. Four stalls left no name, no type and no stack;
    retrieving the task's exception here is the ONLY moment it can be
    seen, because the engine holds the task reference forever and asyncio
    only complains when a task is collected."""
    async def go():
        task = await _dead_task(AssertionError("adapter not connected"))
        w, events, _ = _watchdog([_Engine("ibkr-paper-1", task=task)])
        await w._check_all()
        return events.emitted[0]["message"]

    msg = asyncio.run(go())
    assert "AssertionError" in msg, f"the cause is not in the alert: {msg}"
    assert "adapter not connected" in msg


def test_the_report_says_signals_are_being_dropped():
    """A reader has to know this is not a delay. The loop does not come
    back on its own, and until the hub restarts every signal for this
    slave goes nowhere."""
    async def go():
        task = await _dead_task(RuntimeError("x"))
        w, events, _ = _watchdog([_Engine("ibkr-paper-1", depth=2, task=task)])
        await w._check_all()
        return events.emitted[0]["message"].lower()

    msg = asyncio.run(go())
    assert "dropped" in msg
    assert "restart" in msg


def test_it_alerts_once_not_on_every_poll():
    async def go():
        task = await _dead_task(RuntimeError("x"))
        w, events, notifier = _watchdog([_Engine("ibkr-paper-1", task=task)])
        for _ in range(5):
            await w._check_all()
        return events.emitted, notifier.sent

    emitted, sent = asyncio.run(go())
    assert len(emitted) == 1, f"alerted {len(emitted)} times for one stall"
    assert len(sent) == 1


# ------------------------------------------------- alive but stuck ------

def test_a_queue_that_is_backed_up_while_nothing_is_consumed_is_reported():
    """The other shape: the task is alive, so `alive` says nothing is
    wrong, but a handler is stuck and the queue is filling behind it."""
    async def go():
        engine = _Engine("mt5-icm-1", depth=4, last_consumed_at=time.time() - 300)
        w, events, _ = _watchdog([engine])
        await w._check_all()
        return events.emitted

    emitted = asyncio.run(go())
    assert len(emitted) == 1, "a wedged loop with a filling queue was not reported"
    assert emitted[0]["condition"] == "backed_up"
    assert "4 signal(s) queued" in emitted[0]["message"]


def test_a_queue_being_drained_normally_is_not_reported():
    """Depth on its own is ordinary -- signals arrive in bursts and are
    gone in milliseconds. Alerting on it would make the watchdog noise."""
    async def go():
        engine = _Engine("mt5-icm-1", depth=6, last_consumed_at=time.time())
        w, events, _ = _watchdog([engine])
        await w._check_all()
        return events.emitted

    assert asyncio.run(go()) == []


def test_an_idle_engine_with_an_empty_queue_is_not_reported():
    """A quiet master is not a stall, and this is the case that has to
    stay silent or the alert means nothing."""
    async def go():
        async def forever():
            await asyncio.sleep(3600)

        task = asyncio.create_task(forever())
        w, events, _ = _watchdog([_Engine("ibkr-paper-1", depth=0, task=task)])
        await w._check_all()
        task.cancel()
        return events.emitted

    assert asyncio.run(go()) == []


def test_a_second_stall_is_reported_after_the_first_clears():
    """Arming once and never again would mean the watchdog works exactly
    one time per slave per hub lifetime."""
    async def go():
        engine = _Engine("mt5-icm-1", depth=4, last_consumed_at=time.time() - 300)
        w, events, _ = _watchdog([engine])
        await w._check_all()             # stalled
        while not engine.queue.empty():  # drained
            engine.queue.get_nowait()
        engine.last_consumed_at = time.time()
        await w._check_all()
        engine.queue.put_nowait(object())  # and stalls again
        engine.last_consumed_at = time.time() - 300
        await w._check_all()
        return events.emitted

    assert len(asyncio.run(go())) == 2


# ------------------------------------------------- the watcher itself ---

def test_the_watchdog_survives_its_own_failure():
    """It exists because a loop died quietly. A watchdog that dies the
    same way reproduces the bug one level up."""
    async def go():
        w, _events, _ = _watchdog([])
        calls = []
        # Waited on, not slept past: a fixed sleep passed on its own and
        # lost the race under the full suite, which is the worst kind of
        # test -- it trains you to re-run instead of look.
        third = asyncio.Event()

        async def failing():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("a poll that went wrong")
            third.set()

        w._check_all = failing
        task = asyncio.create_task(w._loop())
        await asyncio.wait_for(third.wait(), timeout=30)
        alive = not task.done()
        task.cancel()
        return alive, len(calls)

    alive, calls = asyncio.run(go())
    assert alive, "the watchdog ended on a failed poll"
    assert calls >= 3, "it stopped polling after the failures"


def test_it_reads_engines_from_state_so_new_slaves_are_watched():
    """POST /slaves appends to app.state.engines after the watchdog
    starts. A watchdog holding its own list would never see them."""
    async def go():
        engines = []
        w, events, _ = _watchdog(engines)
        await w._check_all()
        task = await _dead_task(RuntimeError("x"))
        engines.append(_Engine("added-later", task=task))
        await w._check_all()
        return events.emitted

    emitted = asyncio.run(go())
    assert len(emitted) == 1
    assert emitted[0]["slave_id"] == "added-later"
