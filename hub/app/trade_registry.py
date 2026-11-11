import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.db import db_cursor


@dataclass
class SlaveTradeMapping:
    slave_symbol: str
    slave_ticket: int


def record_open(
    slave_id: str,
    master_account: str,
    master_ticket: int,
    slave_symbol: str,
    slave_ticket: int,
    *,
    master_price: Optional[float] = None,
    slave_price: Optional[float] = None,
    slave_lots: Optional[float] = None,
    requested_at: Optional[float] = None,
    filled_at: Optional[float] = None,
) -> None:
    """Record the copy, and what it executed at.

    The execution fields are keyword-only and optional because not every
    broker reports a fill price synchronously -- IBKR's market order comes
    back with an order id and fills later -- and None has to keep meaning
    "not captured" rather than silently becoming a price of zero.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO slave_trades
                (slave_id, master_account, master_ticket, slave_symbol, slave_ticket, status,
                 opened_at, master_price, slave_price, slave_lots, requested_at, filled_at)
            VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)
            ON CONFLICT (slave_id, master_account, master_ticket, slave_symbol) DO UPDATE SET
                slave_ticket = excluded.slave_ticket,
                status       = 'OPEN',
                opened_at    = excluded.opened_at,
                closed_at    = NULL,
                master_price = excluded.master_price,
                slave_price  = excluded.slave_price,
                slave_lots   = excluded.slave_lots,
                requested_at = excluded.requested_at,
                filled_at    = excluded.filled_at
            """,
            (slave_id, master_account, master_ticket, slave_symbol, slave_ticket, time.time(),
             master_price, slave_price, slave_lots, requested_at, filled_at),
        )


def get_open_mappings(
    slave_id: str, master_account: str, master_ticket: int
) -> List[SlaveTradeMapping]:
    """One master_ticket can map to several slave symbols on the same slave
    (e.g. XAUUSD -> MGC and GLD both opened), so this always returns a list.

    Scoped by master_account as well, because ticket numbers are per-account:
    without it a CLOSE from one master could resolve to -- and close --
    another master's leg on the same slave.
    """
    with db_cursor() as cur:
        rows = cur.execute(
            """
            SELECT slave_symbol, slave_ticket FROM slave_trades
            WHERE slave_id = ? AND master_account = ? AND master_ticket = ? AND status = 'OPEN'
            """,
            (slave_id, master_account, master_ticket),
        ).fetchall()
    return [SlaveTradeMapping(slave_symbol=row["slave_symbol"], slave_ticket=row["slave_ticket"]) for row in rows]


@dataclass
class OpenSlaveTrade:
    master_account: str
    master_ticket: int
    slave_symbol: str
    slave_ticket: int


def get_all_open_for_slave(slave_id: str) -> List[OpenSlaveTrade]:
    """Every OPEN row for a slave, regardless of master_ticket -- used for
    startup reconciliation against the broker's live positions.
    """
    with db_cursor() as cur:
        rows = cur.execute(
            """
            SELECT master_account, master_ticket, slave_symbol, slave_ticket
            FROM slave_trades WHERE slave_id = ? AND status = 'OPEN'
            """,
            (slave_id,),
        ).fetchall()
    return [
        OpenSlaveTrade(
            master_account=row["master_account"],
            master_ticket=row["master_ticket"],
            slave_symbol=row["slave_symbol"],
            slave_ticket=row["slave_ticket"],
        )
        for row in rows
    ]


def record_close(
    slave_id: str,
    master_account: str,
    master_ticket: int,
    slave_symbol: str,
    *,
    master_close_price: Optional[float] = None,
    slave_close_price: Optional[float] = None,
) -> None:
    """Close the row, keeping any exit prices that were reported.

    COALESCE rather than assignment: reconciliation also closes rows, and it
    has no prices to offer -- overwriting a captured fill with NULL would
    lose the half of the round trip that was actually measured.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE slave_trades SET
                status = 'CLOSED',
                closed_at = ?,
                master_close_price = COALESCE(?, master_close_price),
                slave_close_price  = COALESCE(?, slave_close_price)
            WHERE slave_id = ? AND master_account = ? AND master_ticket = ? AND slave_symbol = ?
            """,
            (time.time(), master_close_price, slave_close_price,
             slave_id, master_account, master_ticket, slave_symbol),
        )


def list_trades_with_copies(limit: int = 200, since: Optional[float] = None) -> List[Dict[str, Any]]:
    """Recent master signals (the activity feed's history), each with the
    slave-side copies that resulted from it, if any.

    `since` is an epoch second: the copier view asks for a whole day, which
    is a span rather than a count, and guessing a limit large enough to cover
    a busy day across several masters would silently truncate the quiet end
    of it.
    """
    with db_cursor() as cur:
        trades = cur.execute(
            """
            SELECT id, master_ticket, master_account, symbol, action, direction,
                   lots, price, sl, tp, signal_ts, received_at
            FROM trades
            WHERE (? IS NULL OR received_at >= ?)
            ORDER BY id DESC LIMIT ?
            """,
            (since, since, limit),
        ).fetchall()

        results = []
        for trade in trades:
            copies = cur.execute(
                """
                SELECT slave_id, slave_symbol, slave_ticket, status, opened_at, closed_at,
                       master_price, slave_price, slave_lots, requested_at, filled_at,
                       master_close_price, slave_close_price
                FROM slave_trades WHERE master_account = ? AND master_ticket = ?
                """,
                (trade["master_account"], trade["master_ticket"]),
            ).fetchall()
            results.append({**dict(trade), "slave_copies": [dict(c) for c in copies]})
        return results
