import logging
import time
from typing import Any, Dict, List, Optional

from app import position_store, trade_registry
from app.adapters.base import AdapterError, BrokerAdapter
from app.events import EventBus

logger = logging.getLogger(__name__)


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

    # Rows the registry had OPEN that the broker does not hold, and the
    # positions the broker holds that the registry does not know about.
    # Collected rather than only logged, because the summary below is what
    # reaches the Operations hub and a tab cannot read this hub's log.
    stuck: List[Dict[str, Any]] = []
    untracked: List[Dict[str, Any]] = []

    for row in open_rows:
        if row.slave_symbol in live_symbols:
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
            "matched": len(open_rows) - len(stuck),
            "untracked": untracked,
            "stuck": stuck,
            "at": time.time(),
        }
    )
    if stuck or untracked:
        logger.warning(
            "slave=%s reconciliation: %d row(s) checked, %d matched, %d stuck, "
            "%d untracked at the broker",
            slave_id, len(open_rows), len(open_rows) - len(stuck),
            len(stuck), len(untracked),
        )
    else:
        logger.info("slave=%s reconciliation: %d open row(s), all matched",
                    slave_id, len(open_rows))
