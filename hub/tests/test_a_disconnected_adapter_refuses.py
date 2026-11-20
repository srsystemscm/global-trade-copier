"""A dropped IBKR session must come back as a refusal, not an AssertionError.

This is the other half of test_the_consume_loop_survives.py, and the
cause rather than the consequence.

Every consume-path method on IBKRAdapter used to begin:

    assert self.ib is not None, "adapter not connected"

disconnect() sets self.ib = None, and a dropped session calls it. So any
call already in flight -- a close, a quote, an account read, a margin
preview -- raised AssertionError the moment the session went. An
AssertionError is not an AdapterError, and AutonomousCopyEngine._run
caught only AdapterError, so it escaped the loop and ended that slave's
consume task for the life of the process. Nothing logged it: asyncio
only reports an un-retrieved task exception when the task is collected,
and the engine holds the reference forever.

That is the whole of the 2026-11-16 symptom. It explains why the stalls
followed abandoned entries -- cancel round trips and timeouts are when a
session blip is most likely to land mid-call -- and why the adapter read
connected: true afterwards. The adapter reconnected. The loop did not.

Assertions are also the wrong tool here on their own terms: `python -O`
strips them, which would have turned the identical disconnect into
AttributeError on None. Equally not an AdapterError, equally fatal.
"""
import inspect

import pytest

from app.adapters.base import AdapterConnectionError, AdapterError
from app.adapters.ibkr import IBKRAdapter


def _disconnected():
    """An adapter whose session has gone, which is what disconnect()
    leaves behind: a live object with self.ib set back to None."""
    a = object.__new__(IBKRAdapter)
    a.ib = None
    a.slave_id = "ibkr-paper-1"
    a.account = "DU0000000"
    a._positions = {}
    a._entry_trades = {}
    a._entry_waits = {}
    a._exit_trades = {}
    a.quote_retries = 1
    return a


# Every one of these is reachable from the consume loop. The arguments
# do not matter: each guard sits ahead of any use of them, which is the
# point of a guard.
CONSUME_PATH = [
    ("open", (None,)),
    ("close", (1,)),
    ("modify", (1, 1.0, 2.0)),
    ("preview_margin", (None,)),
    ("get_account_summary", ("live",)),
    ("get_price_history", ("XAUUSD", 20)),
    ("get_quote", ("XAUUSD",)),
    ("get_working_orders", ()),
]


@pytest.mark.parametrize("name,args", CONSUME_PATH, ids=[n for n, _ in CONSUME_PATH])
def test_a_disconnected_adapter_raises_an_adapter_error(name, args):
    """THE BUG. Not "it raises something" -- it has to raise the family
    the engine handles, or the engine dies instead of refusing."""
    adapter = _disconnected()
    with pytest.raises(AdapterError) as caught:
        import asyncio
        asyncio.run(getattr(adapter, name)(*args))
    assert isinstance(caught.value, AdapterConnectionError), (
        f"{name}() refused with {type(caught.value).__name__}; the engine "
        "treats a connection failure differently from a broker rejection"
    )


@pytest.mark.parametrize("name,args", CONSUME_PATH, ids=[n for n, _ in CONSUME_PATH])
def test_a_disconnected_adapter_never_raises_assertionerror(name, args):
    """Stated separately because this is the exact thing that killed four
    engines, and a later refactor could reintroduce it while the test
    above still passed on some other path."""
    adapter = _disconnected()
    try:
        import asyncio
        asyncio.run(getattr(adapter, name)(*args))
    except AssertionError as exc:
        pytest.fail(
            f"{name}() still raises AssertionError ({exc}) when the session "
            "has dropped -- that is not an AdapterError, so it escapes the "
            "consume loop and ends the slave silently"
        )
    except Exception:
        pass


def test_assertionerror_is_not_an_adapter_error():
    """The one-line reason the asserts were fatal. Worth pinning: if
    AdapterError ever grew to cover it, the tests above would start
    passing for the wrong reason."""
    assert not issubclass(AssertionError, AdapterError)


def test_no_connection_guard_is_left_as_a_bare_assert():
    """Fourteen of these existed. A new method copying the old shape
    would be silently fatal again, and nothing else would catch it."""
    src = inspect.getsource(IBKRAdapter)
    offenders = [
        line.strip()
        for line in src.splitlines()
        if line.strip().startswith("assert self.ib")
    ]
    assert not offenders, (
        "connection guards must raise AdapterConnectionError, not assert: "
        f"{offenders}"
    )
