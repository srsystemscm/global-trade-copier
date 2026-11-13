"""Wait for an engine to finish, instead of sleeping and hoping.

Most engine tests were written as "start it, sleep a fixed span, stop it".
That span has to be long enough for the slowest machine the suite will
ever run on, and nobody knows what that is: too short and the test fails
for no reason, too long and the suite crawls. The canary found one such
test failing on CI having behaved perfectly -- it had simply run out of
wall clock.

`drain()` replaces the guess. It waits until the engine stops making
progress and its queue is empty, which is faster than a fixed sleep in the
common case and does not care how slow the machine is.

What counts as progress is deliberately coarse -- the broker's recorded
calls, the engine's tracked positions, its deferral queues, the signal
queue's depth. None of it needs a hook in production code, and a test that
wants something more specific should wait for that instead; see
`until()`.

NOTE this does not avoid the intermittent shutdown hang on CI. That one
happens with the engine already idle: the probe shows the signal handled
and the loop parked on an empty queue before the cancellation arrives. A
longer or smarter wait cannot help, because waiting is not what was
missing.
"""
import asyncio
import time
from typing import Any, Callable, Optional


async def until(predicate: Callable[[], bool], timeout: float = 5.0,
                poll: float = 0.005) -> bool:
    """Wait for a condition. True if it came true, False if it timed out.

    Returned rather than raised so a test can assert on it with its own
    message, which reads better in a failure than an exception from here.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(poll)
    return False


def _progress(engine: Any, broker: Any) -> tuple:
    """A cheap fingerprint of "something is still happening"."""
    return (
        engine.queue.qsize(),
        len(getattr(broker, "calls", ()) or ()),
        len(getattr(broker, "opened", ()) or ()),
        len(getattr(broker, "closed", ()) or ()),
        len(getattr(engine, "_positions", {}) or {}),
        len(getattr(engine, "_deferred", {}) or {}),
        len(getattr(engine, "_deferred_closes", {}) or {}),
    )


async def await_preflight(engine: Any, timeout: float = 5.0) -> bool:
    """Hold until preflight has had its say.

    Feeding a signal while preflight is still resolving contracts is a race
    the engine handles -- it waits -- but one that makes a test's timing
    depend on how long the broker fake takes to answer. Waiting here keeps
    that out of the measurement.
    """
    done = getattr(engine, "_preflight_done", None)
    if done is None:
        return True
    return await until(done.is_set, timeout)


async def drain(engine: Any, broker: Any = None, *, timeout: float = 5.0,
                quiet: float = 0.05, poll: float = 0.005) -> bool:
    """Wait until the engine has nothing left to do.

    "Nothing left" means its queue is empty AND the fingerprint above has
    stopped changing for `quiet` seconds. The quiet period matters: a
    signal that fans out to three instruments goes through several awaits,
    and the queue is empty for all of them.
    """
    deadline = time.monotonic() + timeout
    last = _progress(engine, broker)
    still_since: Optional[float] = None

    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        now = _progress(engine, broker)
        if now != last:
            last, still_since = now, None
            continue
        if not engine.queue.empty():
            still_since = None
            continue
        if still_since is None:
            still_since = time.monotonic()
        elif time.monotonic() - still_since >= quiet:
            return True
    return False


async def feed(engine: Any, signals, broker: Any = None, *,
               timeout: float = 5.0, quiet: float = 0.05) -> bool:
    """Put signals through a running engine and wait for it to settle.

    Returns whether it settled, so a caller can assert on it rather than
    discovering the engine was still busy through a confusing assertion
    further down.
    """
    await await_preflight(engine, timeout)
    for signal in signals:
        await engine.queue.put(signal)
    return await drain(engine, broker, timeout=timeout, quiet=quiet)
