"""Each master reports the build it is running.

Written after an afternoon spent working out which binary was where. A
bridge was deployed to the wrong terminal because its folder was named
"MT4-Master-ICMarkets" while the master in use was the one named
"MT4-Slave-ICMarkets"; a fix was believed live that was not; and three
different .ex4 files of one EA existed with nothing on any screen to tell
them apart. The only way to answer "which build is this master running"
was to hash files.

At four to eight masters that is not an annoyance, it is unanswerable --
so the master says, in every heartbeat.

On the HEARTBEAT and not the signal, deliberately: it arrives every few
seconds whether or not anything trades, so the version of a master that
has been quiet all session is known. That is exactly the master somebody
is asking about.
"""
import asyncio
import json

from app.zmq_receiver import ZmqReceiver


class _Pull:
    """A PULL socket that yields a fixed set of heartbeats then blocks."""

    def __init__(self, messages):
        self._messages = list(messages)

    async def recv(self):
        if self._messages:
            return self._messages.pop(0).encode("utf-8")
        await asyncio.sleep(3600)   # nothing more; the task is cancelled


def _drain(receiver, messages):
    async def go():
        receiver.pull_sock = _Pull(messages)
        task = asyncio.create_task(receiver._pull_loop())
        for _ in range(50):
            await asyncio.sleep(0.01)
            if not receiver.pull_sock._messages:
                break
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())


def test_a_heartbeat_carries_the_build():
    r = ZmqReceiver(bus=None)
    _drain(r, [json.dumps({"account": "44964870", "bridge_version": "1.04"})])
    assert r.bridge_version["44964870"] == "1.04"
    assert "44964870" in r.last_heartbeat


def test_an_older_bridge_simply_does_not_appear():
    """Pre-1.04 sends no version. Absent is the honest answer; inventing
    one would be the same mistake this exists to stop."""
    r = ZmqReceiver(bus=None)
    _drain(r, [json.dumps({"account": "44964870"})])
    assert r.bridge_version.get("44964870") is None
    assert "44964870" in r.last_heartbeat, "the heartbeat still counts"


def test_two_masters_are_tracked_apart():
    r = ZmqReceiver(bus=None)
    _drain(r, [
        json.dumps({"account": "44964870", "bridge_version": "1.04"}),
        json.dumps({"account": "11558127", "bridge_version": "1.03"}),
    ])
    assert r.bridge_version == {"44964870": "1.04", "11558127": "1.03"}


def test_a_master_that_was_recompiled_updates():
    """Somebody reattached a new build under a running hub. That is the
    moment behaviour changes, so the value must follow rather than stick
    at whatever was seen first."""
    r = ZmqReceiver(bus=None)
    _drain(r, [
        json.dumps({"account": "44964870", "bridge_version": "1.03"}),
        json.dumps({"account": "44964870", "bridge_version": "1.04"}),
    ])
    assert r.bridge_version["44964870"] == "1.04"


def test_a_malformed_heartbeat_does_not_take_the_loop_down():
    """The receiver reads whatever arrives on a bound socket. A bad frame
    must not stop the good ones behind it."""
    r = ZmqReceiver(bus=None)
    _drain(r, [
        "{not json at all",
        json.dumps({"account": "44964870", "bridge_version": "1.04"}),
    ])
    assert r.bridge_version["44964870"] == "1.04"


def test_the_source_and_the_reported_version_agree():
    """#property version and BRIDGE_VERSION are kept in step by hand --
    MQL cannot read the property from code -- and they had already
    drifted: the property said 1.04 while the define said 1.03, which
    would have had every master reporting the wrong build through the
    handshake added to stop exactly that.
    """
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent.parent / "bridge_ea" / "TradeCopierBridge.mq4"
    if not src.is_file():
        return  # the EA is not part of every checkout

    text = src.read_text(encoding="utf-8", errors="replace")
    prop = re.search(r'#property\s+version\s+"([^"]+)"', text)
    define = re.search(r'#define\s+BRIDGE_VERSION\s+"([^"]+)"', text)
    assert prop and define, "both version declarations should be present"
    assert prop.group(1) == define.group(1), (
        f"#property version is {prop.group(1)} but BRIDGE_VERSION is "
        f"{define.group(1)} -- every master would report the wrong build"
    )
