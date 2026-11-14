"""The two properties the reporter exists to guarantee.

1. A dead or slow hub does not cost one copy. Reporting is a convenience;
   copying is the job. Everything the reporter needs is written to a local
   queue by the thing that produced it, and nothing in the signal path ever
   waits for the network.

2. A restart neither loses nor doubles an event. The queue is on disk, the
   event's id is stored with it, and only the ids the hub actually acked are
   dropped -- so an unacked event is resent with the same id it was given
   when it happened, and an acked one is never seen again.

Everything here runs against tests/stub_hub.py on a loopback port.
"""
import asyncio
import inspect
import json
import time
from pathlib import Path

import pytest

from app import trade_registry
from app.copy_engine import CopyEngine
from app.events import EventBus
from app.hub_reporter import HubReporter
from app.hub_reporter.outbox import pending_ids
from app.signal_bus import SignalBus
from tests.conftest_fakes import FakeBroker
from tests.stub_hub import StubHub, closed_port
from tests.test_hub_reporter import _drive, cfg, open_event, signal_event, state

SLAVE = "mt5-icm-1"
ACCT = "11558127"


async def _until(predicate, timeout=3.0):
    """Wait for something the engines do in their own tasks."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


def _signal(ticket, action="OPEN"):
    return json.dumps({
        "ticket": ticket, "symbol": "US2000", "action": action,
        "direction": "BUY", "lots": 1.0, "price": 2819.0, "sl": 2684.0,
        "atr": 45.0, "magic": 1001, "account": ACCT, "ts": time.time(),
    }).encode("utf-8")


# ============================================================ 1. off the path


async def _copy_three(url, first_ticket, timeout_s=1.0):
    """Copy three signals, with or without a reporter, and time it.

    `url=None` means no reporter at all, which is the baseline the
    with-reporter run is compared against.
    """
    events = EventBus()
    bus = SignalBus(events=events)
    broker = FakeBroker()
    queue = bus.register_slave(SLAVE)
    engine = CopyEngine(SLAVE, broker, queue, {}, events=events)

    reporter = None
    if url is not None:
        reporter = HubReporter(
            events, state(),
            cfg(url, timeout_s=timeout_s, snapshot_interval_s=0.05,
                debounce_s=0.0),
        )
        await reporter.start()
        assert reporter.running, "the reporter is genuinely trying"

    engine.start()
    started = time.monotonic()
    tickets = [first_ticket + n for n in range(3)]
    for ticket in tickets:
        await bus.publish_raw(_signal(ticket))
    copied = await _until(lambda: len(broker.opened) == 3)
    elapsed = time.monotonic() - started

    tried = queued = True
    if reporter is not None:
        # Only after the copying has been timed, so these assertions are
        # about a reporter that really ran rather than one that had not
        # got going yet.
        tried = await _until(lambda: reporter.consecutive_failures >= 1)
        queued = await _until(lambda: len(pending_ids()) == 6)

    await engine.stop()
    if reporter is not None:
        await reporter.stop()
    return copied, elapsed, broker, reporter, tried, queued, tickets


@pytest.mark.parametrize("hub_state", ["slow", "dead"])
def test_copying_is_unaffected_by_a_hub_that_does_not_answer(hub_state):
    """Three signals copied in full while the reporter gets nowhere.

    "slow" is a hub that accepts the connection and then sits on it for
    longer than the reporter's whole timeout -- the worse case, because a
    naive implementation blocks there rather than failing fast. "dead" is
    nothing listening at all.

    The timing claim is COMPARATIVE, and it took three attempts to get
    there. An absolute budget cannot work: the first version allowed 0.5s
    against a 0.2s timeout and CI failed it at 0.56s; the second allowed
    1.5s against a 1.0s timeout and CI failed it at 1.516s. Both times the
    gap between "waited for a report" and "slow shared runner" was smaller
    than the noise, so the test could not tell them apart in EITHER
    direction -- it would have passed just as readily with a real
    regression in it.

    Copying the same three signals twice, once with no reporter at all,
    removes machine speed from the claim. If the signal path awaited
    reports, the second run would be about three timeouts slower than the
    first. Anything inside the allowance below cannot have waited for even
    one, however slow the machine is.
    """
    with StubHub(delay=3.0) as slow:
        url = slow.url if hub_state == "slow" else f"http://127.0.0.1:{closed_port()}"

        async def go():
            # Both runs share one event loop: a Queue binds to the loop it
            # is first used on, so two asyncio.run() calls would fail with
            # "bound to a different event loop".
            #
            # Different ticket numbers because the registry keys on them
            # and the second run must not collide with the first.
            baseline = await _copy_three(None, 201)
            measured = await _copy_three(url, 101)
            return baseline, measured

        baseline, measured = asyncio.run(go())

    b_copied, b_elapsed, *_ = baseline
    copied, elapsed, broker, reporter, tried, queued, tickets = measured

    assert b_copied and copied, "every signal was copied, both times"
    assert [o["symbol"] for o in broker.opened] == ["US2000"] * 3
    assert [o["size"] for o in broker.opened] == [1.0] * 3

    # One awaited report costs a full timeout, so three cost about 3s. Half
    # a second of allowance catches that six times over while absorbing any
    # plausible difference between two runs on the same machine.
    overhead = elapsed - b_elapsed
    assert overhead < 0.5, (
        f"copying took {elapsed:.2f}s with a dead reporter against "
        f"{b_elapsed:.2f}s without one -- {overhead:.2f}s of it was spent "
        f"waiting on the reporter"
    )

    for ticket in tickets:
        assert trade_registry.get_open_mappings(SLAVE, ACCT, ticket), (
            f"ticket {ticket} is not in the registry"
        )

    # And the events are all still queued, because nothing was ever acked.
    assert tried, "the reporter never actually attempted a report"
    assert reporter.reports_sent == 0
    assert queued, "3 signal_received + 3 slave_open should be held"


def test_the_signal_path_never_waits_for_the_sender():
    """`emit` hands the event over and returns. It does not do the work.

    Checked structurally rather than by a stopwatch: after fifty emits with
    no chance for any other task to run, the outbox is still empty -- the
    translation and the database write belong to the reporter's own task.
    """
    with StubHub(delay=1.0) as hub:

        async def go():
            events = EventBus()
            reporter = HubReporter(
                events, state(), cfg(hub.url, timeout_s=0.2, debounce_s=0.0))
            await reporter.start()

            started = time.monotonic()
            for ticket in range(50):
                await events.emit(open_event(master_ticket=ticket))
            emitting = time.monotonic() - started
            during = len(pending_ids())

            drained = await _until(lambda: len(pending_ids()) == 50)
            await reporter.stop()
            return emitting, during, drained

        emitting, during, drained = asyncio.run(go())

    assert during == 0, "emit wrote to the database on the caller's time"
    assert emitting < 0.1, f"fifty emits took {emitting:.2f}s"
    assert drained, "the reporter's own task picked them up afterwards"


def test_an_ingest_failure_does_not_end_the_reporter():
    """A single bad event must not take reporting down with it."""
    with StubHub() as hub:

        async def go():
            events = EventBus()
            reporter = HubReporter(events, state(), cfg(hub.url, debounce_s=0.0))
            await reporter.start()
            # `at` is required by the outbox's insert; a None type is the
            # shape of an event nobody should ever emit.
            await events.emit({"type": None, "slave_id": "s1"})
            await events.emit(open_event())
            ok = await _until(lambda: len(pending_ids()) == 1)
            running = reporter.running and not any(t.done() for t in reporter._tasks)
            await reporter.stop()
            return ok, running

        ok, running = asyncio.run(go())

    assert ok, "the good event still reached the outbox"
    assert running, "the ingest task survived the bad one"


# ======================================================= 2. across a restart


def test_a_restart_neither_loses_nor_doubles_events():
    """Queue five, send five, ack two, restart, and check what travels.

    The acked two are gone for good. The unacked three come back with the
    event ids they were given when they happened -- ids carrying the OLD
    session, because the id belongs to the occurrence and not to the run
    that sent it. That is what makes the hub's dedupe on event_id work: a
    reply lost in transit causes a resend of events it already stored, and
    that has to be harmless.
    """
    acked_first_two = []

    def ack(report):
        ids = [e["event_id"] for e in report["events"]][:2]
        acked_first_two.extend(ids)
        return ids

    with StubHub(ack=ack) as hub:
        before = HubReporter(EventBus(), state(), cfg(hub.url))
        queued = [before.ingest_one(signal_event(ticket=t)) for t in range(5)]
        assert _drive(before) == [True]
        assert [e for _, e in pending_ids()] == queued[2:]

        # The restart: a new process, a new session, the same database.
        hub.ack = "all"
        after = HubReporter(EventBus(), state(), cfg(hub.url))
        assert after.session_id != before.session_id

        first_report_index = len(hub.reports)
        assert _drive(after, cycles=2) == [True, True]

    resent = [
        e["event_id"]
        for report in hub.reports[first_report_index:]
        for e in report["events"]
    ]
    assert resent == queued[2:], "resent exactly once, oldest first"
    assert all(i.split(":")[1] == before.session_id for i in resent), (
        "an event keeps the id it was given when it happened"
    )
    for lost in acked_first_two:
        assert lost not in resent, "an acked event is never sent again"
    assert pending_ids() == [], "and the backlog is now empty"


def test_a_new_session_cannot_collide_with_a_surviving_event():
    """`n` restarts at 1 every session, so the session id is what separates."""
    with StubHub(ack="none") as hub:
        before = HubReporter(EventBus(), state(), cfg(hub.url))
        survivor = before.ingest_one(signal_event())
        _drive(before)

        after = HubReporter(EventBus(), state(), cfg(hub.url))
        fresh = after.ingest_one(signal_event())

    assert survivor.endswith(":1") and fresh.endswith(":1")
    assert survivor != fresh
    assert [e for _, e in pending_ids()] == [survivor, fresh]


def test_a_hub_coming_back_gets_the_whole_backlog_and_a_current_snapshot():
    """Back-off or not, the snapshot rides along with every attempt."""
    with StubHub(status=503) as hub:
        reporter = HubReporter(
            EventBus(), state(), cfg(hub.url, max_events_per_report=2))
        queued = [reporter.ingest_one(signal_event(ticket=t)) for t in range(3)]
        assert _drive(reporter, cycles=2) == [False, False]
        assert [e for _, e in pending_ids()] == queued

        hub.status = 200
        assert _drive(reporter, cycles=2) == [True, True]

    assert pending_ids() == []
    # Four attempts while it was down or recovering, each carrying a
    # status: a hub that comes back is current immediately.
    assert all("status" in r for r in hub.reports)
    # And the rows that failed to land are sent again rather than skipped:
    # the watermark only advances on a report the hub accepted.
    assert all("orders" in r and "legs" in r for r in hub.reports)
    assert [r["copier"]["seq"] for r in hub.reports] == [1, 2, 3, 4]


# ================================================= the loop, end to end


def test_one_signals_legs_travel_together():
    """Debounced so a fan-out is one report, not one per leg.

    The alternative is three reports a second apart for one signal, which
    is both more requests than the hub needs and harder to read in the tab
    than the thing it describes.
    """
    with StubHub() as hub:

        async def go():
            events = EventBus()
            reporter = HubReporter(
                events, state(),
                cfg(hub.url, snapshot_interval_s=5.0, debounce_s=0.15),
            )
            await reporter.start()
            await events.emit(signal_event())
            for slave in ("ibkr-paper-1", "ibkr-etf-1", SLAVE):
                await events.emit(open_event(slave_id=slave))
            sent = await _until(lambda: reporter.reports_sent >= 1)
            await reporter.stop()
            return sent

        assert asyncio.run(go()), "the reporter never sent anything"

    assert len(hub.reports) == 1, "one report, not four"
    assert [e["type"] for e in hub.reports[0]["events"]] == [
        "signal_received", "slave_open", "slave_open", "slave_open",
    ]
    assert pending_ids() == []


def test_a_quiet_copier_still_sends_a_snapshot():
    """So the tab can tell "quiet" from "dead"; events alone cannot."""
    with StubHub() as hub:

        async def go():
            reporter = HubReporter(
                EventBus(), state(), cfg(hub.url, snapshot_interval_s=0.05))
            await reporter.start()
            sent = await _until(lambda: reporter.reports_sent >= 2)
            await reporter.stop()
            return sent

        assert asyncio.run(go())

    assert all(r["events"] == [] for r in hub.reports)
    assert hub.reports[0]["copier"]["uptime_s"] >= 0


def test_a_backlog_deeper_than_one_report_drains_without_waiting():
    """Otherwise a backlog clears at max_events_per_report per interval."""
    with StubHub() as hub:

        async def go():
            events = EventBus()
            reporter = HubReporter(
                events, state(),
                cfg(hub.url, snapshot_interval_s=30.0, debounce_s=0.0,
                    max_events_per_report=2),
            )
            await reporter.start()
            for ticket in range(6):
                await events.emit(open_event(master_ticket=ticket))
            # Queued first, then drained. Asked the other way round the
            # wait is satisfied instantly by an outbox that is empty only
            # because the reporter has not looked at the bus yet.
            queued = await _until(lambda: len(pending_ids()) == 6)
            drained = await _until(lambda: not pending_ids())
            await reporter.stop()
            return queued, drained, reporter.reports_sent

        queued, drained, sent = asyncio.run(go())

    assert queued, "the six events never reached the outbox"
    assert drained, "the backlog was still waiting for the 30s interval"
    assert sent >= 3, "three reports of two, not one report and a long wait"
    assert len(hub.event_ids()) == 6


def test_stop_releases_the_bus_subscription():
    events = EventBus()
    with StubHub() as hub:

        async def go():
            reporter = HubReporter(events, state(), cfg(hub.url))
            await reporter.start()
            assert len(events._subscribers) == 1
            await reporter.stop()
            return reporter

        reporter = asyncio.run(go())

    assert events._subscribers == []
    assert reporter.running is False


def test_the_reporter_is_wired_into_the_apps_lifespan():
    """It starts and stops with the engines, not on its own schedule."""
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    assert "HubReporter(events, app.state)" in source
    assert "await reporter.start()" in source
    assert "await reporter.stop()" in source
    assert source.index("await reporter.stop()") < source.index("await master_watchdog.stop()"), (
        "the reporter stops before the engines it reports on"
    )


def test_nothing_on_the_signal_path_knows_the_reporter_exists():
    """A guard on the rule, not a restatement of it.

    The engines, the bus and the API route emit onto the event bus and know
    nothing about who is listening. If one of them ever imports the
    reporter, the next person to add a call there has no way of telling
    that the call is on the signal path.
    """
    from app import api_routes, autonomous_copy_engine, copy_engine, signal_bus

    for module in (api_routes, copy_engine, autonomous_copy_engine, signal_bus):
        assert "hub_reporter" not in inspect.getsource(module), (
            f"{module.__name__} imports the reporter; it must not know it exists"
        )


def test_the_stub_is_the_only_hub_these_tests_talk_to():
    """No test here may reach the real Operations hub.

    There is no copier route and no key yet, so a test that needed one
    would be a test nobody could run -- and a suite that could post to
    production is not a mistake worth making once.
    """
    # Assembled rather than written out, so this test does not fail on its
    # own source.
    production = "srsystems" + ".duckdns.org"
    for path in Path(__file__).parent.glob("test_hub_reporter*.py"):
        assert production not in path.read_text(encoding="utf-8"), path.name
