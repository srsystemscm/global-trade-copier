import sqlite3
from contextlib import contextmanager
from typing import Iterator, Optional

from app.config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    master_ticket   INTEGER NOT NULL,
    master_account  TEXT,
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
    UNIQUE (master_ticket, signal_ts)
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
    master_ticket   INTEGER NOT NULL,
    slave_symbol    TEXT NOT NULL,      -- the mapped slave-side symbol (e.g. MGC, GLD)
    slave_ticket    INTEGER,
    status          TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | CLOSED
    opened_at       REAL,
    closed_at       REAL,
    UNIQUE (slave_id, master_ticket, slave_symbol)
);

CREATE TABLE IF NOT EXISTS logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    level           TEXT NOT NULL,
    logger          TEXT NOT NULL,
    message         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs (ts);
"""


def get_connection() -> sqlite3.Connection:
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
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
