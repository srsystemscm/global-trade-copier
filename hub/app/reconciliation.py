import logging
import time
from typing import Any, Dict, List, Optional

from app import position_store, trade_registry
from app.trade_registry import order_ref
from app.adapters.base import AdapterError, BrokerAdapter
from app.events import EventBus

logger = logging.getLogger(__name__)


def _row_is_pending(row, pending_refs: set, pending_symbols: set) -> bool:
    """Is this registry row's entry still resting at the broker?

    By order_ref first, because that is the string the copier chose and
    stamped: an exact answer for this row rather than for the instrument.
    order_ref() builds it from the master account and ticket, so a row
    whose reference is working is unambiguously this row.

    Symbol is the fallback, for a row written before stamping existed or
    by a path that did not stamp. Looser -- two strategies resting on one
    instrument would each keep the other's row OPEN -- and that is the
    right direction to be wrong in: an extra OPEN row is corrected on the
    next pass, while a position nobody tracks is not.
    """
    ref = order_ref(row.master_account, row.master_ticket, row.slave_symbol)
    if ref in pending_refs:
        return True
    return row.slave_symbol in pending_symbols


async def reconcile_slave(
    slave_id: str, adapter: BrokerAdapter, account_types: List[Optional[str]], events: EventBus
) -> None:
    """Compares the Trade Registry's OPEN rows for this slave against the
    broker's actual live positions, and resolves any drift found while the
    hub was down:

    - Registry says OPEN, broker doesn't have it -> the position closed
      while we weren't watching (SL/TP hit, manually closed, whatever).
      The registry is corrected to CLOSED. This is safe: we know that row
      was our own copy, so if the broker doesn't have it, it closed.
    - Broker has an open position not in the registry -> logged as a
      warning, left alone. We can't safely assume an untracked position is
      ours to touch (could be a manual trade on the same account).
    - Registry says OPEN, broker holds no position, but an order we
      stamped is still WORKING -> left OPEN. The third case, and it used
      to fall into the first: a resting limit entry answers "no position"
      while being alive, so the row was closed and position_store emptied.
      When the order then filled there was nothing to record it against,
      which is an untracked position -- the one outcome this function
      exists to prevent. The window was the adapter's 15-minute abandon
      timer; a route with `tif: GTC` and an evening ETF entry makes it a
      day.

    Emits one `reconciliation_summary` event per pass, saying what was
    checked and what did not match. Before it there was no record that a
    pass had run at all: the per-row events below only fire on drift, so a
    reconciliation that found everything in order and one that never
    happened looked identical from outside -- and "nothing is wrong" is
    exactly the claim that needs evidence after a restart.
    """
    open_rows = trade_registry.get_all_open_for_slave(slave_id)

    try:
        live_positions = []
        for account_type in account_types:
            live_positions.extend(await adapter.get_open_positions(account_type))
    except NotImplementedError:
        logger.info(
            "slave=%s adapter can't report open positions, skipping reconciliation "
            "(an MT4 slave here means the EA predates the POSITIONS command)",
            slave_id,
        )
        return
    except AdapterError as exc:
        logger.error("slave=%s reconciliation skipped, couldn't fetch live positions: %s", slave_id, exc)
        return

    live_symbols = {p.symbol for p in live_positions if p.quantity != 0}

    # The THIRD state. A registry row can match a position (open), match
    # nothing (closed), or match an order that is still working -- and the
    # last was being read as the middle one.
    #
    # Keyed by the reference WE stamped, so a row is matched to its own
    # order rather than by symbol: two strategies resting on the same
    # instrument would otherwise protect each other's rows.
    pending_refs: set = set()
    pending_symbols: set = set()
    read_orders = getattr(adapter, "get_working_orders", None)
    if read_orders is not None:
        try:
            for order in await read_orders():
                if order.order_ref:
                    pending_refs.add(str(order.order_ref))
                if order.symbol:
                    pending_symbols.add(order.symbol)
        except NotImplementedError:
            # Most adapters cannot answer. Reconciliation then behaves
            # exactly as it did, which is right for a broker whose entries
            # are market orders and never rest.
            pass
        except Exception as exc:
            # Could not ASK, which is not the same as "none working". Left
            # loud and NOT treated as empty: an empty set here would mark
            # every resting entry closed, the precise bug being fixed.
            #
            # `Exception`, not `AdapterError`, and that width is deliberate.
            # It was AdapterError, and on 2026-11-15 the IBKR adapter raised
            # AttributeError from this very call -- which sailed past here,
            # out of reconcile_slave, out of start_engine_for_slave, and
            # stopped BOTH IBKR slaves from starting at all, with open
            # positions at the broker and the week about to open.
            #
            # Not reading working orders degrades reconciliation: rows stay
            # open, which is the safe direction and is already handled
            # below. Not starting the engine means nothing manages those
            # positions at all. No bug in an optional adapter read is worth
            # the second outcome, so every failure takes the first --
            # logged with its traceback, because a swallowed exception here
            # is how this would become permanent and invisible.
            logger.exception(
                "slave=%s reconciliation: could not read working orders (%s) -- "
                "rows with an unfilled entry will be left OPEN rather than risk "
                "closing one that is still live",
                slave_id, exc,
            )
            pending_refs = None   # "unknown", handled below

    # Rows the registry had OPEN that the broker does not hold, and the
    # positions the broker holds that the registry does not know about.
    # Collected rather than only logged, because the summary below is what
    # reaches the Operations hub and a tab cannot read this hub's log.
    stuck: List[Dict[str, Any]] = []
    untracked: List[Dict[str, Any]] = []

    pending: List[Dict[str, Any]] = []

    for row in open_rows:
        if row.slave_symbol in live_symbols:
            continue

        # Still working at the broker, so this row is not drift. Left OPEN,
        # with its position_store entry intact, because the fill may still
        # land -- and a closed row is a fill nothing can be recorded
        # against.
        #
        # None means the question could not be asked. Every row is then
        # left alone: closing one whose entry is live is the failure this
        # exists to stop, and an extra OPEN row is corrected on the next
        # pass whereas an untracked position is not.
        if pending_refs is None or _row_is_pending(row, pending_refs, pending_symbols):
            why = ("working orders could not be read"
                   if pending_refs is None else "its entry is still working")
            logger.info(
                "slave=%s reconciliation: %s (master_ticket=%s) left OPEN -- %s",
                slave_id, row.slave_symbol, row.master_ticket, why,
            )
            pending.append({
                "slave_symbol": row.slave_symbol,
                "master_account": row.master_account,
                "master_ticket": row.master_ticket,
                "slave_ticket": row.slave_ticket,
                "reason": why,
            })
            continue

        trade_registry.record_close(
            slave_id, row.master_account, row.master_ticket, row.slave_symbol
        )
        # Drop the durable engine state too. Leaving it would restore the
        # position on the next start, and the trailing monitor would then
        # place a stop for something the broker doesn't hold -- on a flat
        # account that opens a position rather than protecting one.
        position_store.delete(
            slave_id, row.master_account, row.master_ticket, row.slave_symbol
        )
        logger.warning(
            "slave=%s reconciliation: %s (master_ticket=%s, slave_ticket=%s) was OPEN in the "
            "registry but the broker doesn't have it open -- marked CLOSED",
            slave_id, row.slave_symbol, row.master_ticket, row.slave_ticket,
        )
        stuck.append({
            "slave_symbol": row.slave_symbol,
            "master_account": row.master_account,
            "master_ticket": row.master_ticket,
            "slave_ticket": row.slave_ticket,
            # Said explicitly: this drift is corrected, not outstanding.
            # The registry row is the thing that was stuck, and it has been
            # moved to CLOSED -- nobody has to go and do it by hand.
            "resolved": "marked_closed",
        })
        await events.emit(
            {
                "type": "reconciliation",
                "slave_id": slave_id,
                "action": "closed_stale",
                "slave_symbol": row.slave_symbol,
                "master_account": row.master_account,
                "master_ticket": row.master_ticket,
                "slave_ticket": row.slave_ticket,
            }
        )

    registry_symbols = {row.slave_symbol for row in open_rows}
    held = {}
    for p in live_positions:
        if p.quantity:
            held[p.symbol] = held.get(p.symbol, 0.0) + float(p.quantity)
    for symbol in sorted(live_symbols - registry_symbols):
        logger.warning(
            "slave=%s reconciliation: broker has an open %s position not tracked in the "
            "registry -- left as-is",
            slave_id, symbol,
        )
        untracked.append({"slave_symbol": symbol, "quantity": held.get(symbol)})
        await events.emit(
            {"type": "reconciliation", "slave_id": slave_id, "action": "untracked_position", "symbol": symbol}
        )

    # One summary for the pass, drift or no drift. `matched` counts the
    # registry rows the broker agreed with, so checked - matched is exactly
    # the number of stuck ones and the two cannot drift apart.
    await events.emit(
        {
            "type": "reconciliation_summary",
            "slave_id": slave_id,
            "checked": len(open_rows),
            # A pending row is NOT matched -- the broker holds no position
            # for it. Counting it as agreement would report a clean pass
            # while an entry was still in the air.
            "matched": len(open_rows) - len(stuck) - len(pending),
            "untracked": untracked,
            "stuck": stuck,
            # Rows left OPEN because their entry is still resting. Reported
            # because a tab showing a slave_symbol with no position needs
            # to distinguish "waiting to fill" from "lost".
            "pending": pending,
            "at": time.time(),
        }
    )
    if stuck or untracked or pending:
        logger.warning(
            "slave=%s reconciliation: %d row(s) checked, %d matched, %d stuck, "
            "%d pending (entry still working), %d untracked at the broker",
            slave_id, len(open_rows), len(open_rows) - len(stuck) - len(pending),
            len(stuck), len(pending), len(untracked),
        )
    else:
        logger.info("slave=%s reconciliation: %d open row(s), all matched",
                    slave_id, len(open_rows))
