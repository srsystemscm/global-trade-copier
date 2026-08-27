import logging
from typing import List, Optional, Union

from app.adapters import create_adapter
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.config import settings
from app.copy_engine import CopyEngine
from app.events import EventBus
from app.notifier import Notifier
from app.reconciliation import reconcile_slave
from app.signal_bus import SignalBus
from app.slaves import SlaveConfig
from app.watchdog import SlaveWatchdog

logger = logging.getLogger(__name__)

Engine = Union[CopyEngine, AutonomousCopyEngine]


def account_types_for(slave: SlaveConfig) -> List[Optional[str]]:
    return ["futures", "brokerage"] if slave.broker_type == "schwab" else [None]


async def start_engine_for_slave(slave: SlaveConfig, bus: SignalBus, events: EventBus, notifier: Notifier) -> Engine:
    """Builds the adapter + Copy Engine for a slave, reconciles the Trade
    Registry against the broker's live positions, starts a connectivity/
    drawdown watchdog, and starts the engine running.

    Shared by hub startup (one call per enabled slave) and the POST /slaves
    route, so a new slave added through the UI starts copying immediately
    without a hub restart.
    """
    adapter = create_adapter(slave)
    await adapter.connect()

    account_types = account_types_for(slave)
    await reconcile_slave(slave.id, adapter, account_types, events)

    queue = bus.register_slave(slave.id)

    mode = slave.config.get("mode", "mirror")
    engine: Engine
    if mode == "autonomous":
        engine = AutonomousCopyEngine(
            slave.id, adapter, queue, slave.config, paused=slave.paused, events=events, notifier=notifier
        )
    else:
        engine = CopyEngine(slave.id, adapter, queue, paused=slave.paused, events=events, notifier=notifier)

    watchdog = SlaveWatchdog(
        slave.id,
        adapter,
        events,
        notifier,
        account_types=account_types,
        poll_interval=settings.watchdog_poll_interval_seconds,
        drawdown_alert_pct=settings.drawdown_alert_pct,
    )
    watchdog.start()
    engine.watchdog = watchdog  # type: ignore[attr-defined]

    engine.start()
    logger.info("%s engine started for slave=%s (%s)", mode, slave.id, slave.broker_type)
    return engine


async def stop_engine(engine: Engine, bus: SignalBus) -> None:
    watchdog: Optional[SlaveWatchdog] = getattr(engine, "watchdog", None)
    if watchdog is not None:
        await watchdog.stop()
    await engine.stop()
    await engine.adapter.disconnect()
    bus.unregister_slave(engine.slave_id)
    logger.info("engine stopped for slave=%s", engine.slave_id)
