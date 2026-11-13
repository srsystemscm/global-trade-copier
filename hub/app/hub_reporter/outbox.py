"""The event queue, on disk, in the copier's own database.

Events are what happened; the snapshot is what is true now. Only events are
queued, because a stale snapshot is worthless and a lost one is simply
replaced by the next -- whereas a lost event is a hole in the history that
nothing later fills.
"""
import json
import logging
import sqlite3
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.db import db_cursor, get_config_value, set_config_value

logger = logging.getLogger(__name__)

# Carried in the `config` table rather than only in memory. The count exists
# so backlog loss is visible in the tab rather than silent, and a counter
# that resets on restart would hide exactly the losses a restart follows.
DROPPED_KEY = "hub_reporter_dropped_total"


def new_session_id() -> str:
    """A fresh id for every copier start, so the hub can see a restart."""
    return uuid.uuid4().hex[:8]


class Outbox:
    """Events waiting for the hub to ack them.

    `event_id` is `{copier_id}:{session_id}:{n}`, assigned once here and
    stored with the event. `n` is monotonic within the session; the session
    id is new on every start, so an id is never reused across one.
    """

    def __init__(self, copier_id: str, session_id: str, queue_max: int = 10_000) -> None:
        self.copier_id = copier_id
        self.session_id = session_id
        self.queue_max = max(1, int(queue_max))
        self._n = 0

    # ------------------------------------------------------------------ write

    def queue(self, event: Dict[str, Any], queued_at: Optional[float] = None) -> Optional[str]:
        """Store one event and return its id, or None if it could not be stored.

        Returns rather than raises, and logs rather than propagates: this is
        called from the task that drains the event bus, and a database that
        will not take a row must not be able to kill that task -- let alone
        reach back into the thing that emitted the event.
        """
        self._n += 1
        event_id = f"{self.copier_id}:{self.session_id}:{self._n}"
        row = {"event_id": event_id, **event}
        try:
            with db_cursor() as cur:
                cur.execute(
                    "INSERT INTO hub_outbox (event_id, at, queued_at, payload) "
                    "VALUES (?, ?, ?, ?)",
                    (event_id, str(row.get("at") or ""),
                     queued_at if queued_at is not None else time.time(),
                     json.dumps(row)),
                )
                dropped = self._trim(cur)
        except sqlite3.Error as exc:
            logger.warning("hub reporter: could not queue %s: %s",
                           event.get("type"), exc)
            return None

        if dropped:
            # Counted outside the transaction that did the deleting, so the
            # count can never claim a drop that was rolled back.
            self._record_dropped(dropped)
            logger.warning(
                "hub reporter: outbox is full at %d events -- dropped the %d "
                "oldest. The hub is not acking; recent events are kept.",
                self.queue_max, dropped,
            )
        return event_id

    def _trim(self, cur: sqlite3.Cursor) -> int:
        """Drop the oldest events once the queue is over its ceiling.

        The oldest, not the newest. A backlog this deep means the hub has
        been unreachable or silent for a long time, and the events worth
        keeping are the ones somebody is about to look at.
        """
        depth = cur.execute("SELECT COUNT(*) AS n FROM hub_outbox").fetchone()["n"]
        excess = depth - self.queue_max
        if excess <= 0:
            return 0
        cur.execute(
            "DELETE FROM hub_outbox WHERE id IN "
            "(SELECT id FROM hub_outbox ORDER BY id LIMIT ?)",
            (excess,),
        )
        return excess

    def _record_dropped(self, n: int) -> None:
        set_config_value(DROPPED_KEY, str(self.dropped_total + n))

    # ------------------------------------------------------------------- read

    @property
    def dropped_total(self) -> int:
        try:
            return int(get_config_value(DROPPED_KEY) or 0)
        except (TypeError, ValueError):
            return 0

    def oldest(self, limit: int) -> List[Dict[str, Any]]:
        """Up to `limit` events, oldest first, as the hub will receive them.

        Oldest first because the hub appends and a reader reads a history
        forwards. More than `limit` and the next cycle takes the rest.
        """
        with db_cursor() as cur:
            rows = cur.execute(
                "SELECT payload FROM hub_outbox ORDER BY id LIMIT ?", (int(limit),)
            ).fetchall()
        events = []
        for row in rows:
            try:
                events.append(json.loads(row["payload"]))
            except json.JSONDecodeError:
                # Unreadable rather than lost in transit, and resending it
                # forever would block everything behind it.
                logger.error("hub reporter: dropping an unreadable outbox row")
        return events

    def stats(self) -> Dict[str, Any]:
        """What the snapshot's `queue` block reports.

        A depth that climbs is the symptom of a hub that is accepting
        reports and not acking them, which nothing else would show.
        """
        with db_cursor() as cur:
            row = cur.execute(
                "SELECT COUNT(*) AS depth, MIN(at) AS oldest FROM hub_outbox"
            ).fetchone()
        return {
            "depth": row["depth"],
            # MIN over an ISO-8601 UTC string with a fixed width is the
            # earliest time in it, so this needs no second query.
            "oldest_event_at": row["oldest"] or None,
            "dropped_total": self.dropped_total,
        }

    # -------------------------------------------------------------------- ack

    def ack(self, event_ids: Iterable[str]) -> int:
        """Remove exactly the events the hub said it stored. Nothing else.

        Not "everything up to the last acked id", and not "everything that
        was sent": a hub that stored eight of ten events has to be able to
        say so, and the two it did not store have to still be here next
        cycle. A reply that is missing or malformed acks nothing, which is
        this being called with an empty list.
        """
        ids = [str(i) for i in event_ids if i]
        if not ids:
            return 0
        removed = 0
        with db_cursor() as cur:
            # Chunked: SQLite's parameter limit is 999 by default and a
            # report can carry 200, but the ceiling is configurable.
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                marks = ",".join("?" * len(chunk))
                cur.execute(
                    f"DELETE FROM hub_outbox WHERE event_id IN ({marks})", chunk
                )
                removed += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return removed


def pending_ids() -> List[Tuple[int, str]]:
    """(id, event_id) for everything queued. For tests and diagnostics."""
    with db_cursor() as cur:
        rows = cur.execute("SELECT id, event_id FROM hub_outbox ORDER BY id").fetchall()
    return [(r["id"], r["event_id"]) for r in rows]
