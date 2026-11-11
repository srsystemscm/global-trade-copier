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

    async def recover_session() -> None:
        """Re-run this slave's startup path after its broker session died.

        Deliberately the same three steps in the same order as above --
        connect, reconcile, restore -- rather than a bare reconnect. Order
        matters: reconcile marks registry rows CLOSED (and drops their
        position_store entries) for anything the broker no longer holds, so
        restore then rebuilds only what is genuinely still open. A position
        stopped out while the session was down would otherwise come back as
        live state, and the trailing monitor would place a stop for
        something the broker doesn't hold -- which on a flat account opens a
        position rather than protecting one.
        """
        await adapter.disconnect()
        await adapter.connect()
        await reconcile_slave(slave.id, adapter, account_types, events)
        if hasattr(engine, "restore"):
            await engine.restore()

    watchdog = SlaveWatchdog(
        slave.id,
        adapter,
        events,
        notifier,
        account_types=account_types,
        poll_interval=settings.watchdog_poll_interval_seconds,
        drawdown_alert_pct=settings.drawdown_alert_pct,
        is_paused=lambda: engine.paused,
        # Only adapters holding a stateful session need rebuilding; MT4 and
        # Schwab recover through their own next call, so leave them exactly
        # as they were.
        recover=recover_session if adapter.requires_session_recovery else None,
        on_session_state=engine.set_session_up,
        recovery_max_attempts=settings.session_recovery_max_attempts,
        recovery_base_delay=settings.session_recovery_base_delay,
        recovery_max_delay=settings.session_recovery_max_delay,
    )
    if hasattr(engine, "restore"):
        await engine.restore()

    watchdog.start()
    engine.watchdog = watchdog  # type: ignore[attr-defined]

    engine.start()
    logger.info("%s engine started for slave=%s (%s)", mode, slave.id, slave.broker_type)
    return engine


async def start_engines(
    slaves: List[SlaveConfig], bus: SignalBus, events: EventBus, notifier: Notifier
) -> List[Engine]:
    """Start every enabled slave, surviving the ones that cannot start.

    One unreachable broker must not stop the hub. This ran as a bare list
    comprehension, so a single adapter raising at connect -- IB Gateway not
    up yet, an MT4 terminal not launched -- aborted startup entirely and
    took every other slave's copying down with it, including ones whose
    brokers were perfectly fine.

    That is not a rare case on a VPS: the hub and IB Gateway both start at
    boot and the hub routinely wins the race, and Gateway's forced daily
    restart hands it the same situation every day.

    A slave that fails here is loud rather than silent -- it keeps showing in
    GET /status, because that reads the slave table rather than this list, so
    it appears as present and disconnected rather than quietly vanishing.
    """
    engines: List[Engine] = []
    for slave in slaves:
        try:
            engines.append(await start_engine_for_slave(slave, bus, events, notifier))
        except Exception:
            logger.exception("slave=%s failed to start; the hub is running WITHOUT it", slave.id)
            await events.emit({"type": "slave_start_failed", "slave_id": slave.id})
            await notifier.notify(
                "slave_start_failed",
                f"Slave {slave.id} did not start and is NOT copying -- check the hub log",
                slave_id=slave.id,
            )
    return engines


async def stop_engine(engine: Engine, bus: SignalBus) -> None:
    watchdog: Optional[SlaveWatchdog] = getattr(engine, "watchdog", None)
    if watchdog is not None:
        await watchdog.stop()
    await engine.stop()
    await engine.adapter.disconnect()
    bus.unregister_slave(engine.slave_id)
    logger.info("engine stopped for slave=%s", engine.slave_id)
