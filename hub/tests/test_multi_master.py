"""Several MT4 masters feeding one hub.

Everything used to key on master_ticket alone, which is only safe with one
master: MT4 ticket numbers are assigned per account, so two masters can
legitimately issue the same ticket number for different trades. Of the three
resulting collisions, none announced itself -- a dropped signal looks like a
quiet market, and a cross-master close looks like the copier working.

The worst is test_one_masters_close_cannot_close_anothers_leg below.
"""
import asyncio
import json

import pytest

from app import position_store, trade_registry
from app.copy_engine import CopyEngine
from app.db import db_cursor, init_db
from app.events import EventBus
from app.models import Signal
from app.routing import describe_refusal, resolve_routes
from app.signal_bus import SignalBus
from tests.conftest_fakes import FakeBroker

A = "MASTER-A"
B = "MASTER-B"
SLAVE = "slave-1"


def _raw(account, ticket=5001, action="OPEN", ts=1700000000.0, **kw):
    base = dict(ticket=ticket, symbol="XAUUSD", action=action, direction="BUY",
                lots=0.1, price=4029.32, sl=3937.0, atr=18.0, account=account, ts=ts)
    base.update(kw)
    return json.dumps(base).encode("utf-8")


def _trade_rows():
    with db_cursor() as cur:
        return [dict(r) for r in cur.execute(
            "SELECT master_account, master_ticket, action FROM trades ORDER BY id")]


# --- dedup ----------------------------------------------------------------

def test_two_masters_sending_the_same_ticket_are_both_kept():
    """The silent one. Keyed ticket:action:ts, master B's signal was dropped
    as a duplicate of master A's -- same ticket number, same action, and the
    EA's TimeGMT() only has whole-second resolution so the timestamps match
    routinely."""
    bus = SignalBus()
    asyncio.run(bus.publish_raw(_raw(A)))
    asyncio.run(bus.publish_raw(_raw(B)))

    rows = _trade_rows()
    assert len(rows) == 2, "one master's signal was swallowed as a duplicate"
    assert {r["master_account"] for r in rows} == {A, B}


def test_a_genuine_replay_from_one_master_is_still_dropped():
    """The account must not weaken what dedup was there for."""
    bus = SignalBus()
    asyncio.run(bus.publish_raw(_raw(A)))
    asyncio.run(bus.publish_raw(_raw(A)))
    assert len(_trade_rows()) == 1


def test_the_same_ticket_from_one_master_with_a_new_action_is_kept():
    bus = SignalBus()
    asyncio.run(bus.publish_raw(_raw(A, action="OPEN")))
    asyncio.run(bus.publish_raw(_raw(A, action="MODIFY")))
    assert [r["action"] for r in _trade_rows()] == ["OPEN", "MODIFY"]


# --- the registry ---------------------------------------------------------

def test_two_masters_same_ticket_are_separate_rows_on_one_slave():
    trade_registry.record_open(SLAVE, A, 5001, "XAUUSD", 111)
    trade_registry.record_open(SLAVE, B, 5001, "XAUUSD", 222)

    assert [m.slave_ticket for m in trade_registry.get_open_mappings(SLAVE, A, 5001)] == [111]
    assert [m.slave_ticket for m in trade_registry.get_open_mappings(SLAVE, B, 5001)] == [222]


def test_closing_one_master_leaves_the_other_open():
    trade_registry.record_open(SLAVE, A, 5001, "XAUUSD", 111)
    trade_registry.record_open(SLAVE, B, 5001, "XAUUSD", 222)

    trade_registry.record_close(SLAVE, B, 5001, "XAUUSD")

    assert trade_registry.get_open_mappings(SLAVE, A, 5001), "master A's leg was closed too"
    assert not trade_registry.get_open_mappings(SLAVE, B, 5001)


def test_startup_reconciliation_knows_which_master_each_row_belongs_to():
    """get_all_open_for_slave() feeds record_close() and position_store
    .delete() during reconciliation, so the row has to carry its account or
    reconciliation would close the wrong master's registry row."""
    trade_registry.record_open(SLAVE, A, 5001, "XAUUSD", 111)
    trade_registry.record_open(SLAVE, B, 5001, "XAUUSD", 222)

    rows = {r.master_account: r for r in trade_registry.get_all_open_for_slave(SLAVE)}
    assert set(rows) == {A, B}
    assert rows[A].slave_ticket == 111
    assert rows[B].slave_ticket == 222


# --- the one that could lose money ---------------------------------------

def test_one_masters_close_cannot_close_anothers_leg():
    """Both masters long XAUUSD, both happening to use ticket 5001. Master B
    closes. Before this, get_open_mappings(slave, 5001) returned whichever
    row the database reached first, so B's CLOSE could close A's position and
    leave B's open -- and both the registry and the activity feed would show
    it as a clean copy."""
    broker = FakeBroker()
    queue: asyncio.Queue = asyncio.Queue()
    engine = CopyEngine(SLAVE, broker, queue)

    async def go():
        engine.start()
        await queue.put(Signal(ticket=5001, symbol="XAUUSD", action="OPEN",
                               direction="BUY", lots=0.1, price=4029.32, account=A))
        await queue.put(Signal(ticket=5001, symbol="XAUUSD", action="OPEN",
                               direction="BUY", lots=0.2, price=4029.50, account=B))
        await asyncio.sleep(0.08)
        await queue.put(Signal(ticket=5001, symbol="XAUUSD", action="CLOSE", account=B))
        await asyncio.sleep(0.08)
        await engine.stop()

    asyncio.run(go())

    assert len(broker.opened) == 2, "the second master's OPEN never reached the broker"
    a_ticket, b_ticket = (o["ticket"] for o in broker.opened)
    assert broker.closed == [b_ticket], "closed the wrong master's leg"
    assert trade_registry.get_open_mappings(SLAVE, A, 5001), "master A's leg was orphaned"


# --- durable engine state -------------------------------------------------

def _pos(**kw):
    base = dict(slave_ticket=99, direction="BUY", size=1.0, entry_price=4050.0,
                atr=20.0, breakeven_done=False, month_slot=0)
    base.update(kw)
    return base


def test_stored_positions_are_separated_by_master():
    position_store.save(SLAVE, A, 5001, "GC", _pos(slave_ticket=111))
    position_store.save(SLAVE, B, 5001, "GC", _pos(slave_ticket=222))

    rows = dict(position_store.load_all(SLAVE))
    assert set(rows) == {(A, 5001, "GC"), (B, 5001, "GC")}
    assert rows[(A, 5001, "GC")]["slave_ticket"] == 111


def test_deleting_one_masters_position_leaves_the_other():
    position_store.save(SLAVE, A, 5001, "GC", _pos(slave_ticket=111))
    position_store.save(SLAVE, B, 5001, "GC", _pos(slave_ticket=222))

    position_store.delete(SLAVE, B, 5001, "GC")

    assert [k for k, _ in position_store.load_all(SLAVE)] == [(A, 5001, "GC")]


# --- routing --------------------------------------------------------------

def _sig(account, magic=2389):
    return Signal(ticket=1, symbol="XAUUSD", action="OPEN", direction="BUY",
                  lots=1.0, price=4029.32, atr=18.0, magic=magic, account=account)


def test_a_route_can_be_pinned_to_one_master():
    """Two masters running the same EA have the same magic, so magic alone
    cannot tell them apart."""
    config = {"routes": [
        {"symbol": "XAUUSD", "magic": 2389, "account": A, "target": "GC", "month_slot": 0},
        {"symbol": "XAUUSD", "magic": 2389, "account": B, "target": "GC", "month_slot": 1},
    ]}
    assert [r.month_slot for r in resolve_routes(config, _sig(A))] == [0]
    assert [r.month_slot for r in resolve_routes(config, _sig(B))] == [1]


def test_a_route_without_an_account_still_matches_any_master():
    """Backward compatibility: every existing route config omits it."""
    config = {"routes": [{"symbol": "XAUUSD", "magic": 2389, "target": "GC"}]}
    assert len(resolve_routes(config, _sig(A))) == 1
    assert len(resolve_routes(config, _sig(B))) == 1


def test_an_unrouted_master_is_refused_and_told_why():
    config = {"routes": [
        {"symbol": "XAUUSD", "magic": 2389, "account": A, "target": "GC"},
    ]}
    assert resolve_routes(config, _sig(B)) == []
    why = describe_refusal(config, _sig(B))
    assert B in why, "the refusal should name the master that was refused"
    assert "account" in why


# --- migrating an existing database --------------------------------------

_OLD_SCHEMA = """
DROP TABLE IF EXISTS trades;
DROP TABLE IF EXISTS slave_trades;
DROP TABLE IF EXISTS engine_positions;

CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    master_ticket INTEGER NOT NULL, master_account TEXT, symbol TEXT NOT NULL,
    action TEXT NOT NULL, direction TEXT, lots REAL, price REAL, sl REAL, tp REAL,
    signal_ts REAL NOT NULL, received_at REAL NOT NULL, raw_json TEXT NOT NULL,
    UNIQUE (master_ticket, signal_ts)
);
CREATE TABLE slave_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slave_id TEXT NOT NULL, master_ticket INTEGER NOT NULL,
    slave_symbol TEXT NOT NULL, slave_ticket INTEGER,
    status TEXT NOT NULL DEFAULT 'OPEN', opened_at REAL, closed_at REAL,
    UNIQUE (slave_id, master_ticket, slave_symbol)
);
CREATE TABLE engine_positions (
    slave_id TEXT NOT NULL, master_ticket INTEGER NOT NULL,
    slave_symbol TEXT NOT NULL, slave_ticket INTEGER NOT NULL,
    direction TEXT NOT NULL, size REAL NOT NULL, entry_price REAL, atr REAL,
    sl REAL, tp REAL, sl_mult REAL, tp_mult REAL,
    breakeven_done INTEGER NOT NULL DEFAULT 0, month_slot INTEGER NOT NULL DEFAULT 0,
    contract_id INTEGER, contract_local TEXT, contract_expiry TEXT,
    sl_order_id INTEGER, tp_order_id INTEGER, updated_at REAL NOT NULL,
    PRIMARY KEY (slave_id, master_ticket, slave_symbol)
);
"""


@pytest.fixture
def legacy_db():
    """A database as it stood before master_account joined the keys."""
    with db_cursor() as cur:
        cur.executescript(_OLD_SCHEMA)
        cur.execute(
            "INSERT INTO trades (master_ticket, master_account, symbol, action, "
            "signal_ts, received_at, raw_json) VALUES (379792233, NULL, 'XAUUSD', 'OPEN', 1.0, 2.0, '{}')")
        cur.execute(
            "INSERT INTO slave_trades (slave_id, master_ticket, slave_symbol, slave_ticket, status) "
            "VALUES ('ibkr-paper-1', 379792233, 'GC', 101, 'OPEN')")
        cur.execute(
            "INSERT INTO engine_positions (slave_id, master_ticket, slave_symbol, slave_ticket, "
            "direction, size, updated_at) VALUES ('ibkr-paper-1', 379792233, 'GC', 101, 'BUY', 1.0, 3.0)")
    yield


def test_migration_carries_existing_rows_over(legacy_db):
    """A live hub has open positions in these tables. Losing them would
    orphan real broker positions, so the migration has to move rows rather
    than recreate the tables empty."""
    init_db()

    assert [m.slave_ticket for m in trade_registry.get_open_mappings("ibkr-paper-1", "", 379792233)] == [101]
    assert [k for k, _ in position_store.load_all("ibkr-paper-1")] == [("", 379792233, "GC")]
    assert len(_trade_rows()) == 1


def test_migration_turns_a_null_account_into_the_empty_string(legacy_db):
    """NULL cannot stay: in SQLite two NULLs never compare equal, so a NULL
    account would silently disable the uniqueness it is part of."""
    init_db()
    assert _trade_rows()[0]["master_account"] == ""


def test_migration_installs_the_account_aware_keys(legacy_db):
    """After migrating, the collision the old keys allowed must be gone."""
    init_db()

    trade_registry.record_open("s", A, 5001, "XAUUSD", 1)
    trade_registry.record_open("s", B, 5001, "XAUUSD", 2)
    assert len(trade_registry.get_all_open_for_slave("s")) == 2


def test_migration_is_idempotent(legacy_db):
    """init_db() runs on every hub start."""
    init_db()
    init_db()
    assert [m.slave_ticket for m in trade_registry.get_open_mappings("ibkr-paper-1", "", 379792233)] == [101]


def test_a_fresh_database_is_not_migrated(caplog):
    """The autouse fixture already built this one from the current SCHEMA."""
    with caplog.at_level("WARNING"):
        init_db()
    assert "migrating" not in caplog.text.lower()


# --- a repeated signal must not open twice --------------------------------

def test_a_repeat_open_does_not_put_on_a_second_position():
    """SignalBus dedup keys on the EA's timestamp, which has whole-second
    resolution -- so two copies of one signal published milliseconds apart
    can land either side of a second boundary and both get through. Seen
    live: a second TradeCopierBridge instance on another chart did exactly
    that, 76ms apart with timestamps one second different.

    A duplicate CLOSE is harmless, the mapping is already gone. A duplicate
    OPEN would put on a second position, and record_open() upserts, so the
    first would be left live with nothing tracking it."""
    broker = FakeBroker()
    queue: asyncio.Queue = asyncio.Queue()
    engine = CopyEngine(SLAVE, broker, queue)

    def _open(ts):
        return Signal(ticket=5001, symbol="XAUUSD", action="OPEN", direction="BUY",
                      lots=0.1, price=4029.32, account=A, ts=ts)

    async def go():
        engine.start()
        await queue.put(_open(1700000000.0))
        await asyncio.sleep(0.06)
        await queue.put(_open(1700000001.0))   # same trade, a second later
        await asyncio.sleep(0.08)
        await engine.stop()

    asyncio.run(go())

    assert len(broker.opened) == 1, "a repeated signal opened a second position"
    assert len(trade_registry.get_open_mappings(SLAVE, A, 5001)) == 1


def test_a_genuinely_new_ticket_still_opens():
    """The guard keys on the master ticket, so it must not block the next
    trade just because the previous one is still open."""
    broker = FakeBroker()
    queue: asyncio.Queue = asyncio.Queue()
    engine = CopyEngine(SLAVE, broker, queue)

    async def go():
        engine.start()
        for ticket in (5001, 5002):
            await queue.put(Signal(ticket=ticket, symbol="XAUUSD", action="OPEN",
                                   direction="BUY", lots=0.1, price=4029.32, account=A))
            await asyncio.sleep(0.05)
        await engine.stop()

    asyncio.run(go())
    assert len(broker.opened) == 2
