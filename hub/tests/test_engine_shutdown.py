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
    """Swallows its first cancellation, then goes quietly.

    Swallowing every cancellation would make this test the exact hang it
    is testing for: `asyncio.run()` cancels and awaits leftover tasks when
    the loop closes, so a permanently stubborn task would never let the
    test finish. One swallow is enough to prove `stop()` gives up.
    """

    def __init__(self) -> None:
        self.cancels = 0

    async def run(self) -> None:
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancels += 1
                if self.cancels >= 2:
                    raise


async def _settle(task: asyncio.Task) -> None:
    """Put a task to rest for good, so the loop can close."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


def _autonomous(cfg=None):
    return AutonomousCopyEngine(
        SLAVE, FakeBroker(), asyncio.Queue(), cfg or {"mode": "autonomous"})


# ------------------------------------------------------- the bounded wait


def test_stop_gives_up_on_a_task_that_ignores_cancellation(monkeypatch, caplog):
    monkeypatch.setattr(auto_mod, "STOP_GRACE_SECONDS", 0.1)
    engine = _autonomous()
    stubborn = Stubborn()

    async def go():
        engine._consume_task = asyncio.create_task(
            stubborn.run(), name="stubborn-consume")
        await asyncio.sleep(0)          # let it reach its first await
        started = time.monotonic()
        with caplog.at_level(logging.ERROR):
            await engine.stop()
        elapsed = time.monotonic() - started
        await _settle(engine._consume_task)
        return elapsed

    elapsed = asyncio.run(go())

    assert stubborn.cancels >= 1, "it was asked to stop"
    assert elapsed < 2.0, f"stop() waited {elapsed:.1f}s for a task that will not die"
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


def test_the_mirror_engine_gives_up_too(monkeypatch, caplog):
    monkeypatch.setattr(mirror_mod, "STOP_GRACE_SECONDS", 0.1)
    engine = CopyEngine(SLAVE, FakeBroker(), asyncio.Queue(), {})
    stubborn = Stubborn()

    async def go():
        engine._task = asyncio.create_task(stubborn.run(), name="stubborn-mirror")
        await asyncio.sleep(0)
        started = time.monotonic()
        with caplog.at_level(logging.ERROR):
            await engine.stop()
        elapsed = time.monotonic() - started
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
