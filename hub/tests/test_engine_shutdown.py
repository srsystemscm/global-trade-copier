"""Stopping an engine must not be able to hang.

`stop_engine` is called from the app's lifespan AND from
`PATCH /slaves/{id}` and `DELETE /slaves/{id}`. An unbounded wait there is
not merely a slow shutdown: it is an API request that never answers, and a
hub that cannot be restarted cleanly on a VPS where IB Gateway forces a
restart every day.

None of this claims to fix the intermittent hang in the suite.
`asyncio.run()` cancels and awaits leftover tasks itself, so a task that
ignores cancellation hangs the teardown whether or not `stop()` waited for
it. This removes one way for the HUB to hang, not that one.
"""
import asyncio
import contextlib
import logging
import time

import pytest

from app import autonomous_copy_engine as auto_mod
from app import copy_engine as mirror_mod
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.copy_engine import CopyEngine
from tests.conftest_fakes import FakeBroker

SLAVE = "ibkr-paper-1"


class Stubborn:
    """Swallows `swallow` cancellations, then goes quietly.

    This is the shape of the real failure: a cancellation that does not
    take effect. The measured cause is narrower -- a cancel arriving while
    the task is about to collect a queue item is deferred and then lost --
    but what stop() has to survive is the same either way, and a task that
    simply ignores one is reproducible where the race is not.

    relent() makes it give up, so a test can settle a permanently stubborn
    task afterwards. Without that, asyncio.run() cancels and awaits
    leftovers when the loop closes and the test becomes the very hang it
    is testing for.
    """

    def __init__(self, swallow: int = 1) -> None:
        self.swallow = swallow
        self.cancels = 0

    async def run(self) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancels += 1
                if self.cancels > self.swallow:
                    raise

    def relent(self) -> None:
        self.swallow = 0


async def _settle(task: asyncio.Task) -> None:
    """Put a task to rest for good, so the loop can close."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _autonomous(cfg=None):
    return AutonomousCopyEngine(
        SLAVE, FakeBroker(), asyncio.Queue(), cfg or {"mode": "autonomous"})


# ------------------------------------------------------- the bounded wait


def test_stop_asks_again_when_a_cancellation_is_lost(monkeypatch, caplog):
    """The point of the retry.

    A cancellation that does not take effect used to leave stop() waiting
    on the task for ever. Asking again stops it -- which is what happens
    on CI, where a cancel arriving just as a queue item lands is deferred
    and then lost, leaving the task parked on a fresh future.
    """
    monkeypatch.setattr(auto_mod, "CANCEL_RETRY_SECONDS", 0.02)
    engine = _autonomous()
    stubborn = Stubborn(swallow=1)

    async def go():
        engine._consume_task = asyncio.create_task(
            stubborn.run(), name="stubborn-consume")
        await asyncio.sleep(0)          # let it reach its first await
        started = time.monotonic()
        with caplog.at_level(logging.WARNING):
            await engine.stop()
        return time.monotonic() - started, engine._consume_task

    elapsed, task = asyncio.run(go())

    assert stubborn.cancels == 2, "asked twice: once lost, once delivered"
    assert task.done(), "and it actually stopped"
    assert elapsed < 2.0
    assert "did not stop within" not in caplog.text, "it was not abandoned"
    assert "cancellation was lost and had to be repeated" in caplog.text


def test_stop_still_gives_up_on_a_task_that_never_stops(monkeypatch, caplog):
    """The retry is not a licence to wait for ever."""
    monkeypatch.setattr(auto_mod, "STOP_GRACE_SECONDS", 0.15)
    monkeypatch.setattr(auto_mod, "CANCEL_RETRY_SECONDS", 0.02)
    engine = _autonomous()
    stubborn = Stubborn(swallow=10_000)

    async def go():
        engine._consume_task = asyncio.create_task(
            stubborn.run(), name="stubborn-consume")
        await asyncio.sleep(0)
        started = time.monotonic()
        with caplog.at_level(logging.ERROR):
            await engine.stop()
        elapsed = time.monotonic() - started
        # Let it go, so asyncio.run()'s own teardown is not left awaiting a
        # task that ignores everything.
        stubborn.relent()
        await _settle(engine._consume_task)
        return elapsed

    elapsed = asyncio.run(go())

    assert stubborn.cancels > 2, "it kept being asked"
    assert elapsed < 2.0, f"stop() waited {elapsed:.1f}s past its grace"
    assert "did not stop within" in caplog.text
    assert "stubborn-consume" in caplog.text, "the abandoned task is named"


def test_a_well_behaved_engine_still_stops_everything():
    engine = _autonomous()

    async def go():
        engine.start()
        await asyncio.sleep(0.05)
        await engine.stop()
        return [engine._consume_task, engine._monitor_task,
                engine._deferred_task, engine._deferred_close_task,
                engine._preflight_task]

    tasks = asyncio.run(go())
    assert all(t is not None for t in tasks), "every task was started"
    assert all(t.done() for t in tasks), "and every one of them is finished"


def test_stop_does_not_wait_for_the_full_grace_when_tasks_die(monkeypatch):
    """The grace is a ceiling, not a delay."""
    monkeypatch.setattr(auto_mod, "STOP_GRACE_SECONDS", 30.0)
    engine = _autonomous()

    async def go():
        engine.start()
        await asyncio.sleep(0.05)
        started = time.monotonic()
        await engine.stop()
        return time.monotonic() - started

    assert asyncio.run(go()) < 2.0


# ----------------------------------------------- stopping what never ran


def test_stop_before_start_does_not_raise():
    """`_deferred_close_task` was only ever assigned in start(), while
    stop() read it unconditionally -- so an engine built but never started,
    one whose restore() raised, could not be shut down at all. It died with
    AttributeError instead."""
    asyncio.run(_autonomous().stop())


def test_stop_is_idempotent():
    engine = _autonomous()

    async def go():
        engine.start()
        await asyncio.sleep(0.05)
        await engine.stop()
        await engine.stop()

    asyncio.run(go())


# ------------------------------------------------------------ mirror mode


def test_the_mirror_engine_asks_again_too(monkeypatch, caplog):
    monkeypatch.setattr(mirror_mod, "CANCEL_RETRY_SECONDS", 0.02)
    engine = CopyEngine(SLAVE, FakeBroker(), asyncio.Queue(), {})
    stubborn = Stubborn(swallow=1)

    async def go():
        engine._task = asyncio.create_task(stubborn.run(), name="stubborn-mirror")
        await asyncio.sleep(0)
        with caplog.at_level(logging.WARNING):
            await engine.stop()
        return engine._task

    task = asyncio.run(go())

    assert stubborn.cancels == 2
    assert task.done()
    assert "did not stop within" not in caplog.text


def test_the_mirror_engine_still_gives_up(monkeypatch, caplog):
    monkeypatch.setattr(mirror_mod, "STOP_GRACE_SECONDS", 0.15)
    monkeypatch.setattr(mirror_mod, "CANCEL_RETRY_SECONDS", 0.02)
    engine = CopyEngine(SLAVE, FakeBroker(), asyncio.Queue(), {})
    stubborn = Stubborn(swallow=10_000)

    async def go():
        engine._task = asyncio.create_task(stubborn.run(), name="stubborn-mirror")
        await asyncio.sleep(0)
        started = time.monotonic()
        with caplog.at_level(logging.ERROR):
            await engine.stop()
        elapsed = time.monotonic() - started
        stubborn.relent()
        await _settle(engine._task)
        return elapsed

    elapsed = asyncio.run(go())

    assert elapsed < 2.0
    assert "did not stop within" in caplog.text


def test_the_mirror_engine_stops_normally():
    engine = CopyEngine(SLAVE, FakeBroker(), asyncio.Queue(), {})

    async def go():
        engine.start()
        await asyncio.sleep(0.05)
        await engine.stop()
        return engine._task

    assert asyncio.run(go()).done()


def test_the_mirror_engine_stop_before_start_does_not_raise():
    asyncio.run(CopyEngine(SLAVE, FakeBroker(), asyncio.Queue(), {}).stop())
