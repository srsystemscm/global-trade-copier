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
            task.print_stack(limit=15, file=sys.stderr)
        except Exception as exc:                   # pragma: no cover
            print(f"  <could not dump {task}: {exc}>", file=sys.stderr, flush=True)
    print("======== end asyncio tasks ========\n", file=sys.stderr, flush=True)


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
