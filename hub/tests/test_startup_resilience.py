"""One unreachable broker must not stop the hub.

Slave startup was a bare list comprehension, so a single adapter raising at
connect aborted the whole thing. That is not a rare case on a VPS: the hub
and IB Gateway both start at boot and the hub routinely wins the race, and
Gateway's forced daily restart presents the same situation every day. The
cost of getting it wrong is that a dead IBKR session stops the MT4 and MT5
legs copying too.
"""
import asyncio

from app import engine_manager
from app.events import EventBus
from app.notifier import Notifier
from app.signal_bus import SignalBus
from app.slaves import SlaveConfig


def _slave(sid):
    return SlaveConfig(id=sid, name=sid, broker_type="mt4", enabled=True,
                       paused=False, config={"host": "127.0.0.1", "port": 1})


class _RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def notify(self, kind, message, **fields):
        self.sent.append((kind, message))


def _run(monkeypatch, failing):
    started = []
    events = []

    async def fake_start(slave, bus, ev, notifier):
        if slave.id in failing:
            raise ConnectionRefusedError(f"{slave.id} broker is down")
        started.append(slave.id)
        return object()

    monkeypatch.setattr(engine_manager, "start_engine_for_slave", fake_start)

    bus_ = SignalBus()
    ev_bus = EventBus()

    async def capture(e):
        events.append(e)

    ev_bus.emit = capture  # type: ignore[assignment]
    notifier = _RecordingNotifier()

    engines = asyncio.run(engine_manager.start_engines(
        [_slave("mt5-icm-1"), _slave("ibkr-paper-1"), _slave("mt4-demo-1")],
        bus_, ev_bus, notifier))
    return engines, started, events, notifier


def test_the_other_slaves_still_start(monkeypatch):
    engines, started, _, _ = _run(monkeypatch, failing={"ibkr-paper-1"})
    assert started == ["mt5-icm-1", "mt4-demo-1"]
    assert len(engines) == 2, "a failed slave left a hole in the engine list"


def test_the_failure_is_loud(monkeypatch):
    """Silently carrying on would be worse than failing: the operator would
    believe a slave is copying when it is not."""
    _, _, events, notifier = _run(monkeypatch, failing={"ibkr-paper-1"})

    assert any(e["type"] == "slave_start_failed" and e["slave_id"] == "ibkr-paper-1"
               for e in events)
    assert any(kind == "slave_start_failed" for kind, _ in notifier.sent)


def test_every_slave_failing_still_leaves_a_running_hub(monkeypatch):
    """The hub itself must come up -- the dashboard, the master's signal
    intake and the registry are all still useful, and a slave can be fixed
    and resumed without a restart."""
    engines, started, _, _ = _run(
        monkeypatch, failing={"mt5-icm-1", "ibkr-paper-1", "mt4-demo-1"})
    assert engines == [] and started == []


def test_a_healthy_fleet_is_unaffected(monkeypatch):
    engines, started, events, notifier = _run(monkeypatch, failing=set())
    assert len(engines) == 3
    assert not [e for e in events if e["type"] == "slave_start_failed"]
    assert notifier.sent == []
