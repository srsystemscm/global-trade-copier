"""Durable per-position state for autonomous engines.

Why this exists: the Trade Registry records *that* a position was opened, but
everything needed to manage or close it afterwards lived only in memory -- the
engine's ATR multiples and breakeven flag, and the adapter's contract handle
and bracket order ids.

A hub restart therefore orphaned every open position: the trailing monitor
silently stopped managing it, and `close()` raised "no tracked position" when
the master finally closed, leaving a live position the copier could no longer
touch. Reconciliation didn't help -- it repairs the registry, never this.

That stops being an annoyance on a VPS, where IB Gateway forces a daily
restart and the hub restarts with it.
"""
import logging
import time
from typing import Any, Dict, List, Tuple

from app.db import db_cursor

logger = logging.getLogger(__name__)

_FIELDS = (
    "direction", "size", "entry_price", "atr", "sl", "tp", "sl_mult", "tp_mult",
    "breakeven_done", "month_slot", "contract_id", "contract_local",
    "contract_expiry", "sl_order_id", "tp_order_id",
    # Which ATR period measured this position. Restored so a roll after a
    # restart re-derives stops the way the entry was measured rather than
    # from whatever the slave default is by then.
    "atr_period",
)


def save(slave_id: str, master_account: str, master_ticket: int, slave_symbol: str,
         pos: Dict[str, Any]) -> None:
    """Upsert one position's state. Called whenever it changes -- open, roll,
    trailing stop move -- so a restart never loses more than the last tick."""
    values = {f: pos.get(f) for f in _FIELDS}
    values["breakeven_done"] = int(bool(values.get("breakeven_done")))
    cols = ", ".join(_FIELDS)
    placeholders = ", ".join("?" for _ in _FIELDS)
    updates = ", ".join(f"{f} = excluded.{f}" for f in _FIELDS)
    with db_cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO engine_positions
                (slave_id, master_account, master_ticket, slave_symbol, slave_ticket, {cols}, updated_at)
            VALUES (?, ?, ?, ?, ?, {placeholders}, ?)
            ON CONFLICT (slave_id, master_account, master_ticket, slave_symbol) DO UPDATE SET
                slave_ticket = excluded.slave_ticket,
                {updates},
                updated_at = excluded.updated_at
            """,
            (slave_id, master_account, master_ticket, slave_symbol, pos["slave_ticket"],
             *[values[f] for f in _FIELDS], time.time()),
        )


def delete(slave_id: str, master_account: str, master_ticket: int, slave_symbol: str) -> None:
    with db_cursor() as cur:
        cur.execute(
            """
            DELETE FROM engine_positions
            WHERE slave_id = ? AND master_account = ? AND master_ticket = ? AND slave_symbol = ?
            """,
            (slave_id, master_account, master_ticket, slave_symbol),
        )


def load_all(slave_id: str) -> List[Tuple[Tuple[str, int, str], Dict[str, Any]]]:
    """Every stored position for a slave, shaped like the engine's own
    in-memory dict so it can be restored directly."""
    with db_cursor() as cur:
        rows = cur.execute(
            "SELECT * FROM engine_positions WHERE slave_id = ? ORDER BY master_ticket", (slave_id,)
        ).fetchall()

    out = []
    for row in rows:
        d = dict(row)
        pos: Dict[str, Any] = {f: d[f] for f in _FIELDS}
        pos["slave_ticket"] = d["slave_ticket"]
        pos["breakeven_done"] = bool(d["breakeven_done"])
        out.append(((d["master_account"], d["master_ticket"], d["slave_symbol"]), pos))
    return out
