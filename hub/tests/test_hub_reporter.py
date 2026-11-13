"""The reporter's contract with the Operations hub: queue, ack, snapshot.

docs/hub-reporter.md is the agreement these assert. Every one of them runs
against tests/stub_hub.py -- a real HTTP server on a loopback port -- and
never against the live hub, which has no copier route and no key yet.
"""
import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from app.db import db_cursor, set_config_value
from app.events import EventBus
from app.hub_reporter import HubReporter
from app.hub_reporter.config import HubReporterConfig
from app.hub_reporter.outbox import DROPPED_KEY, Outbox, pending_ids
from app.slaves import insert_slave
from tests.stub_hub import StubHub, closed_port


def cfg(url: str, **kw) -> HubReporterConfig:
    base = dict(
        enabled=True, url=url, copier_id="gtc-test", key="test-key",
        snapshot_interval_s=0.05, timeout_s=1.0, debounce_s=0.0,
    )
    base.update(kw)
    return HubReporterConfig(**base)


def state(**kw) -> SimpleNamespace:
    """A stand-in for app.state: what the snapshot reads, nothing else."""
    return SimpleNamespace(
        start_time=kw.get("start_time", time.time() - 100),
        receiver=kw.get("receiver", SimpleNamespace(last_heartbeat={})),
        engines=kw.get("engines", []),
    )


def signal_event(ticket=999031902, **kw):
    event = {
        "type": "signal", "ticket": ticket, "master_ticket": ticket,
        "master_account": "11558127", "master_symbol": "US2000",
        "symbol": "US2000", "magic": 1001, "action": "OPEN",
        "direction": "BUY", "lots": 1.0, "price": 2819.0, "sl": 2684.0,
        "tp": None, "atr": 45.0, "targets": 3,
        "emitted_at": 1763064302.0,
    }
    event.update(kw)
    return event


def open_event(slave_id="ibkr-paper-1", **kw):
    event = {
        "type": "slave_open", "slave_id": slave_id,
        "master_ticket": 999031902, "master_account": "11558127",
        "master_symbol": "US2000", "magic": 1001,
        "slave_symbol": "M2K", "slave_ticket": 392, "size": 24.0,
        "sizing": "pct_risk on M2K: ... -> 24 lots",
        "requested_price": 2808.8, "fill_price": 2808.8,
        "sl": 2674.2, "tp": None, "legs": 1,
        "latency_ms": 1180, "signal_to_fill_ms": 2140,
        "emitted_at": 1763064304.0,
    }
    event.update(kw)
    return event


def _drive(reporter, cycles=1):
    """Run `cycles` send cycles against whatever the reporter is pointed at.

    The sender loop itself is not run: these assert on what one cycle does,
    and a loop would make the timing of the assertions the test's problem.
    """

    async def go():
        reporter._client = httpx.AsyncClient(timeout=1.0)
        try:
            return [await reporter._send_once() for _ in range(cycles)]
        finally:
            await reporter._client.aclose()

    return asyncio.run(go())


# --------------------------------------------------------------- the shape


def test_a_report_carries_the_shape_the_hub_agreed_to():
    with StubHub() as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        reporter.ingest_one(signal_event())
        reporter.ingest_one(open_event())
        assert _drive(reporter) == [True]

    assert len(hub.reports) == 1
    report = hub.reports[0]
    assert report["schema_version"] == "1.0"
    assert report["copier_id"] == "gtc-test"
    assert report["session_id"] == reporter.session_id
    assert report["seq"] == 1
    assert report["sent_at"].endswith("Z")
    assert hub.auth[0] == "Bearer test-key"

    snapshot = report["snapshot"]
    assert set(snapshot) == {"copier", "masters", "slaves", "risk", "counters", "queue"}
    assert snapshot["copier"]["http_port"]
    assert snapshot["copier"]["started_at"].endswith("Z")
    assert snapshot["counters"]["since_start"] == {
        "received": 1, "copied": 1, "refused": 0, "failed": 0
    }

    received, opened = report["events"]
    assert received["type"] == "signal_received"
    assert received["event_id"] == f"gtc-test:{reporter.session_id}:1"
    assert received["at"] == "2025-11-13T20:05:02Z"
    assert received["slave_id"] is None
    assert received["detail"]["targets"] == 3

    assert opened["type"] == "slave_open"
    assert opened["master_account"] == "11558127"
    assert opened["master_ticket"] == 999031902
    assert opened["magic"] == 1001, "magic is how a copy is attributed to a strategy"
    assert opened["master_symbol"] == "US2000"
    assert opened["slave_id"] == "ibkr-paper-1"
    assert opened["detail"]["sizing"].startswith("pct_risk")
    assert opened["detail"]["signal_to_fill_ms"] == 2140


def test_the_snapshot_is_never_queued():
    """It is what is true now, so a lost one is replaced by the next."""
    with StubHub() as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        # Three cycles with nothing happening at all.
        assert _drive(reporter, cycles=3) == [True, True, True]

    assert [r["seq"] for r in hub.reports] == [1, 2, 3]
    assert all(r["events"] == [] for r in hub.reports)
    assert pending_ids() == [], "a snapshot must never reach the outbox"


# ----------------------------------------------------------- ack semantics


def test_only_the_acked_ids_are_dropped():
    """A hub that stored eight of ten has to be able to say so."""
    with StubHub(ack=lambda report: [report["events"][0]["event_id"]]) as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        first = reporter.ingest_one(signal_event())
        second = reporter.ingest_one(open_event())
        third = reporter.ingest_one(open_event(slave_id="mt5-icm-1"))

        _drive(reporter)

    assert [e for _, e in pending_ids()] == [second, third]
    assert hub.event_ids() == [first, second, third]


@pytest.mark.parametrize(
    "stub, why",
    [
        (dict(ack="none"), "a hub that acks nothing keeps the whole backlog"),
        (dict(status=500), "a non-200 acks nothing"),
        (dict(status=422), "a rejected schema version acks nothing"),
        (dict(body="not json at all"), "a 200 with an unreadable body acks nothing"),
        (dict(body='{"ok": true, "acked_event_ids": "417"}'), "a malformed ack list acks nothing"),
    ],
)
def test_a_reply_that_acks_nothing_leaves_the_queue_intact(stub, why):
    with StubHub(**stub) as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        queued = [reporter.ingest_one(signal_event()), reporter.ingest_one(open_event())]
        _drive(reporter)

    assert [e for _, e in pending_ids()] == queued, why
    assert len(hub.reports) == 1, "and never a retry inside one cycle"


def test_a_200_that_says_nothing_about_events_is_not_a_failure():
    """The hub may answer a snapshot-only report without an ack list."""
    with StubHub(body='{"ok": true, "schema_version": "1.0"}') as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        queued = reporter.ingest_one(signal_event())
        assert _drive(reporter) == [True]

    assert [e for _, e in pending_ids()] == [queued]
    assert reporter._backoff == 0.0
    assert len(hub.reports) == 1


def test_at_most_max_events_per_report_oldest_first():
    with StubHub() as hub:
        reporter = HubReporter(
            EventBus(), state(), cfg(hub.url, max_events_per_report=2))
        queued = [reporter.ingest_one(signal_event(ticket=t)) for t in range(5)]
        _drive(reporter, cycles=3)

    assert hub.event_ids() == queued, "oldest first, each exactly once"
    assert [len(r["events"]) for r in hub.reports] == [2, 2, 1]


# ------------------------------------------------------------ bounded queue


def test_the_queue_is_bounded_and_counts_what_it_drops():
    """At the ceiling the OLDEST go, and the loss is visible in the snapshot.

    Dropping the oldest is the right way round: a backlog this deep means
    the hub has been unreachable for a long time, and the events somebody is
    about to look at are the recent ones.
    """
    reporter = HubReporter(EventBus(), state(), cfg("http://127.0.0.1:1", queue_max=3))
    queued = [reporter.ingest_one(signal_event(ticket=t)) for t in range(5)]

    assert [e for _, e in pending_ids()] == queued[2:]
    stats = reporter.outbox.stats()
    assert stats["depth"] == 3
    assert stats["dropped_total"] == 2
    assert stats["oldest_event_at"] == "2025-11-13T20:05:02Z"


def test_dropped_total_survives_a_restart():
    """A counter that reset on restart would hide the losses a restart follows."""
    first = Outbox("gtc-test", "aaaa1111", queue_max=2)
    for t in range(4):
        first.queue({"type": "signal_received", "at": "2026-11-12T20:05:02Z"})
    assert first.dropped_total == 2

    second = Outbox("gtc-test", "bbbb2222", queue_max=2)
    assert second.dropped_total == 2, "the count is in the database, not in memory"


def test_a_corrupt_dropped_counter_reads_as_zero():
    set_config_value(DROPPED_KEY, "not a number")
    assert Outbox("gtc-test", "aaaa1111").dropped_total == 0


# -------------------------------------------------------------------- backoff


def test_backoff_doubles_to_a_ceiling_and_resets_on_success():
    with StubHub() as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        seen = []
        for _ in range(10):
            reporter._note_failure("down")
            seen.append(reporter._backoff)

        assert seen[:7] == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0]
        assert seen[7:] == [60.0, 60.0, 60.0], "steady at the ceiling, not growing"

        assert _drive(reporter) == [True]

    assert reporter._backoff == 0.0, "one good report clears the whole wait"
    assert reporter.consecutive_failures == 0
    assert hub.reports


def test_a_backoff_is_not_shortened_by_something_happening():
    """A hub that just refused a report will refuse the next one.

    Letting a nudge cut the wait short would turn the back-off into a tight
    loop precisely when the hub is least able to take one.
    """
    reporter = HubReporter(EventBus(), state(), cfg("http://127.0.0.1:1"))
    reporter._backoff = 0.2
    reporter._nudge.set()

    async def go():
        started = time.monotonic()
        await reporter._wait_for_turn()
        return time.monotonic() - started

    waited = asyncio.run(go())
    assert waited >= 0.18, "the nudge must not have shortened it"
    assert reporter._nudge.is_set(), "and the nudge is still pending"


# -------------------------------------------------------------- the snapshot


def test_the_snapshot_reports_slaves_masters_and_risk():
    insert_slave("mt5-icm-1", "MT5 ICM Mirror", "mt4",
                 {"mode": "mirror", "account": "53116707"}, True)
    insert_slave("ibkr-etf-1", "IBKR ETF", "ibkr",
                 {"mode": "autonomous", "balance_adjustment": -5000.0}, True)
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO trades (master_ticket, master_account, symbol, action, "
            "signal_ts, received_at, raw_json) VALUES (?,?,?,?,?,?,?)",
            (999031902, "11558127", "US2000", "OPEN", 1763064302.0, 1763064302.0, "{}"),
        )
        cur.execute(
            "INSERT INTO slave_trades (slave_id, master_account, master_ticket, "
            "slave_symbol, status) VALUES (?,?,?,?,'OPEN')",
            ("mt5-icm-1", "11558127", 999031902, "US2000.cash"),
        )
    set_config_value("kill_switch_enabled", "true")

    engine = SimpleNamespace(
        slave_id="ibkr-etf-1", paused=False, session_up=True,
        blocked_targets={"MBT_X": "multiplier disagrees with its spec"},
        preflight={"targets": 12, "resolved": 11, "at": 1763065762.0},
        watchdog=SimpleNamespace(connected=True),
        adapter=SimpleNamespace(account="DUR314653"),
    )
    with StubHub() as hub:
        reporter = HubReporter(
            EventBus(),
            state(engines=[engine],
                  receiver=SimpleNamespace(last_heartbeat={"11558127": 1763064598.0})),
            cfg(hub.url),
        )
        _drive(reporter)

    snapshot = hub.reports[0]["snapshot"]
    assert snapshot["masters"] == [{
        "account": "11558127",
        "last_signal_at": "2025-11-13T20:05:02Z",
        "last_heartbeat_at": "2025-11-13T20:09:58Z",
    }]

    by_id = {s["id"]: s for s in snapshot["slaves"]}
    mirror = by_id["mt5-icm-1"]
    assert mirror["mode"] == "mirror"
    assert mirror["running"] is False, "no engine for it in this test"
    assert mirror["session_state"] == "not_running"
    assert mirror["connected"] is None, "unknown, which is not disconnected"
    assert mirror["open_legs"] == 1
    assert mirror["account"] == "53116707"
    assert mirror["preflight"] is None

    etf = by_id["ibkr-etf-1"]
    assert etf["running"] is True
    assert etf["connected"] is True
    assert etf["session_state"] == "connected"
    assert etf["account"] == "DUR314653", "the adapter's resolved account wins"
    assert etf["blocked_targets"] == {"MBT_X": "multiplier disagrees with its spec"}
    assert etf["preflight"] == {"targets": 12, "resolved": 11,
                                "at": "2025-11-13T20:29:22Z"}

    assert snapshot["risk"] == {
        "kill_switch_enabled": True,
        "balance_adjustments": {"ibkr-etf-1": -5000.0},
    }


def test_a_slaves_last_error_comes_from_the_events_that_passed():
    """Nothing in the copier keeps one: the engines log and move on."""
    insert_slave("mt5-icm-1", "MT5 ICM Mirror", "mt4", {"mode": "mirror"}, True)
    with StubHub() as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        reporter.ingest_one({"type": "slave_open", "slave_id": "mt5-icm-1",
                             "emitted_at": 1763064304.0})
        reporter.ingest_one({"type": "slave_error", "slave_id": "mt5-icm-1",
                             "action": "OPEN", "message": "no money",
                             "emitted_at": 1763064305.0})
        _drive(reporter)

    slave = hub.reports[0]["snapshot"]["slaves"][0]
    assert slave["last_error"] == "no money"
    assert slave["last_error_at"] == "2025-11-13T20:05:05Z"


def test_an_unacked_backlog_shows_as_a_growing_depth():
    """The one symptom of a hub that accepts reports and does not ack them."""
    with StubHub(ack="none") as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        for t in range(3):
            reporter.ingest_one(signal_event(ticket=t))
            _drive(reporter)

    depths = [r["snapshot"]["queue"]["depth"] for r in hub.reports]
    assert depths == [1, 2, 3]
    assert hub.reports[0]["snapshot"]["queue"]["oldest_event_at"].endswith("Z")


# ----------------------------------------------------------- size_unit


@pytest.mark.parametrize("mode, symbol, unit", [
    ("mirror", "US2000.cash", "lots"),
    ("autonomous", "M2K", "contracts"),
    ("autonomous", "BITU", "shares"),
])
def test_size_unit_is_the_slave_brokers_own(mode, symbol, unit):
    """24 M2K contracts and 1.0 US2000 lots are the same signal.

    Nothing is converted anywhere, so the unit is what stops a tab adding
    them together.
    """
    insert_slave("s1", "S1", "ibkr", {"mode": mode}, True)
    with StubHub() as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        reporter.ingest_one(open_event(slave_id="s1", slave_symbol=symbol))
        _drive(reporter)

    assert hub.reports[0]["events"][0]["detail"]["size_unit"] == unit


# -------------------------------------------------------- off by default


def test_a_copier_with_no_key_does_nothing_at_all():
    """The most important property of the whole feature.

    No subscription, no task, no row, no request. A copier without a key
    behaves exactly as one built before the reporter existed.
    """
    events = EventBus()
    reporter = HubReporter(events, state(), HubReporterConfig(
        enabled=True, url="http://127.0.0.1:1", key=""))

    async def go():
        await reporter.start()
        await events.emit({"type": "slave_open", "slave_id": "s1"})
        await reporter.stop()

    asyncio.run(go())

    assert reporter.running is False
    assert events._subscribers == []
    assert pending_ids() == []
    assert reporter.reports_sent == 0


def test_enabled_false_is_inert_even_with_a_key():
    reporter = HubReporter(EventBus(), state(), HubReporterConfig(
        enabled=False, url="http://127.0.0.1:1", key="a-key"))
    asyncio.run(reporter.start())
    assert reporter.running is False


def test_a_malformed_config_file_disables_rather_than_raises(tmp_path):
    """A typo in a JSON file must never be why a copier does not start."""
    from app.hub_reporter.config import load

    broken = tmp_path / "hub_reporter.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load(broken).active is False

    missing = tmp_path / "absent.json"
    assert load(missing).active is False

    bad_value = tmp_path / "bad.json"
    bad_value.write_text(json.dumps({"hub_reporter": {"timeout_s": "soon"}}),
                         encoding="utf-8")
    assert load(bad_value).active is False


def test_the_key_comes_from_the_environment_before_the_file(tmp_path, monkeypatch):
    from app.hub_reporter.config import KEY_ENV, load

    key_file = tmp_path / "key.txt"
    key_file.write_text("from-the-file\n", encoding="utf-8")
    conf = tmp_path / "hub_reporter.json"
    conf.write_text(json.dumps({"hub_reporter": {
        "enabled": True, "url": "https://example.invalid",
        "key_file": str(key_file),
    }}), encoding="utf-8")

    monkeypatch.delenv(KEY_ENV, raising=False)
    assert load(conf).key == "from-the-file"

    monkeypatch.setenv(KEY_ENV, "from-the-environment")
    assert load(conf).key == "from-the-environment"


def test_the_shipped_config_is_off():
    """It stays off until the hub issues a key. Nothing here is deployed."""
    from app.hub_reporter.config import CONFIG_FILE, load

    assert CONFIG_FILE.exists(), "the block the spec defines should be present"
    block = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))["hub_reporter"]
    assert block["enabled"] is False
    assert load(CONFIG_FILE).active is False


# ---------------------------------------------------- unmapped and ignored


def test_the_per_row_reconciliation_events_are_not_reported_twice():
    """The summary carries the same rows in untracked[] and stuck[]."""
    with StubHub() as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        assert reporter.ingest_one({
            "type": "reconciliation", "slave_id": "s1",
            "action": "untracked_position", "symbol": "MGC",
            "emitted_at": 1763064304.0,
        }) is None
        reporter.ingest_one({
            "type": "reconciliation_summary", "slave_id": "s1",
            "checked": 3, "matched": 2,
            "untracked": [{"slave_symbol": "MGC", "quantity": 2.0}],
            "stuck": [{"slave_symbol": "M2K", "master_ticket": 7,
                       "slave_ticket": 392, "resolved": "marked_closed"}],
            "at": 1763064304.0, "emitted_at": 1763064304.0,
        })
        _drive(reporter)

    reported = hub.reports[0]["events"]
    assert len(reported) == 1
    assert reported[0]["type"] == "reconciliation"
    assert reported[0]["detail"]["checked"] == 3
    assert reported[0]["detail"]["matched"] == 2
    assert reported[0]["detail"]["untracked"][0]["slave_symbol"] == "MGC"
    assert reported[0]["detail"]["stuck"][0]["resolved"] == "marked_closed"
    assert reported[0]["detail"]["at"] == "2025-11-13T20:05:04Z"


def test_an_event_type_nobody_mapped_is_passed_through_not_dropped():
    """A new event added to an engine should appear unlabelled, not vanish."""
    with StubHub() as hub:
        reporter = HubReporter(EventBus(), state(), cfg(hub.url))
        reporter.ingest_one({"type": "something_new", "slave_id": "s1",
                             "master_ticket": 7, "oddity": 42,
                             "emitted_at": 1763064304.0})
        _drive(reporter)

    event = hub.reports[0]["events"][0]
    assert event["type"] == "something_new"
    assert event["master_ticket"] == 7
    assert event["detail"] == {"oddity": 42}


def test_a_hub_that_cannot_be_reached_costs_no_events():
    """Nothing listening on the port. Whatever the socket layer calls that
    -- refused on Linux, a connect timeout on Windows -- it means the same
    thing here: nothing was acked, so nothing is dropped."""
    reporter = HubReporter(
        EventBus(), state(), cfg(f"http://127.0.0.1:{closed_port()}"))
    queued = reporter.ingest_one(signal_event())
    assert _drive(reporter) == [False]
    assert [e for _, e in pending_ids()] == [queued]
    assert reporter._backoff == 1.0
    assert reporter.last_error, "and the reason is kept, not just the failure"
