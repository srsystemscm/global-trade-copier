"""Master orders and slave legs as rows, for schema 1.1.

The reporter sent a snapshot and an append-only event stream. The hub's
Copier tab wants one row per master order with its legs beneath, and could
fold that out of the events -- but the copier already holds exactly those
rows in its registry, so folding them again in the hub is two
implementations of one truth that drift the first time an event type is
added. The events remain the record; these rows are the copier saying what
it currently believes.

Both are UPSERTS, keyed as the registry keys them:

    orders  (master_account, master_ticket)
    legs    (master_account, master_ticket, slave_id)

Never `master_ticket` alone. MT4 ticket numbers are per account, so two
masters legitimately issue the same number, and a key without the account
lets one master's CLOSE land on another's row.

Sent when they change, so a quiet hub costs nothing. Resending is harmless
by construction: the hub writes them by key, and a full resend after an
outage repairs whatever was missed.
"""
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from app.db import db_cursor

logger = logging.getLogger(__name__)

# What a cold start sends. Everything the registry holds would be the whole
# history on the first report after an upgrade; a day covers an outage or a
# restart, which is what the resend is for.
DEFAULT_BACKFILL_S = 24 * 3600


def _iso(ts: Optional[float]) -> Optional[str]:
    from app.hub_reporter.catalogue import iso
    return iso(ts)


def _str_or_none(value: Any) -> Optional[str]:
    """Identifiers travel as strings, and only when they exist.

    MT5 tickets are ulong and account numbers are long enough that a JSON
    number is a precision question nobody should have to think about. "" and
    0 are not identifiers -- the hub reads null as "not known".
    """
    if value in (None, "", 0):
        return None
    return str(value)


def _changed_tickets(cur, since: float) -> List[Tuple[str, int]]:
    """Which (account, ticket) pairs have moved since `since`.

    A leg can change long after its signal -- a deferred open can fill forty
    minutes later, and a close lands whenever the master closes -- so the
    leg timestamps are asked about as well as the signal's. slave_trades has
    no single updated_at, so the latest of the four times it does keep
    stands in for one.
    """
    rows = cur.execute(
        """
        SELECT DISTINCT master_account, master_ticket FROM (
            SELECT master_account, master_ticket FROM trades
             WHERE received_at > ?
            UNION
            SELECT master_account, master_ticket FROM slave_trades
             WHERE MAX(COALESCE(requested_at, 0), COALESCE(filled_at, 0),
                       COALESCE(opened_at, 0), COALESCE(closed_at, 0)) > ?
        )
        """,
        (since, since),
    ).fetchall()
    return [(r["master_account"], r["master_ticket"]) for r in rows]


def _fold_order(signals: List[Any]) -> Optional[Dict[str, Any]]:
    """One master order, folded out of its OPEN / MODIFY* / CLOSE signals.

    `trades` stores one row per signal rather than per order, so the order
    is the fold. Sending the state rather than each publish is also what
    absorbs a master EA publishing the same CLOSE twice a second apart --
    seen on 2026-11-11, ticket 382156066.
    """
    if not signals:
        return None

    opened = next((s for s in signals if s["action"] == "OPEN"), None)
    closed = next((s for s in reversed(signals) if s["action"] == "CLOSE"), None)
    latest = signals[-1]
    modifies = sum(1 for s in signals if s["action"] == "MODIFY")

    magic = None
    raw = (opened or latest)["raw_json"]
    if raw:
        try:
            magic = json.loads(raw).get("magic")
        except (ValueError, TypeError):
            magic = None

    return {
        "master_account": str(latest["master_account"]),
        "master_ticket": _str_or_none(latest["master_ticket"]),
        "magic": magic,
        "symbol": latest["symbol"],
        "direction": (opened or latest)["direction"],
        "lots": (opened or latest)["lots"],
        "opened_at": _iso(opened["received_at"]) if opened else None,
        "open_price": opened["price"] if opened else None,
        # The newest levels, which is what a stop-widening MODIFY leaves
        # behind -- not the ones the order opened with.
        "sl": latest["sl"],
        "tp": latest["tp"],
        "modifies": modifies,
        "closed_at": _iso(closed["received_at"]) if closed else None,
        "close_price": closed["price"] if closed else None,
        "updated_at": _iso(latest["received_at"]),
    }


def _leg(row: Any, facts: Any) -> Dict[str, Any]:
    """One slave leg.

    Several fields the contract defines are not in the registry and are
    sent as null rather than guessed:

    `slave_direction` -- the TRADED side. It is the master's for most
    routes and the opposite for an inverse ETF, and which applies is a
    route property the registry does not record. Sending the master's would
    be wrong exactly where the field matters.

    `requested_price` -- the level the order aimed at. The engines report it
    on the slave_open EVENT, which carries it; the registry keeps the fill.
    """
    slave_id = row["slave_id"]
    latest = max(
        row["requested_at"] or 0, row["filled_at"] or 0,
        row["opened_at"] or 0, row["closed_at"] or 0,
    ) or None

    return {
        "master_account": str(row["master_account"]),
        "master_ticket": _str_or_none(row["master_ticket"]),
        "slave_id": slave_id,
        # Here as well as in status.slaves[], so a row can be tied to the
        # hub's ledger without a second lookup.
        "slave_account": facts.slave_account(slave_id),
        "slave_ticket": _str_or_none(row["slave_ticket"]),
        # The BROKER's own id, and the copier's own reference. The reference
        # is the better key: it is stamped on every order including the
        # protective legs, while the broker's id is on the entry alone.
        "broker_order_id": _str_or_none(row["broker_order_id"]),
        "broker_order_ref": _str_or_none(row["broker_order_ref"]),
        "autonomous": facts.is_autonomous(slave_id),
        "slave_symbol": row["slave_symbol"],
        "slave_direction": None,
        "size": row["slave_lots"],
        "size_unit": facts.size_unit(slave_id, row["slave_symbol"]),
        "master_price": row["master_price"],
        "requested_price": None,
        "fill_price": row["slave_price"],
        "master_close_price": row["master_close_price"],
        "slave_close_price": row["slave_close_price"],
        "state": (row["status"] or "").lower() or None,
        "requested_at": _iso(row["requested_at"]),
        "filled_at": _iso(row["filled_at"]),
        "updated_at": _iso(latest),
    }


def since(watermark: Optional[float], backfill_s: float = DEFAULT_BACKFILL_S) -> float:
    """Where to read from: the watermark, or a bounded backfill on a cold start."""
    if watermark is not None:
        return watermark
    return time.time() - backfill_s


def build(watermark: Optional[float],
          facts: Any,
          backfill_s: float = DEFAULT_BACKFILL_S,
          max_orders: int = 200) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float]:
    """(orders, legs, new_watermark) for everything that moved since.

    The watermark advances to the moment the read began rather than to the
    newest row seen: a row written while this was reading would otherwise
    be skipped forever. Re-sending a row is free; missing one is not.
    """
    started = time.time()
    try:
        with db_cursor() as cur:
            pairs = _changed_tickets(cur, since(watermark, backfill_s))
            if not pairs:
                return [], [], started

            # Oldest first, so a backlog is sent in the order it happened
            # and a cap drops the newest rather than the oldest -- the
            # newest will come again on the next report anyway.
            pairs = pairs[:max_orders]

            orders: List[Dict[str, Any]] = []
            legs: List[Dict[str, Any]] = []
            for account, ticket in pairs:
                signals = cur.execute(
                    """
                    SELECT master_account, master_ticket, symbol, action, direction,
                           lots, price, sl, tp, received_at, raw_json
                    FROM trades
                    WHERE master_account = ? AND master_ticket = ?
                    ORDER BY id
                    """,
                    (account, ticket),
                ).fetchall()
                order = _fold_order(signals)
                if order is not None:
                    orders.append(order)

                for row in cur.execute(
                    """
                    SELECT * FROM slave_trades
                    WHERE master_account = ? AND master_ticket = ?
                    """,
                    (account, ticket),
                ).fetchall():
                    legs.append(_leg(row, facts))

            return orders, legs, started
    except Exception as exc:  # noqa: BLE001 -- reporting must never break copying
        logger.warning("hub reporter: could not build order/leg rows: %s", exc)
        # The watermark is NOT advanced, so the next report tries the same
        # span again rather than skipping it.
        return [], [], watermark if watermark is not None else started
