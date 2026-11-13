"""Durable state for opens and closes the broker could not take yet.

Both deferral queues lived only in the engine's memory, and the comments
where they were built said as much: "a hub restart mid-halt loses them".
That is a daily exposure, not a rare one -- CME halts its whole complex for
an hour every day, an MT4 server on GMT+2/+3 opens its daily bar right on
that boundary, and IB Gateway forces a restart once a day that the hub
restarts with.

The close side is the worse loss. A deferred close waits up to 72 hours by
default, because a Friday evening exit legitimately waits for Monday, and
losing one leaves the slave holding a position the master has exited with
nothing retrying it. Reconciliation cannot find that either: the registry
says OPEN and the broker agrees, because nothing ever recorded the
master's exit.

What is stored is the signal and the key. The route and the registry
mapping are deliberately NOT stored -- they are re-derived when the engine
restores, so:

- a `Route` that gains a field does not leave stale JSON behind to be
  reconstructed into a half-populated object;
- a config edited while the hub was down is honoured. If the route was
  removed, the deferred open is dropped rather than opened into a route
  nobody has any more;
- a close whose registry row is no longer OPEN is dropped, because
  something else has already resolved it.
"""
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.db import db_cursor
from app.models import Signal

logger = logging.getLogger(__name__)

OPEN = "OPEN"
CLOSE = "CLOSE"


@dataclass
class DeferredRow:
    """One held action, as stored. `signal` is already parsed."""

    kind: str
    slave_symbol: str
    month_slot: int
    signal: Signal
    sl_mult: Optional[float]
    tp_mult: Optional[float]
    first_at: float
    next_at: float
    attempts: int
    last_error: Optional[str]

    @property
    def key(self) -> tuple:
        """The key the engine's own dict uses.

        Deliberately the two different shapes rather than one unified one:
        the engine's `_deferred` is keyed by target and month slot and its
        `_deferred_closes` by symbol, and changing either to match the
        other would mean touching every lookup for the sake of this module.
        """
        account = self.signal.account
        ticket = self.signal.ticket
        if self.kind == OPEN:
            return (account, ticket, self.slave_symbol, self.month_slot)
        return (account, ticket, self.slave_symbol)

    def as_item(self, **extra: Any) -> Dict[str, Any]:
        """The engine's in-memory item, minus whatever it re-derives."""
        item = {
            "signal": self.signal,
            "first_at": self.first_at,
            "next_at": self.next_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }
        if self.kind == OPEN:
            item["sl_mult"] = self.sl_mult
            item["tp_mult"] = self.tp_mult
        item.update(extra)
        return item


def save(
    slave_id: str,
    kind: str,
    signal: Signal,
    slave_symbol: str,
    item: Dict[str, Any],
    month_slot: int = 0,
) -> None:
    """Upsert one held action.

    Called on every change -- deferred, retried, rescheduled -- so a restart
    never loses more than the last attempt's bookkeeping. Failures are
    logged and swallowed: this is durability for a retry loop, and a
    database that will not take the row must not stop the retry itself from
    happening in memory.
    """
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                INSERT INTO deferred_actions
                    (slave_id, kind, master_account, master_ticket, slave_symbol,
                     month_slot, signal_json, sl_mult, tp_mult, first_at,
                     next_at, attempts, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (slave_id, kind, master_account, master_ticket,
                             slave_symbol, month_slot) DO UPDATE SET
                    signal_json = excluded.signal_json,
                    sl_mult     = excluded.sl_mult,
                    tp_mult     = excluded.tp_mult,
                    first_at    = excluded.first_at,
                    next_at     = excluded.next_at,
                    attempts    = excluded.attempts,
                    last_error  = excluded.last_error,
                    updated_at  = excluded.updated_at
                """,
                (
                    slave_id, kind, signal.account, signal.ticket, slave_symbol,
                    int(month_slot), signal.model_dump_json(),
                    item.get("sl_mult"), item.get("tp_mult"),
                    item["first_at"], item["next_at"],
                    int(item.get("attempts", 1)), item.get("last_error"),
                    time.time(),
                ),
            )
    except Exception:
        logger.exception(
            "slave=%s could not persist the deferred %s of %s for ticket=%s; "
            "it is still held in memory and will still be retried, but a "
            "restart before it resolves will lose it",
            slave_id, kind, slave_symbol, signal.ticket,
        )


def delete(
    slave_id: str, kind: str, master_account: str, master_ticket: int,
    slave_symbol: str, month_slot: int = 0,
) -> None:
    """Forget one held action: it filled, was cancelled, or was abandoned."""
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                DELETE FROM deferred_actions
                WHERE slave_id = ? AND kind = ? AND master_account = ?
                  AND master_ticket = ? AND slave_symbol = ? AND month_slot = ?
                """,
                (slave_id, kind, master_account, master_ticket, slave_symbol,
                 int(month_slot)),
            )
    except Exception:
        logger.exception("slave=%s could not drop the deferred %s of %s",
                         slave_id, kind, slave_symbol)


def delete_for_ticket(slave_id: str, master_account: str, master_ticket: int) -> int:
    """Forget every held action for one master ticket.

    The master closing a trade cancels any open still waiting for it, which
    the engine does in one step rather than per key.
    """
    try:
        with db_cursor() as cur:
            cur.execute(
                """
                DELETE FROM deferred_actions
                WHERE slave_id = ? AND master_account = ? AND master_ticket = ?
                  AND kind = ?
                """,
                (slave_id, master_account, master_ticket, OPEN),
            )
            return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    except Exception:
        logger.exception("slave=%s could not drop deferred opens for ticket=%s",
                         slave_id, master_ticket)
        return 0


def load_all(slave_id: str, kind: Optional[str] = None) -> List[DeferredRow]:
    """Every held action for a slave, oldest first.

    Oldest first so a restore rebuilds the queue in the order things were
    deferred, which is the order they should be retried in.

    A row whose signal will not parse is dropped and logged rather than
    raised: it would otherwise stop the engine from starting at all, and an
    engine that will not start is a worse outcome than one deferral lost.
    """
    query = "SELECT * FROM deferred_actions WHERE slave_id = ?"
    params: list = [slave_id]
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY first_at, master_ticket"

    with db_cursor() as cur:
        rows = cur.execute(query, params).fetchall()

    out: List[DeferredRow] = []
    for row in rows:
        try:
            signal = Signal.model_validate(json.loads(row["signal_json"]))
        except Exception as exc:
            logger.error(
                "slave=%s dropping an unreadable deferred %s of %s for "
                "ticket=%s: %s",
                slave_id, row["kind"], row["slave_symbol"], row["master_ticket"], exc,
            )
            continue
        out.append(DeferredRow(
            kind=row["kind"],
            slave_symbol=row["slave_symbol"],
            month_slot=row["month_slot"],
            signal=signal,
            sl_mult=row["sl_mult"],
            tp_mult=row["tp_mult"],
            first_at=row["first_at"],
            next_at=row["next_at"],
            attempts=row["attempts"],
            last_error=row["last_error"],
        ))
    return out
