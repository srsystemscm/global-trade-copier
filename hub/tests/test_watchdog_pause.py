"""A paused slave shouldn't be health-checked.

Pausing is deliberate, so reporting it "disconnected" is noise about a
condition the operator created. On MT4 each failed PING also costs three
retries with backoff, which buried real signals in the log during testing.
"""
import asyncio

from app.events import EventBus
from app.notifier import Notifier
from app.watchdog import SlaveWatchdog


class _Adapter:
    def __init__(self, ok=True):
        self.ok = ok
        self.status_calls = 0

    async def get_status(self):
        self.status_calls += 1
        return self.ok

    async def get_account_summary(self, account_type):
        raise NotImplementedError


def _watchdog(adapter, paused_flag):
    return SlaveWatchdog(
        "s1", adapter, EventBus(), Notifier(),
        account_types=[None], poll_interval=0.01, drawdown_alert_pct=0.05,
        is_paused=lambda: paused_flag["paused"],
    )


def _run(adapter, flag, ticks=4):
    async def go():
        w = _watchdog(adapter, flag)
        w.start()
        await asyncio.sleep(0.01 * ticks + 0.05)
        await w.stop()
        return w
    return asyncio.run(go())


def test_paused_slave_is_not_polled():
    adapter = _Adapter()
    _run(adapter, {"paused": True})
    assert adapter.status_calls == 0


def test_unpaused_slave_is_polled():
    adapter = _Adapter()
    _run(adapter, {"paused": False})
    assert adapter.status_calls > 0


def test_pausing_clears_last_known_state():
    """So the first poll after resume re-baselines silently rather than
    emitting a transition for time the slave spent deliberately idle."""
    adapter = _Adapter()
    w = _run(adapter, {"paused": True})
    assert w.connected is None


def test_events_are_not_emitted_while_paused():
    """A paused slave that is genuinely unreachable must stay quiet."""
    adapter = _Adapter(ok=False)
    emitted = []

    async def go():
        w = _watchdog(adapter, {"paused": True})
        w.events.emit = lambda e: emitted.append(e)  # type: ignore[assignment]
        w.start()
        await asyncio.sleep(0.1)
        await w.stop()

    asyncio.run(go())
    assert emitted == []
    assert adapter.status_calls == 0


def test_default_is_never_paused():
    """Watchdogs built without the hook keep their old behaviour."""
    w = SlaveWatchdog("s1", _Adapter(), EventBus(), Notifier(),
                      account_types=[None], poll_interval=1, drawdown_alert_pct=0.05)
    assert w.is_paused() is False
