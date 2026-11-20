"""An engine that stops consuming must not do it silently.

Four times on 2026-11-16 an AutonomousCopyEngine stopped draining its
queue, permanently, with no exception and no log line. The adapter
stayed connected, /slaves kept reporting connected: true, and every
later signal for that slave was dropped while other slaves copied
normally. Eight signals went to MT5 and never reached ibkr-paper-1.

THE MECHANISM IS IN THE LOOP, not in whatever raised:

    async def _run(self):
        while True:
            signal = await self.queue.get()
            ...
            try:
                await self._handle(signal)
            except AdapterError as exc:      # <- ONLY AdapterError
                logger.error(...)

Anything that is not an AdapterError escapes `_run`, which ends the
task. Nothing logs it: asyncio only complains about an un-retrieved task
exception when the task is garbage collected, and `self._consume_task`
holds a reference for the life of the engine, so it never is. The
engine is then a live object with a dead loop -- which is exactly what
"connected, consuming nothing, no error" means.

The background fill-capture task three hundred lines away already gets
this right:

    except Exception:
        logger.exception("slave=%s failed collecting the %s fill ...")

The consume loop is the one place where swallowing is more dangerous
than crashing, and it was the one place that did not.

WHAT THIS DOES NOT CLAIM. The live trigger correlated with abandoning an
unfilled entry, and the exception behind it is still unidentified --
every path read so far (_handle_close, IBKRAdapter.close) raises only
AdapterError subclasses. These tests pin the loop's behaviour, which is
wrong whatever raised; the fix also makes the next occurrence name its
own cause instead of vanishing.
"""
import asyncio
import logging

import pytest

from app.adapters.base import AdapterError, AdapterRejectedError
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal


def _signal(ticket=1):
    return Signal(ticket=ticket, symbol="XAUUSD", action="OPEN",
                  direction="BUY", lots=1.0, price=100.0, sl=98.0,
                  atr=1.0, magic=1001, account="44964870")


def _engine(handler):
    """A real engine with _handle replaced, so the loop is the thing
    under test rather than the signal path."""
    e = object.__new__(AutonomousCopyEngine)
    e.slave_id = "test-consume"
    e.queue = asyncio.Queue()
    e.paused = False
    e.handled = []

    async def _handle(signal):
        e.handled.append(signal.ticket)
        await handler(signal)

    async def _refuse(signal):
        return False

    async def _emit(*a, **k):
        return None

    e._handle = _handle
    e.refuse_while_down = _refuse
    e._emit = _emit
    return e


async def _drive(engine, signals, settle=0.05):
    task = asyncio.create_task(engine._run())
    for s in signals:
        engine.queue.put_nowait(s)
    await asyncio.sleep(settle)
    done = task.done()
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    return done


def _run(engine, signals):
    return asyncio.run(_drive(engine, signals))


# ------------------------------------------------- the bug --------------

def test_a_non_adapter_error_does_not_kill_the_loop():
    """THE BUG. One signal raising ValueError must not stop the slave
    copying for the rest of the session."""
    async def boom(signal):
        if signal.ticket == 2:
            raise ValueError("something the loop never expected")

    e = _engine(boom)
    _run(e, [_signal(1), _signal(2), _signal(3), _signal(4)])
    assert e.handled == [1, 2, 3, 4], (
        f"the loop stopped after the raising signal: handled {e.handled}"
    )


def test_the_loop_task_stays_alive():
    """Not just "later signals arrive" -- the task itself must not end.
    A finished task is an engine that will never consume again, however
    many signals are queued behind it."""
    async def boom(signal):
        raise RuntimeError("anything at all")

    e = _engine(boom)
    ended = _run(e, [_signal(1)])
    assert not ended, "the consume task ended; the slave is dead and silent"


def test_the_failure_is_logged_with_its_traceback(caplog):
    """The reason four stalls were untraceable: nothing was written.
    A stack is what turns the next one into a ten-minute diagnosis."""
    async def boom(signal):
        raise KeyError("some_missing_key")

    e = _engine(boom)
    with caplog.at_level(logging.ERROR):
        _run(e, [_signal(1)])
    text = caplog.text
    assert "some_missing_key" in text, "the exception was swallowed"
    assert "KeyError" in text, "the exception type is not in the log"
    assert "Traceback" in text, (
        "logged without a stack, so the next stall still cannot be placed"
    )


def test_an_adapter_error_still_behaves_as_before(caplog):
    """The existing branch must keep its wording and its event: it is the
    ordinary 'the broker refused this' path, not a surprise."""
    async def refuse(signal):
        raise AdapterRejectedError("broker said no")

    e = _engine(refuse)
    emitted = []

    async def _emit(name, **kw):
        emitted.append(name)

    e._emit = _emit
    with caplog.at_level(logging.ERROR):
        _run(e, [_signal(1), _signal(2)])
    assert e.handled == [1, 2]
    assert "failed to handle" in caplog.text
    assert "slave_error" in emitted


def test_a_cancel_still_stops_the_loop():
    """Shutdown must keep working. CancelledError is not a failure to
    survive -- catching it would make an engine unstoppable, which is a
    worse bug than the one being fixed."""
    async def fine(signal):
        return None

    async def go():
        e = _engine(fine)
        task = asyncio.create_task(e._run())
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return task.cancelled()

    assert asyncio.run(go())


def test_the_engine_reports_what_it_last_consumed():
    """A stall has to be visible from outside. Without this the only
    symptom is an absence -- signals that quietly never arrive."""
    async def fine(signal):
        return None

    e = _engine(fine)
    assert getattr(e, "last_consumed_at", None) in (None, 0), (
        "expected the engine to start with no consume timestamp"
    )
    _run(e, [_signal(1)])
    assert getattr(e, "last_consumed_at", 0), (
        "the engine does not record when it last took a signal off the "
        "queue, so a stopped loop cannot be told from a quiet master"
    )


# ------------------------------------------------- the asymmetry --------

def test_the_consume_loop_is_no_less_careful_than_the_background_task():
    """_capture_fill's task already catches Exception and logs it. The
    consume loop catching less is the asymmetry that made four stalls
    invisible, and it is worth asserting rather than remembering."""
    import inspect

    src = inspect.getsource(AutonomousCopyEngine._run)
    assert "except Exception" in src, (
        "the consume loop still catches only AdapterError, so any other "
        "exception ends the task with nothing written anywhere"
    )
    assert "logger.exception" in src or "exc_info" in src, (
        "the loop logs without a stack"
    )
