import time
from dataclasses import dataclass
from typing import Any, Dict, List

from app.db import db_cursor


@dataclass
class SlaveTradeMapping:
    slave_symbol: str
    slave_ticket: int


def record_open(slave_id: str, master_ticket: int, slave_symbol: str, slave_ticket: int) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO slave_trades (slave_id, master_ticket, slave_symbol, slave_ticket, status, opened_at)
            VALUES (?, ?, ?, ?, 'OPEN', ?)
            ON CONFLICT (slave_id, master_ticket, slave_symbol) DO UPDATE SET
                slave_ticket = excluded.slave_ticket,
                status       = 'OPEN',
                opened_at    = excluded.opened_at,
                closed_at    = NULL
            """,
            (slave_id, master_ticket, slave_symbol, slave_ticket, time.time()),
        )


def get_open_mappings(slave_id: str, master_ticket: int) -> List[SlaveTradeMapping]:
    """One master_ticket can map to several slave symbols on the same slave
    (e.g. XAUUSD -> MGC and GLD both opened), so this always returns a list.
    """
    with db_cursor() as cur:
        rows = cur.execute(
            """
            SELECT slave_symbol, slave_ticket FROM slave_trades
            WHERE slave_id = ? AND master_ticket = ? AND status = 'OPEN'
            """,
            (slave_id, master_ticket),
        ).fetchall()
    return [SlaveTradeMapping(slave_symbol=row["slave_symbol"], slave_ticket=row["slave_ticket"]) for row in rows]


@dataclass
class OpenSlaveTrade:
    master_ticket: int
    slave_symbol: str
    slave_ticket: int


def get_all_open_for_slave(slave_id: str) -> List[OpenSlaveTrade]:
    """Every OPEN row for a slave, regardless of master_ticket -- used for
    startup reconciliation against the broker's live positions.
    """
    with db_cursor() as cur:
        rows = cur.execute(
            "SELECT master_ticket, slave_symbol, slave_ticket FROM slave_trades WHERE slave_id = ? AND status = 'OPEN'",
            (slave_id,),
        ).fetchall()
    return [
        OpenSlaveTrade(master_ticket=row["master_ticket"], slave_symbol=row["slave_symbol"], slave_ticket=row["slave_ticket"])
        for row in rows
    ]


def record_close(slave_id: str, master_ticket: int, slave_symbol: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE slave_trades SET status = 'CLOSED', closed_at = ?
            WHERE slave_id = ? AND master_ticket = ? AND slave_symbol = ?
            """,
            (time.time(), slave_id, master_ticket, slave_symbol),
        )


def list_trades_with_copies(limit: int = 200) -> List[Dict[str, Any]]:
    """Recent master signals (the activity feed's history), each with the
    slave-side copies that resulted from it, if any.
    """
    with db_cursor() as cur:
        trades = cur.execute(
            """
            SELECT id, master_ticket, master_account, symbol, action, direction,
                   lots, price, sl, tp, signal_ts, received_at
            FROM trades ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()

        results = []
        for trade in trades:
            copies = cur.execute(
                """
                SELECT slave_id, slave_symbol, slave_ticket, status, opened_at, closed_at
                FROM slave_trades WHERE master_ticket = ?
                """,
                (trade["master_ticket"],),
            ).fetchall()
            results.append({**dict(trade), "slave_copies": [dict(c) for c in copies]})
        return results
