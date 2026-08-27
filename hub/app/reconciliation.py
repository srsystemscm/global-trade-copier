import logging
from typing import List, Optional

from app import trade_registry
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
    """
    open_rows = trade_registry.get_all_open_for_slave(slave_id)

    try:
        live_positions = []
        for account_type in account_types:
            live_positions.extend(await adapter.get_open_positions(account_type))
    except NotImplementedError:
        logger.info("slave=%s adapter doesn't support position reconciliation, skipping", slave_id)
        return
    except AdapterError as exc:
        logger.error("slave=%s reconciliation skipped, couldn't fetch live positions: %s", slave_id, exc)
        return

    live_symbols = {p.symbol for p in live_positions if p.quantity != 0}

    for row in open_rows:
        if row.slave_symbol in live_symbols:
            continue
        trade_registry.record_close(slave_id, row.master_ticket, row.slave_symbol)
        logger.warning(
            "slave=%s reconciliation: %s (master_ticket=%s, slave_ticket=%s) was OPEN in the "
            "registry but the broker doesn't have it open -- marked CLOSED",
            slave_id, row.slave_symbol, row.master_ticket, row.slave_ticket,
        )
        await events.emit(
            {
                "type": "reconciliation",
                "slave_id": slave_id,
                "action": "closed_stale",
                "slave_symbol": row.slave_symbol,
                "master_ticket": row.master_ticket,
                "slave_ticket": row.slave_ticket,
            }
        )

    registry_symbols = {row.slave_symbol for row in open_rows}
    for symbol in live_symbols - registry_symbols:
        logger.warning(
            "slave=%s reconciliation: broker has an open %s position not tracked in the "
            "registry -- left as-is",
            slave_id, symbol,
        )
        await events.emit(
            {"type": "reconciliation", "slave_id": slave_id, "action": "untracked_position", "symbol": symbol}
        )
