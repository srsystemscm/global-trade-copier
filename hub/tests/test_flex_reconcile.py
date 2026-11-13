"""Checking the copier's record of what it did against the broker's own.

The comparison works because `IBKRAdapter.open()` records
`slave_ticket = trade.order.orderId` and Flex reports `ibOrderID`, so the
statement and the registry are already keyed the same way.

Never the live Flex service: every test here hands the pass a fake client
returning one of the trimmed real responses from `tests/test_flex.py`. A
token IS configured in `hub/.env` on the development machine, which is
exactly why a test must not be able to reach out.
"""
import asyncio
import time

import pytest

from app import flex_reconcile
from app.db import db_cursor
from app.flex import FlexError, parse_statement
from app.flex_reconcile import FlexReconciler, compare, run_once, store
from app.slaves import insert_slave
from tests.test_flex import EMPTY, REAL

def execution(trade_id, exec_id, order_id, qty, price, side="BUY",
              open_close="O", symbol="GCZ6"):
    """One EXECUTION row, in the shape the real statement uses."""
    return (
        f'<Trade accountId="DUR314653" currency="USD" assetCategory="FUT" '
        f'symbol="{symbol}" conid="462941472" underlyingSymbol="GC" '
        f'tradeID="{trade_id}" ibExecID="{exec_id}" ibOrderID="{order_id}" '
        f'multiplier="100" expiry="20261229" dateTime="20261029;023253" '
        f'tradeDate="20261029" exchange="COMEX" quantity="{qty}" '
        f'tradePrice="{price}" ibCommission="-2.51" '
        f'openCloseIndicator="{open_close}" buySell="{side}" '
        f'orderTime="20261029;023253" levelOfDetail="EXECUTION" />'
    )


def statement(*executions):
    """A statement carrying exactly these executions.

    Built rather than string-replaced into `REAL`, because what a partial
    fill actually looks like matters here: two executions on ONE order are
    the SAME side. Giving an entry and an exit one order id -- which is
    what editing REAL produced -- is not a partial fill, and the first
    version of this file quietly tested that instead.
    """
    return (
        '<FlexQueryResponse queryName="CFD-FUTS TEST" type="AF">'
        '<FlexStatements count="1">'
        '<FlexStatement accountId="DUR314653" fromDate="20261012" '
        'toDate="20261110" period="Last30CalendarDays" '
        'whenGenerated="20261111;230354"><Trades>'
        + "".join(executions) +
        "</Trades></FlexStatement></FlexStatements></FlexQueryResponse>"
    )


# One order filled in two pieces, both BUY: 1 lot at 4040.7 and 1 at
# 4037.2 is 2 lots at a 4038.95 volume-weighted average.
ONE_ORDER = statement(
    execution("330723926", "exec-a", "900001", 1, 4040.7),
    execution("330723927", "exec-b", "900001", 1, 4037.2),
)

# An entry and an exit, which is two orders.
TWO_ORDERS = statement(
    execution("330723926", "exec-a", "900001", 1, 4040.7),
    execution("330724022", "exec-c", "900002", -1, 4037.2,
              side="SELL", open_close="C"),
)

# Both sides under one order id: nothing a sane statement contains, and the
# case where summing signed quantity reads the order as a zero-size fill.
TWO_SIDED = statement(
    execution("330723926", "exec-a", "900001", 1, 4040.7),
    execution("330724022", "exec-c", "900001", -1, 4037.2,
              side="SELL", open_close="C"),
)


class FakeFlex:
    """Stands in for FlexClient. Never touches a socket."""

    def __init__(self, xml=None, error=None):
        self.xml = xml
        self.error = error
        self.fetches = 0

    async def fetch_async(self):
        self.fetches += 1
        if self.error is not None:
            raise self.error
        return parse_statement(self.xml)


def ibkr_slave(slave_id="ibkr-paper-1"):
    insert_slave(slave_id, "IBKR Paper", "ibkr", {"mode": "autonomous"}, True)
    return slave_id


def registry_row(slave_id, broker_order_id, symbol="GCZ6", price=None,
                 lots=None, master_ticket=1, slave_ticket=1):
    """A copy this hub made, keyed the way the statement keys it.

    `broker_order_id` is the join, NOT `slave_ticket`: the latter is TWS's
    client-side order number and never appears in a Flex statement. The
    first version of these tests seeded `slave_ticket` and passed, which is
    how a join that matched 0 of 109 real orders got shipped.
    """
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO slave_trades (slave_id, master_account, master_ticket, "
            "slave_symbol, slave_ticket, broker_order_id, status, slave_price, "
            "slave_lots) VALUES (?, '11558127', ?, ?, ?, ?, 'OPEN', ?, ?)",
            (slave_id, master_ticket, symbol, slave_ticket,
             str(broker_order_id), price, lots),
        )


def protective_leg(slave_id, sl_order_id=None, tp_order_id=None,
                   symbol="GCZ6", master_ticket=1):
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO engine_positions (slave_id, master_account, "
            "master_ticket, slave_symbol, slave_ticket, direction, size, "
            "sl_order_id, tp_order_id, updated_at) "
            "VALUES (?, '11558127', ?, ?, 1, 'BUY', 1.0, ?, ?, ?)",
            (slave_id, master_ticket, symbol, sl_order_id, tp_order_id, time.time()),
        )


# ------------------------------------------------------------------ storing


def test_a_statement_is_stored_once_however_often_it_arrives():
    """Flex periods overlap by design: a "last 30 days" query re-sends 29 of
    them every time it runs."""
    statement = parse_statement(ONE_ORDER)

    assert store(statement) == (2, 0)
    assert store(statement) == (0, 2), "the same executions, already held"

    with db_cursor() as cur:
        rows = cur.execute(
            "SELECT exec_key, ib_order_id, quantity, price, commission "
            "FROM flex_trades ORDER BY exec_key"
        ).fetchall()
    assert len(rows) == 2
    assert {r["ib_order_id"] for r in rows} == {"900001"}
    assert [r["quantity"] for r in rows] == [1.0, 1.0], "one side, two pieces"
    assert sorted(r["price"] for r in rows) == [4037.2, 4040.7]


def test_the_broker_times_are_stored_as_written_not_converted():
    """IBKR reports them in the account's configured report timezone, which
    the file does not state. Converting them would be inventing an offset."""
    store(parse_statement(ONE_ORDER))
    with db_cursor() as cur:
        row = cur.execute(
            "SELECT trade_time FROM flex_trades ORDER BY exec_key"
        ).fetchone()
    assert row["trade_time"].startswith("2026-10-29T02:")
    assert "+" not in row["trade_time"] and "Z" not in row["trade_time"]


def test_an_execution_with_no_id_at_all_is_not_stored():
    """There would be no way to avoid storing it again tomorrow."""
    xml = ONE_ORDER.replace(' tradeID="330723926"', "").replace(
        ' ibExecID="exec-a"', "")
    stored, seen = store(parse_statement(xml))
    assert (stored, seen) == (1, 0), "the other execution still stored"


def test_an_empty_statement_stores_nothing_and_is_clean():
    statement = parse_statement(EMPTY)
    assert store(statement) == (0, 0)
    summary = compare(statement)
    assert summary.checked == 0
    assert summary.clean is True


# ---------------------------------------------------------------- comparing


def test_an_order_the_hub_recorded_is_matched_and_its_price_checked():
    """The point of the whole exercise: a fill price is reported once over
    the API and is simply wrong if it was mis-captured. Nothing other than
    the broker's own statement can say so.
    """
    slave = ibkr_slave()
    # 1 @ 4040.7 and 1 @ 4037.2 is 2 lots at 4038.95.
    registry_row(slave, 900001, price=4038.95, lots=2.0)

    summary = compare(parse_statement(ONE_ORDER))
    assert summary.checked == 2, "two executions"
    assert summary.orders == 1, "of one order"
    assert summary.matched == 1
    assert summary.mismatched == []
    assert summary.untracked == []
    assert summary.clean is True


def test_a_price_the_hub_captured_wrongly_is_reported():
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4040.70, lots=2.0)

    summary = compare(parse_statement(ONE_ORDER))
    assert summary.matched == 1
    assert len(summary.mismatched) == 1
    difference = summary.mismatched[0]
    assert difference["ib_order_id"] == "900001"
    assert difference["slave_id"] == slave
    assert difference["executions"] == 2
    assert difference["price"]["hub"] == 4040.70
    assert round(difference["price"]["broker"], 2) == 4038.95
    assert "size" not in difference, "the size agreed"
    assert summary.clean is False


def test_a_size_the_hub_recorded_wrongly_is_reported():
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4038.95, lots=5.0)

    difference = compare(parse_statement(ONE_ORDER)).mismatched[0]
    assert difference["size"] == {"hub": 5.0, "broker": 2.0}
    assert "price" not in difference


def test_rounding_is_not_a_mismatch():
    """A disagreement of 0.01 is nothing on gold at 4,040, so the tolerance
    is relative rather than absolute."""
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4038.9501, lots=2.0)
    assert compare(parse_statement(ONE_ORDER)).mismatched == []


def test_a_price_the_hub_never_captured_is_not_a_mismatch():
    """NULL is "not captured", which is not a disagreement about a value."""
    slave = ibkr_slave()
    registry_row(slave, 900001, price=None, lots=None)
    summary = compare(parse_statement(ONE_ORDER))
    assert summary.matched == 1
    assert summary.mismatched == []


def test_an_order_the_broker_filled_that_the_hub_has_no_record_of():
    """Reported, but not alarmed on -- yet.

    One of the two orders is in the registry, so the join is available and
    the other can be named. It is still not raised, because protective
    stop and target orders are recorded by their client-side ids and so
    cannot be recognised either: an alerting untracked list would include
    every protective fill. See Summary.untracked_reliable.
    """
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4040.7, lots=1.0)

    summary = compare(parse_statement(TWO_ORDERS))
    assert summary.orders == 2
    assert summary.join_available is True
    assert summary.matched == 1
    assert [u["ib_order_id"] for u in summary.untracked] == ["900002"]
    assert summary.untracked[0]["slave_symbol"] == "GCZ6"
    assert summary.untracked[0]["size"] == 1.0
    assert summary.untracked_reliable is False
    assert summary.clean is True, "reported, not raised"


def test_a_pass_with_nothing_to_join_on_claims_nothing():
    """The failure this guard exists for.

    The join was first written against `slave_ticket`, which is TWS's
    client-side order number and never appears in a statement. Run against
    a real 30-day statement it matched 0 of 109 orders and called every one
    untracked -- an alarm that would have fired daily and been wrong daily.

    With no row carrying a broker order id there is nothing to compare, so
    the pass says so and asserts nothing about the statement.
    """
    ibkr_slave()                      # a slave, but no copies recorded
    summary = compare(parse_statement(TWO_ORDERS))

    assert summary.join_available is False
    assert summary.matched == 0
    assert summary.untracked == [], "no key, no claim"
    assert summary.mismatched == []
    assert summary.clean is True, "and nothing to alert about"
    assert summary.checked == 2, "the executions were still counted"


def test_rows_predating_the_key_are_counted_not_guessed_at():
    """They are invisible to the join and nothing can recover their id."""
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4040.7, lots=1.0)
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO slave_trades (slave_id, master_account, master_ticket, "
            "slave_symbol, slave_ticket, status) "
            "VALUES (?, '11558127', 77, 'MGC', 404, 'OPEN')", (slave,))

    summary = compare(parse_statement(TWO_ORDERS))
    assert summary.join_available is True
    assert summary.unjoinable_rows == 1


def test_both_sides_under_one_order_id_is_reported_as_itself():
    """Summing signed quantity reads it as a zero-size fill, which then
    looks like the hub recorded the size wrongly -- a true statement about
    nothing, since the hub's figure was right and the pairing was the odd
    thing."""
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4038.95, lots=2.0)

    difference = compare(parse_statement(TWO_SIDED)).mismatched[0]
    assert difference["two_sided"] == {"filled": 2.0, "net": 0.0}
    assert "size" not in difference, "the hub's 2 lots WERE filled"


def test_a_stop_or_target_fill_is_not_untracked():
    """They execute at the broker without this hub placing a closing order,
    so they appear under order ids that are nowhere in slave_trades. Counted
    as untracked, every protected position would look like drift.
    """
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4040.7, lots=1.0)
    protective_leg(slave, sl_order_id=900002)

    summary = compare(parse_statement(TWO_ORDERS))
    assert summary.matched == 1
    assert summary.protective == 1
    assert summary.untracked == [], "the stop fill is accounted for"
    assert summary.clean is True


def test_a_target_order_id_counts_too():
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4040.7, lots=1.0)
    protective_leg(slave, tp_order_id=900002)
    assert compare(parse_statement(TWO_ORDERS)).protective == 1


def test_executions_with_no_order_id_are_counted_not_hidden():
    """So an empty reconciliation shows its cause rather than looking like
    "nothing to check". The field has to be ticked in the query."""
    ibkr_slave()
    summary = compare(parse_statement(REAL))
    assert summary.unjoinable == 2
    assert summary.orders == 0
    assert summary.matched == 0
    assert summary.untracked == [], "unjoinable is not untracked"


def test_only_ibkr_slaves_are_joined():
    """An MT4 slave's ids live in a different numbering space entirely, so
    joining across both would match by coincidence of value.
    """
    insert_slave("mt5-icm-1", "MT5 ICM", "mt4", {"mode": "mirror"}, True)
    registry_row("mt5-icm-1", 900001, price=4038.95, lots=2.0)

    # An IBKR row too, on an id the statement does NOT carry, so the join
    # is available and the question is only whether the MT4 row is used.
    slave = ibkr_slave()
    registry_row(slave, 555555, symbol="MGC", price=1.0, lots=1.0,
                 master_ticket=99)

    summary = compare(parse_statement(ONE_ORDER))
    assert summary.join_available is True
    assert summary.matched == 0, "the MT4 row was not joined against"
    assert [u["ib_order_id"] for u in summary.untracked] == ["900001"]


def test_absence_is_never_claimed_to_have_been_checked():
    """A statement covers completed days only, so a registry row with no
    execution in it is usually legitimate rather than drift. The payload
    says so, rather than letting a clean pass be read as "the broker
    executed everything the registry expected".
    """
    slave = ibkr_slave()
    registry_row(slave, 777777, price=1.0, lots=1.0, master_ticket=9)

    summary = compare(parse_statement(EMPTY))
    assert summary.clean is True, "a row absent from the statement is not drift"
    assert summary.as_detail()["absence_checked"] is False


# -------------------------------------------------------------- the pass


def _collect():
    seen = []

    class Collecting:
        async def emit(self, event):
            seen.append(event)

    return seen, Collecting()


def test_a_pass_stores_compares_and_reports():
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4038.95, lots=2.0)
    seen, events = _collect()
    client = FakeFlex(ONE_ORDER)

    summary = asyncio.run(run_once(events, client))

    assert client.fetches == 1
    assert summary.stored == 2
    assert [e["type"] for e in seen] == ["flex_reconciliation"]
    event = seen[0]
    assert event["slave_id"] is None, "a statement covers the whole account"
    assert event["matched"] == 1
    assert event["account"] == "DUR314653"
    assert event["period"] == "20261012 -> 20261110"
    assert event["absence_checked"] is False


def test_a_second_pass_on_the_same_statement_reports_no_new_executions():
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4038.95, lots=2.0)
    client = FakeFlex(ONE_ORDER)

    first = asyncio.run(run_once(None, client))
    second = asyncio.run(run_once(None, client))

    assert (first.stored, first.already_had) == (2, 0)
    assert (second.stored, second.already_had) == (0, 2)
    assert second.matched == 1, "and it still reconciles"


def test_a_disagreement_notifies_and_a_clean_pass_does_not():
    slave = ibkr_slave()
    registry_row(slave, 900001, price=9999.0, lots=2.0)
    sent = []

    class Recording:
        async def notify(self, event_type, message, **kw):
            sent.append((event_type, message))

    asyncio.run(run_once(None, FakeFlex(ONE_ORDER), Recording()))
    assert [t for t, _ in sent] == ["flex_reconciliation"]
    assert "disagrees" in sent[0][1]

    sent.clear()
    with db_cursor() as cur:
        cur.execute("UPDATE slave_trades SET slave_price = 4038.95")
    asyncio.run(run_once(None, FakeFlex(ONE_ORDER), Recording()))
    assert sent == [], "a clean pass is not an alert"


def test_a_fetch_failure_is_an_outage_not_a_clean_pass():
    """Reporting it as "nothing disagrees" would be the worst of both."""
    with pytest.raises(FlexError):
        asyncio.run(run_once(None, FakeFlex(error=FlexError("1018", "rate limited"))))

    with db_cursor() as cur:
        assert cur.execute("SELECT COUNT(*) AS n FROM flex_trades").fetchone()["n"] == 0


def test_a_pass_without_flex_configured_does_nothing(monkeypatch):
    monkeypatch.setattr(flex_reconcile, "client_from_settings", lambda: None)
    assert asyncio.run(run_once(None)) is None


# ------------------------------------------------------------ the schedule


def test_the_reconciler_is_inert_without_a_token(monkeypatch):
    """Which is how the hub has always run."""
    monkeypatch.setattr(flex_reconcile, "client_from_settings", lambda: None)
    reconciler = FlexReconciler()
    assert reconciler.configured is False
    reconciler.start()
    assert reconciler._task is None
    asyncio.run(reconciler.stop())


def test_the_loop_runs_a_pass_and_can_be_stopped():
    slave = ibkr_slave()
    registry_row(slave, 900001, price=4038.95, lots=2.0)
    seen, events = _collect()
    client = FakeFlex(ONE_ORDER)

    async def go():
        reconciler = FlexReconciler(
            events=events, client=client,
            interval=3600.0, initial_delay=0.0, retry_base=0.01)
        reconciler.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and reconciler.passes < 1:
            await asyncio.sleep(0.005)
        await reconciler.stop()
        return reconciler

    reconciler = asyncio.run(go())
    assert reconciler.passes == 1
    assert reconciler.failures == 0
    assert [e["type"] for e in seen] == ["flex_reconciliation"]


def test_a_failing_fetch_backs_off_and_never_ends_the_task():
    """Tomorrow's statement is a fresh attempt, so there is nothing worth
    ending the task for."""
    client = FakeFlex(error=FlexError("1018", "too many requests"))

    async def go():
        reconciler = FlexReconciler(
            client=client, interval=3600.0, initial_delay=0.0, retry_base=0.01)
        reconciler.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and client.fetches < 3:
            await asyncio.sleep(0.005)
        alive = not reconciler._task.done()
        await reconciler.stop()
        return reconciler, alive

    reconciler, alive = asyncio.run(go())
    assert client.fetches >= 3, "it kept trying"
    assert alive, "the task survived"
    assert reconciler.failures >= 3
    assert "1018" in reconciler.last_error


def test_an_expired_token_waits_for_a_human_instead_of_retrying():
    """No schedule fixes an expired token, and a daily alert about one
    becomes noise within a week -- so it is said once per cause."""
    sent = []

    class Recording:
        async def notify(self, event_type, message, **kw):
            sent.append(event_type)

    reconciler = FlexReconciler(
        notifier=Recording(), client=FakeFlex(ONE_ORDER),
        interval=3600.0, initial_delay=0.0, retry_base=0.01)

    async def go():
        expired = FlexError("1012", "Token has expired.")
        first = reconciler._note_failure(expired, "1012", 0.0)
        second = reconciler._note_failure(expired, "1012", first)
        await asyncio.sleep(0)   # let the queued notification run
        return first, second

    first, second = asyncio.run(go())
    assert first == second == 3600.0, "waits the full interval, does not hammer"
    assert sent == ["flex_unavailable"], "told once, not twice"


def test_backoff_doubles_from_the_base_and_is_capped_at_the_interval():
    reconciler = FlexReconciler(
        client=FakeFlex(ONE_ORDER), interval=100.0, initial_delay=0.0,
        retry_base=10.0)
    waits = []
    backoff = 0.0
    for _ in range(6):
        backoff = reconciler._note_failure(FlexError("1021", "failed"), "1021", backoff)
        waits.append(backoff)
    assert waits == [10.0, 20.0, 40.0, 80.0, 100.0, 100.0]


def test_the_tests_never_reach_the_live_flex_service():
    """A token IS configured in hub/.env on the development machine, which
    is exactly why this has to be impossible by construction rather than by
    care: every pass here is handed a fake client."""
    from pathlib import Path

    # Both needles assembled, so this test does not fail on its own source.
    live_host = "interactivebrokers" + ".com"
    real_client = "FlexClient" + "("
    text = Path(__file__).read_text(encoding="utf-8")
    assert live_host not in text
    assert real_client not in text, "every pass here is handed a fake"


def test_the_reconciler_is_wired_into_the_apps_lifespan():
    import inspect

    from app import main

    source = inspect.getsource(main.lifespan)
    assert "FlexReconciler(events=events, notifier=notifier)" in source
    assert "flex.start()" in source
    assert "await flex.stop()" in source
