import logging
import sqlite3
from contextlib import contextmanager
from typing import Iterator, List, Optional

from app.config import settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    master_ticket   INTEGER NOT NULL,
    master_account  TEXT NOT NULL DEFAULT '',
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,      -- OPEN | MODIFY | CLOSE
    direction       TEXT,               -- BUY | SELL
    lots            REAL,
    price           REAL,
    sl              REAL,
    tp              REAL,
    signal_ts       REAL NOT NULL,      -- timestamp assigned by the master EA
    received_at     REAL NOT NULL,      -- timestamp assigned by the hub
    raw_json        TEXT NOT NULL,
    -- Matches SignalBus._dedup_key() exactly, and has to.
    -- master_account, because MT4 ticket numbers are per-account: two masters
    -- can send the same ticket number in the same second for different
    -- trades, and without it the second is silently discarded by the
    -- INSERT OR IGNORE in _persist().
    -- action, because the EA's TimeGMT() has whole-second resolution while it
    -- polls every 500ms, so an OPEN and the MODIFY that follows it share a
    -- signal_ts routinely -- and that MODIFY was being dropped from the
    -- signal history even though it was still fanned out to the slaves.
    UNIQUE (master_account, master_ticket, action, signal_ts)
);

CREATE TABLE IF NOT EXISTS slaves (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    broker_type     TEXT NOT NULL,      -- mt4 | schwab | ibkr
    enabled         INTEGER NOT NULL DEFAULT 1,
    paused          INTEGER NOT NULL DEFAULT 0,
    config_json     TEXT NOT NULL DEFAULT '{}',
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

CREATE TABLE IF NOT EXISTS slave_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    slave_id        TEXT NOT NULL,
    master_account  TEXT NOT NULL DEFAULT '',   -- which master; '' = unknown/legacy
    master_ticket   INTEGER NOT NULL,
    slave_symbol    TEXT NOT NULL,      -- the mapped slave-side symbol (e.g. MGC, GLD)
    slave_ticket    INTEGER,
    status          TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED | CANCELLED
    opened_at       REAL,
    closed_at       REAL,
    -- What this copy actually executed at. Captured because none of it can
    -- be reconstructed afterwards: a fill price is reported once, in the
    -- reply to the order, and is gone if it isn't written down here.
    -- The point isn't any single row -- it's the distribution across many,
    -- which is what says whether the wobble is spread, latency or sizing.
    master_price        REAL,   -- master's own entry, denormalised so the
                                -- delta survives independent of `trades`
    slave_price         REAL,   -- what the slave actually filled at
    slave_lots          REAL,   -- what went on after any rounding
    requested_at        REAL,   -- engine handed the order to the adapter
    filled_at           REAL,   -- adapter came back with a ticket
    master_close_price  REAL,
    slave_close_price   REAL,
    -- The BROKER's own id for the order, where it has one distinct from
    -- the handle this hub trades by.
    --
    -- `slave_ticket` for an IBKR slave is `trade.order.orderId`, which is
    -- TWS's client-side sequence number: small, and restarting with the
    -- session. An IBKR Activity Flex statement reports `ibOrderID`, which
    -- is the broker's own nine-digit id. They are different numbering
    -- spaces and never match -- measured, after reconciliation was built
    -- on the assumption that they did and matched 0 of 109 orders against
    -- a real statement.
    --
    -- So the broker's id is recorded separately, and that is what
    -- reconciliation against the statement joins on.
    broker_order_id     TEXT,
    UNIQUE (slave_id, master_account, master_ticket, slave_symbol)
);

CREATE TABLE IF NOT EXISTS engine_positions (
    slave_id        TEXT    NOT NULL,
    master_account  TEXT    NOT NULL DEFAULT '',
    master_ticket   INTEGER NOT NULL,
    slave_symbol    TEXT    NOT NULL,
    slave_ticket    INTEGER NOT NULL,
    direction       TEXT    NOT NULL,
    size            REAL    NOT NULL,
    entry_price     REAL,
    atr             REAL,
    sl              REAL,
    tp              REAL,
    sl_mult         REAL,                -- ATR multiples the position was opened
    tp_mult         REAL,                -- with; a roll re-derives stops from these
    atr_period      INTEGER,             -- and the period that measured them
    bar_size        TEXT,                -- on these bars
    breakeven_done  INTEGER NOT NULL DEFAULT 0,
    month_slot      INTEGER NOT NULL DEFAULT 0,
    contract_id     INTEGER,             -- concrete futures contract, not a slot:
    contract_local  TEXT,                -- slot numbering shifts when the front
    contract_expiry TEXT,                -- month rolls, an expiry does not
    sl_order_id     INTEGER,             -- broker-side bracket legs, so they can
    tp_order_id     INTEGER,             -- be cancelled/replaced after a restart
    updated_at      REAL    NOT NULL,
    PRIMARY KEY (slave_id, master_account, master_ticket, slave_symbol)
);

CREATE TABLE IF NOT EXISTS logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    level           TEXT NOT NULL,
    logger          TEXT NOT NULL,
    message         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs (ts);

-- The Operations hub reporter's outbox. A table rather than a file because
-- an append interrupted mid-write must not corrupt the backlog: these rows
-- are the only record that an event happened at all once the live feed has
-- moved on, and they have to survive a restart intact.
--
-- `event_id` is assigned when the event is queued and stored WITH it, so
-- the id survives a restart along with the event it belongs to and the same
-- occurrence is never given two ids. UNIQUE because the hub deduplicates on
-- it, and an id issued twice here would defeat that.
--
-- `payload` is the whole event as the hub will receive it, built once at
-- queue time. Rebuilding it at send time would read engine state that has
-- since moved on, which is how a resend after an outage comes to disagree
-- with what actually happened.
-- The broker's own record of what executed, from an IBKR Activity Flex
-- statement. Two systems that agree are worth something only if they were
-- written down separately, and this is the separate one: everything else in
-- this database came through the Gateway API at the moment it happened.
--
-- Keyed on the execution, not the order: an order can fill in several
-- executions and each is its own row here. `exec_key` is IBKR's ibExecID,
-- or its tradeID where the query does not report one -- both identify an
-- execution uniquely, and INSERT OR IGNORE on it is what makes re-importing
-- an overlapping statement period harmless. Flex periods DO overlap
-- routinely: a query configured for "last 30 days" re-sends 29 of them
-- every time it runs.
--
-- `trade_time` and `order_time` are stored exactly as IBKR wrote them,
-- which is NAIVE and in the account's configured report timezone -- stated
-- nowhere in the file and not necessarily UTC. They are kept for a human to
-- read and must not be compared against this database's epoch timestamps
-- without establishing that offset first.
CREATE TABLE IF NOT EXISTS flex_trades (
    exec_key        TEXT PRIMARY KEY,
    account         TEXT NOT NULL,
    symbol          TEXT NOT NULL,      -- the contract, e.g. MGCZ6
    underlying      TEXT,               -- e.g. MGC
    conid           TEXT,
    -- The join back to this hub's registry: for an IBKR slave,
    -- slave_trades.slave_ticket IS the IB order id (see IBKRAdapter.open).
    -- NULL when the Flex query does not report ibOrderID, in which case
    -- nothing can be joined and reconciliation says so rather than
    -- guessing from tradeID, which identifies an execution and not an order.
    ib_order_id     TEXT,
    direction       TEXT NOT NULL,      -- BUY | SELL
    quantity        REAL NOT NULL,      -- signed: negative is a sale
    price           REAL NOT NULL,
    proceeds        REAL,
    commission      REAL,               -- negative, as IBKR reports it
    net_cash        REAL,
    open_close      TEXT,               -- O | C | NULL
    exchange        TEXT,
    trade_time      TEXT,               -- see the note above
    order_time      TEXT,
    statement_to    TEXT,               -- the statement period it arrived in
    imported_at     REAL NOT NULL       -- hub clock
);
CREATE INDEX IF NOT EXISTS idx_flex_trades_order ON flex_trades (ib_order_id);

CREATE TABLE IF NOT EXISTS hub_outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    at          TEXT NOT NULL,      -- when it happened: ISO-8601 UTC, Z
    queued_at   REAL NOT NULL,      -- hub clock, for the queue's own age
    payload     TEXT NOT NULL       -- the complete event JSON
);
"""


def get_connection() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Tables whose keys gained master_account, and how to tell an old one.
# `trades` already had the column -- what it lacked was the column in its
# UNIQUE constraint, so the column test alone would miss it.
_ACCOUNT_KEYED = {
    "slave_trades": "master_account",
    "engine_positions": "master_account",
    "trades": "UNIQUE (master_account, master_ticket, action",
}


def _tables_predating_account_keys(conn: sqlite3.Connection) -> List[str]:
    stale = []
    for table, marker in _ACCOUNT_KEYED.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is not None and marker not in (row["sql"] or ""):
            stale.append(table)
    return stale


def _migrate_to_account_keys(conn: sqlite3.Connection) -> None:
    """Rebuild the tables whose keys assumed a single master.

    SQLite cannot alter a UNIQUE constraint or a PRIMARY KEY, so each stale
    table is renamed aside, recreated from SCHEMA, refilled, and dropped.

    Every existing row came from a single-master setup, so '' is the correct
    value for them rather than a guess -- and it is what a bridge EA that
    sends no account still produces, which keeps those rows reachable.
    """
    stale = _tables_predating_account_keys(conn)
    if not stale:
        return

    logger.warning("migrating %s to account-aware keys", ", ".join(sorted(stale)))
    for table in stale:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}__pre_account")

    conn.executescript(SCHEMA)  # recreates them with the new keys

    for table in stale:
        old_cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table}__pre_account)")]
        new_cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        shared = [c for c in old_cols if c in new_cols]
        # trades carried a nullable master_account; the column is NOT NULL now.
        selects = ["COALESCE(master_account, '')" if c == "master_account" else c for c in shared]
        conn.execute(
            f"INSERT INTO {table} ({', '.join(shared)}) "
            f"SELECT {', '.join(selects)} FROM {table}__pre_account"
        )
        moved = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        conn.execute(f"DROP TABLE {table}__pre_account")
        logger.warning("  %s: %d row(s) carried over", table, moved)


# Columns added after a table already existed. Unlike a key change these
# need no rebuild -- SQLite can ALTER TABLE ADD COLUMN -- so they stay
# separate from the account-key migration, which does.
_ADDED_COLUMNS = {
    "slave_trades": [
        ("broker_order_id", "TEXT"),
        ("master_price", "REAL"),
        ("slave_price", "REAL"),
        ("slave_lots", "REAL"),
        ("requested_at", "REAL"),
        ("filled_at", "REAL"),
        ("master_close_price", "REAL"),
        ("slave_close_price", "REAL"),
    ],
    "engine_positions": [
        ("atr_period", "INTEGER"),
        ("bar_size", "TEXT"),
    ],
}


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                logger.warning("added column %s.%s", table, name)


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _migrate_to_account_keys(conn)
        _add_missing_columns(conn)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def db_cursor() -> Iterator[sqlite3.Cursor]:
    conn = get_connection()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def get_config_value(key: str) -> Optional[str]:
    with db_cursor() as cur:
        row = cur.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_config_value(key: str, value: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO config (key, value) VALUES (?, ?)
            ON CONFLICT (key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def insert_log(ts: float, level: str, logger_name: str, message: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            "INSERT INTO logs (ts, level, logger, message) VALUES (?, ?, ?, ?)",
            (ts, level, logger_name, message),
        )


def list_logs(limit: int = 200, level: Optional[str] = None):
    query = "SELECT ts, level, logger, message FROM logs"
    params: list = []
    if level:
        query += " WHERE level = ?"
        params.append(level)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with db_cursor() as cur:
        rows = cur.execute(query, params).fetchall()
    return [dict(row) for row in rows]
