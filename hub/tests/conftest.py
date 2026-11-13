import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402  (must follow the sys.path insert)

from app import db  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Give every test its own empty database.

    Without this the suite wrote into hub/data/tradecopier.db -- the live
    registry the running hub reconciles against. A stray `s1` row left at
    OPEN by an engine test was found sitting in it, which is exactly the
    shape of row reconciliation treats as a real open position.

    Patching `settings.db_path` is enough because db.get_connection() reads
    it per call rather than holding a connection open.
    """
    monkeypatch.setattr(settings, "db_path", tmp_path / "tradecopier.db")
    db.init_db()
    yield


# --------------------------------------------------------------------------
# Say WHICH task hung, not just which test.
#
# The suite hangs intermittently on CI -- roughly half of runs, in a
# different test each time, always one driving an engine through
# asyncio.run(). pytest-timeout kills the process and dumps every THREAD's
# stack, which for an asyncio hang says only "the event loop is idle": true,
# unhelpful, and the same for every possible cause.
#
# What is missing is the tasks. This dumps them just before pytest-timeout
# fires, so the next occurrence names the coroutine that ignored its
# cancellation instead of leaving it to be guessed at.
#
# Tasks are found through the garbage collector rather than
# asyncio.all_tasks(), which needs the running loop and cannot be called
# from this timer's thread. `Task.print_stack()` needs no loop.

def _dump_asyncio_tasks() -> None:
    import asyncio as _asyncio
    import gc

    print("\n======== asyncio tasks, just before the timeout ========",
          file=sys.stderr, flush=True)
    try:
        tasks = [o for o in gc.get_objects() if isinstance(o, _asyncio.Task)]
    except Exception as exc:                       # pragma: no cover
        print(f"  could not walk the heap: {exc}", file=sys.stderr, flush=True)
        return

    alive = [t for t in tasks if not t.done()]
    print(f"  {len(tasks)} task object(s), {len(alive)} not done",
          file=sys.stderr, flush=True)
    for task in alive:
        try:
            print(f"\n  --- {task!r}", file=sys.stderr, flush=True)
            print(f"      {_cancel_state(task)}", file=sys.stderr, flush=True)
            task.print_stack(limit=15, file=sys.stderr)
        except Exception as exc:                   # pragma: no cover
            print(f"  <could not dump {task}: {exc}>", file=sys.stderr, flush=True)

    _dump_loop_state(alive)
    print("======== end asyncio tasks ========\n", file=sys.stderr, flush=True)


def _cancel_state(task) -> str:
    """The internals that say whether a cancellation was actually delivered.

    A healthy cancel leaves `_fut_waiter` CANCELLED -- verified directly:
    before, `<Future pending cb=[Task.task_wakeup()]>`; after,
    `<Future cancelled>`.

    The hang under investigation showed a task whose repr said
    `cancelling` -- which in CPython means `_must_cancel is True` and
    nothing else -- while the future it was suspended on was still
    PENDING. That state should not be reachable: Task.cancel() cancels
    `_fut_waiter` when it has one, and Task.__step cancels the new one
    when `_must_cancel` is still set afterwards.

    So print the numbers that separate the possibilities instead of
    inferring them from a repr.
    """
    bits = []
    try:
        bits.append(f"cancelling()={task.cancelling()}")
    except Exception:                              # pragma: no cover
        bits.append("cancelling()=?")
    bits.append(f"_must_cancel={getattr(task, '_must_cancel', '?')}")

    waiter = getattr(task, "_fut_waiter", None)
    if waiter is None:
        bits.append("_fut_waiter=None (so __step must already be scheduled)")
    else:
        try:
            bits.append(f"_fut_waiter done={waiter.done()} "
                        f"cancelled={waiter.cancelled()}")
        except Exception:                          # pragma: no cover
            bits.append("_fut_waiter=<unreadable>")
    return "  ".join(bits)


def _dump_loop_state(tasks) -> None:
    """Whether the loop has anything left to run.

    If a task is waiting for a cancellation it has not received, and the
    loop has no ready callbacks and no timers due, then nothing will ever
    wake it. That is the difference between a wakeup that was LOST and one
    that is merely late -- and it is not visible in a task repr.
    """
    loops = []
    for task in tasks:
        try:
            loop = task.get_loop()
        except Exception:                          # pragma: no cover
            continue
        if not any(loop is seen for seen in loops):
            loops.append(loop)

    for loop in loops:
        ready = getattr(loop, "_ready", None)
        scheduled = getattr(loop, "_scheduled", None)
        print(f"\n  loop {loop!r}"
              f"\n    running={loop.is_running()} closed={loop.is_closed()}"
              f"\n    ready callbacks="
              f"{len(ready) if ready is not None else '?'}"
              f"  scheduled timers="
              f"{len(scheduled) if scheduled is not None else '?'}",
              file=sys.stderr, flush=True)


def pytest_timeout_set_timer(item, settings):
    """Fire our own dump shortly before pytest-timeout kills the process.

    Returning None rather than True, so pytest-timeout still installs its
    own timer and still does the killing -- this only adds information.
    """
    timeout = getattr(settings, "timeout", None)
    if not timeout:
        return None
    import threading

    lead = min(10.0, max(1.0, timeout * 0.1))
    timer = threading.Timer(max(0.1, timeout - lead), _dump_asyncio_tasks)
    timer.daemon = True
    timer.start()
    item._asyncio_task_dump_timer = timer
    return None


def pytest_timeout_cancel_timer(item):
    timer = getattr(item, "_asyncio_task_dump_timer", None)
    if timer is not None:
        timer.cancel()
        del item._asyncio_task_dump_timer
    return None
