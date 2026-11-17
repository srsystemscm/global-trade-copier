import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.db import db_cursor


@dataclass
class SlaveTradeMapping:
    slave_symbol: str
    slave_ticket: int


def order_ref(master_account: str, master_ticket: int, slave_symbol: str) -> str:
    """The copier's reference for one leg, as stamped on the broker order.

    Carries the three things that identify a copy -- which master, which
    trade of that master, and which instrument it became here -- because
    those are exactly this table's own key, so a row on the broker's
    statement can be traced back to the row that caused it.

    Kept short deliberately: IBKR truncates an over-long orderRef
    silently, and a truncated reference would fail by matching nothing
    rather than by complaining. The `gtc` tag marks it as this copier's,
    so an execution placed by hand or by another tool is distinguishable
    from one of ours rather than merely absent.
    """
    return f"gtc:{master_account}:{master_ticket}:{slave_symbol}"


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
    broker_order_id: Optional[str] = None,
    broker_order_ref: Optional[str] = None,
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
                 opened_at, master_price, slave_price, slave_lots, requested_at, filled_at,
                 broker_order_id, broker_order_ref)
            VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (slave_id, master_account, master_ticket, slave_symbol) DO UPDATE SET
                slave_ticket = excluded.slave_ticket,
                status       = 'OPEN',
                opened_at    = excluded.opened_at,
                closed_at    = NULL,
                master_price = excluded.master_price,
                slave_price  = excluded.slave_price,
                slave_lots   = excluded.slave_lots,
                requested_at = excluded.requested_at,
                filled_at    = excluded.filled_at,
                -- COALESCE, not excluded: a re-record that does not know
                -- the broker's id must not erase one already captured.
                broker_order_id = COALESCE(excluded.broker_order_id,
                                           slave_trades.broker_order_id),
                broker_order_ref = COALESCE(excluded.broker_order_ref,
                                            slave_trades.broker_order_ref)
            """,
            (slave_id, master_account, master_ticket, slave_symbol, slave_ticket, time.time(),
             master_price, slave_price, slave_lots, requested_at, filled_at,
             broker_order_id, broker_order_ref),
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


def record_entry_cancelled(
    slave_id: str,
    master_account: str,
    master_ticket: int,
    slave_symbol: str,
) -> bool:
    """Close out a row whose entry never filled.

    RETURNS whether this call is the one that moved it. Two paths reach
    an unfilled entry for the same ticket -- the master's CLOSE, and the
    background fill-capture task when its own wait runs out -- and on UGL
    1193 those were 30 seconds apart. Both are right to ask; only the
    first has something to report. Without this they each raised the
    "the master IS in this trade and this leg is NOT" alert, and each
    sent its own notification, for one event.

    CANCELLED rather than CLOSED, and deliberately not deleted: the signal
    did reach this slave and produced no position, which is exactly the kind
    of thing the variance work exists to notice. Deleting it would make a
    missed fill indistinguishable from a signal that never arrived.

    Moving it out of OPEN is also what stops reconciliation reporting a
    position the broker has never had.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE slave_trades SET status = 'CANCELLED', closed_at = ?
            WHERE slave_id = ? AND master_account = ? AND master_ticket = ?
              AND slave_symbol = ? AND status = 'OPEN'
            """,
            (time.time(), slave_id, master_account, master_ticket, slave_symbol),
        )
        # The WHERE clause carries status = 'OPEN', so a second call
        # matches nothing. That is what makes this answer meaningful
        # rather than a guess about who got here first.
        return cur.rowcount > 0


def record_entry_slices(
    slave_id: str,
    master_account: str,
    master_ticket: int,
    slave_symbol: str,
    slave_ticket: Optional[int],
    slices: Optional[List[Dict[str, Any]]],
) -> None:
    """What each order of a split entry was, and what it filled.

    The position is one row in slave_trades and that is what everything
    trades from. These rows are a MEASUREMENT and nothing else: the
    reason to split a 33-lot entry is market impact, and the position's
    single average price is the number the splitting was meant to
    improve, so it cannot be the thing that judges it. Comparing slice 1
    against slice 3 can.

    Upserted on the slice number, so a re-report corrects rather than
    duplicates. A slice that filled nothing still gets a row -- "placed
    11, filled none" is a measurement too, and it is how a window that
    was cut short reads afterwards.
    """
    if not slices:
        return
    with db_cursor() as cur:
        for row in slices:
            cur.execute(
                """
                INSERT INTO slave_trade_slices (
                    slave_id, master_account, master_ticket, slave_symbol,
                    slave_ticket, n, broker_order_id, size, filled,
                    fill_price, placed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (slave_id, master_account, master_ticket,
                             slave_symbol, n)
                DO UPDATE SET
                    slave_ticket    = excluded.slave_ticket,
                    broker_order_id = COALESCE(excluded.broker_order_id,
                                               broker_order_id),
                    size            = excluded.size,
                    filled          = excluded.filled,
                    fill_price      = excluded.fill_price,
                    placed_at       = COALESCE(excluded.placed_at, placed_at)
                """,
                (slave_id, master_account, master_ticket, slave_symbol,
                 slave_ticket, int(row.get("n") or 0),
                 row.get("broker_order_id"), row.get("size"),
                 row.get("filled"), row.get("fill_price"),
                 row.get("placed_at")),
            )


def entry_slices_for(slave_id: str, master_account: str, master_ticket: int,
                     slave_symbol: str) -> List[Dict[str, Any]]:
    """One entry's slices, in the order they went out."""
    with db_cursor() as cur:
        return [dict(r) for r in cur.execute(
            """
            SELECT n, broker_order_id, size, filled, fill_price, placed_at
            FROM slave_trade_slices
            WHERE slave_id = ? AND master_account = ? AND master_ticket = ?
              AND slave_symbol = ?
            ORDER BY n
            """,
            (slave_id, master_account, master_ticket, slave_symbol),
        ).fetchall()]


def record_fill(
    slave_id: str,
    master_account: str,
    master_ticket: int,
    slave_symbol: str,
    *,
    slave_price: Optional[float] = None,
    slave_lots: Optional[float] = None,
    filled_at: Optional[float] = None,
    entry_slices: Optional[int] = None,
) -> None:
    """Write in an execution that arrived after the row was created.

    For brokers that fill asynchronously the row is inserted the moment the
    order is accepted, with no price; this fills that in once the broker
    reports it.

    COALESCE on every field, so a later NULL cannot erase a captured value,
    and so this is safe to call when the fill never arrived.
    """
    with db_cursor() as cur:
        cur.execute(
            """
            UPDATE slave_trades SET
                slave_price  = COALESCE(?, slave_price),
                slave_lots   = COALESCE(?, slave_lots),
                filled_at    = COALESCE(?, filled_at),
                entry_slices = COALESCE(?, entry_slices)
            WHERE slave_id = ? AND master_account = ? AND master_ticket = ? AND slave_symbol = ?
            """,
            (slave_price, slave_lots, filled_at, entry_slices,
             slave_id, master_account, master_ticket, slave_symbol),
        )


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
                       master_close_price, slave_close_price, entry_slices
                FROM slave_trades WHERE master_account = ? AND master_ticket = ?
                """,
                (trade["master_account"], trade["master_ticket"]),
            ).fetchall()
            copied = []
            for c in copies:
                leg = dict(c)
                # Only for an entry that was actually split. One slice is
                # an ordinary entry, and a one-element list under every
                # leg on the page reads as though something were going on.
                if (leg.get("entry_slices") or 0) > 1:
                    leg["slices"] = [dict(r) for r in cur.execute(
                        """
                        SELECT n, size, filled, fill_price, placed_at
                        FROM slave_trade_slices
                        WHERE slave_id = ? AND master_account = ?
                          AND master_ticket = ? AND slave_symbol = ?
                        ORDER BY n
                        """,
                        (leg["slave_id"], trade["master_account"],
                         trade["master_ticket"], leg["slave_symbol"]),
                    ).fetchall()]
                copied.append(leg)
            results.append({**dict(trade), "slave_copies": copied})
        return results
