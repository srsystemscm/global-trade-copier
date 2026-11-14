import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import magic_filter, trade_registry
from app.adapters.base import AdapterError
from app.config import settings
from app.db import db_cursor, get_config_value, list_logs, set_config_value
from app.engine_manager import start_engine_for_slave, stop_engine
from app.slaves import (
    delete_slave,
    get_slave,
    insert_slave,
    list_all_slaves,
    set_enabled,
    set_paused,
    update_slave,
)

logger = logging.getLogger(__name__)

router = APIRouter()

KNOWN_BROKER_TYPES = {"mt4", "schwab", "ibkr"}


class SlaveCreate(BaseModel):
    id: str
    name: str
    broker_type: str
    config: Dict[str, Any] = {}
    enabled: bool = True


class SlaveUpdate(BaseModel):
    name: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    paused: Optional[bool] = None
    # Was missing, so PATCH {"enabled": false} was silently dropped by
    # pydantic and the route answered 200 with the slave still enabled --
    # a no-op that reported success.
    enabled: Optional[bool] = None


class RiskUpdate(BaseModel):
    kill_switch_enabled: Optional[bool] = None
    trading_hours_enabled: Optional[bool] = None
    trading_hours_start: Optional[str] = None  # "HH:MM", UTC
    trading_hours_end: Optional[str] = None


def _validated_magic_filter(config: Optional[Dict[str, Any]]):
    """Parse a config's magic selection, or answer 400 saying what is wrong.

    Called before anything is written. A selection that cannot be read must
    not reach the database: persisted, it would stop the slave from
    starting on the next restart rather than on the request that broke it,
    and the connection between the two would be a day old by then.
    """
    if config is None:
        return None
    try:
        return magic_filter.parse(config)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def _find_engine(request: Request, slave_id: str):
    for engine in request.app.state.engines:
        if engine.slave_id == slave_id:
            return engine
    return None


# How long one slave's position fetch may hold up a page that lists all of
# them. The adapters have their own timeouts -- the MT4 one is 45 seconds --
# which are right for a copy that must not be abandoned halfway, and far too
# long for a dashboard poll. This is the dashboard's own patience, not the
# broker's.
POSITIONS_TIMEOUT_S = 5.0


async def _fetch_positions(engine, account_types) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for account_type in account_types:
        for pos in await engine.adapter.get_open_positions(account_type):
            out.append({
                "symbol": pos.symbol,
                "quantity": pos.quantity,
                "average_price": pos.average_price,
                "market_value": pos.market_value,
                "unrealized_pnl": pos.unrealized_pnl,
            })
    return out


async def _positions_within(engine, account_types):
    """This slave's positions, or None if it did not answer in time.

    NEVER CANCELS THE FETCH. A ZMQ REQ socket is strictly lock-step --
    send, receive, send, receive -- and cancelling between the two leaves
    it expecting a reply that nobody will read. Every later call on that
    socket then dies with "Operation cannot be accomplished in current
    state", so a page timeout would take the slave down rather than just
    skip it. That was observed: wrapping the fetch in wait_for turned a
    slow slave into a permanently broken one.

    So the request runs to the adapter's own timeout, which is sized for a
    copy that must not be abandoned halfway. This only bounds how long the
    PAGE waits for it.

    One fetch in flight per slave, shared by every caller. Without that, a
    dashboard polling every few seconds queues request after request behind
    the adapter's lock, and they arrive long after anyone wanted them.
    """
    task = getattr(engine, "_positions_task", None)
    if task is None or task.done():
        task = asyncio.ensure_future(_fetch_positions(engine, account_types))
        # Someone must retrieve the exception even when no page is waiting
        # by the time it fails, or asyncio logs it as never retrieved.
        task.add_done_callback(lambda t: t.cancelled() or t.exception())
        engine._positions_task = task

    done, _ = await asyncio.wait({task}, timeout=POSITIONS_TIMEOUT_S)
    if not done:
        return None
    return task.result()


async def _slave_summary(request: Request, slave) -> Dict[str, Any]:
    engine = _find_engine(request, slave.id)
    mode = slave.config.get("mode", "mirror")
    watchdog = getattr(engine, "watchdog", None) if engine is not None else None
    summary: Dict[str, Any] = {
        "id": slave.id,
        "name": slave.name,
        "broker_type": slave.broker_type,
        "mode": mode,
        "enabled": slave.enabled,
        "paused": engine.paused if engine is not None else slave.paused,
        "running": engine is not None,
        "connected": watchdog.connected if watchdog is not None else None,
        "positions": None,
        "positions_error": None,
        "atr_period": slave.config.get("atr_period") if mode == "autonomous" else None,
        # Reported for BOTH modes. It used to be autonomous-only, from when a
        # mirror slave had no sizing of its own and simply carried the
        # master's lots across. A mirror slave with a `sizing` block now
        # sizes from its own capital through the same compute_size the
        # autonomous engines use -- so hiding it here meant a setting that
        # decides what gets traded was live in the engine and absent from
        # every screen that shows the slave.
        "sizing": slave.config.get("sizing"),
        "risk_management": slave.config.get("risk_management") if mode == "autonomous" else None,
        # Both decide whether a signal becomes a trade at all, and both were
        # invisible: netting.allow_opposing is why a hedged leg copies or is
        # refused, and margin.max_pct_of_free is why a size gets reduced.
        "netting": slave.config.get("netting") or None,
        "margin": slave.config.get("margin") or None,
    }

    if engine is not None:
        account_types = ["futures", "brokerage"] if slave.broker_type == "schwab" else [None]
        try:
            fetched = await _positions_within(engine, account_types)
            if fetched is None:
                # A terminal that is shut, or a bridge that stopped
                # answering. Reported as this slave's problem rather than
                # allowed to become the whole page's: a closed Darwinex
                # terminal took /status past two minutes and the dashboard
                # never rendered at all.
                summary["positions_error"] = (
                    f"no answer within {POSITIONS_TIMEOUT_S:g}s -- is the terminal open?")
            else:
                summary["positions"] = fetched
        except NotImplementedError:
            summary["positions_error"] = "not supported for this broker"
        except AdapterError as exc:
            summary["positions_error"] = str(exc)

    return summary


async def _all_slave_summaries(request: Request) -> List[Dict[str, Any]]:
    """Every slave's summary, gathered at once rather than one after another.

    Sequentially, a page listing six slaves waited for six broker round
    trips end to end, so the slowest one set the page's speed and several
    slow ones added up. They have nothing to do with each other.
    """
    slaves = list_all_slaves()
    return list(await asyncio.gather(
        *(_slave_summary(request, slave) for slave in slaves)))


@router.get("/slaves")
async def get_slaves(request: Request):
    return {"slaves": await _all_slave_summaries(request)}


@router.post("/slaves")
async def create_slave(payload: SlaveCreate, request: Request):
    if payload.broker_type not in KNOWN_BROKER_TYPES:
        raise HTTPException(400, f"unknown broker_type {payload.broker_type!r}")
    if get_slave(payload.id) is not None:
        raise HTTPException(409, f"slave id {payload.id!r} already exists")
    _validated_magic_filter(payload.config)

    slave = insert_slave(payload.id, payload.name, payload.broker_type, payload.config, payload.enabled)

    if slave.enabled:
        try:
            engine = await start_engine_for_slave(
                slave, request.app.state.bus, request.app.state.events, request.app.state.notifier
            )
        except Exception as exc:
            delete_slave(slave.id)
            raise HTTPException(400, f"could not start slave: {exc}") from exc
        request.app.state.engines.append(engine)

    return await _slave_summary(request, slave)


@router.patch("/slaves/{slave_id}")
async def patch_slave(slave_id: str, payload: SlaveUpdate, request: Request):
    slave = get_slave(slave_id)
    if slave is None:
        raise HTTPException(404, f"no slave with id={slave_id!r}")

    takes = _validated_magic_filter(payload.config)

    if payload.name is not None or payload.config is not None:
        slave = update_slave(slave_id, name=payload.name, config=payload.config)
        engine = _find_engine(request, slave_id)
        if engine is not None and payload.config is not None and hasattr(engine, "slave_config"):
            # live-updates symbol_map/sizing/risk_management for autonomous
            # engines; broker connection details (host/account IDs) still
            # need a hub restart to take effect
            engine.slave_config = slave.config
            engine.atr_period = slave.config.get("atr_period", 14)
            engine.risk_config = slave.config.get("risk_management", {})
            if hasattr(engine, "roll_config"):
                engine.roll_config = slave.config.get("contract_roll", {})
        if payload.config is not None:
            # Held by the bus, not the engine: the selection decides what
            # reaches this slave's queue in the first place. Updated even
            # when no engine is running, which is a no-op the bus ignores.
            request.app.state.bus.set_magic_filter(slave_id, takes)

    if payload.paused is not None:
        set_paused(slave_id, payload.paused)
        engine = _find_engine(request, slave_id)
        if engine is not None:
            engine.paused = payload.paused
        slave = get_slave(slave_id)

    if payload.enabled is not None and payload.enabled != slave.enabled:
        set_enabled(slave_id, payload.enabled)
        engine = _find_engine(request, slave_id)
        if payload.enabled and engine is None:
            slave = get_slave(slave_id)
            engine = await start_engine_for_slave(
                slave, request.app.state.bus, request.app.state.events, request.app.state.notifier
            )
            request.app.state.engines.append(engine)
        elif not payload.enabled and engine is not None:
            await stop_engine(engine, request.app.state.bus)
            request.app.state.engines.remove(engine)
        slave = get_slave(slave_id)

    return await _slave_summary(request, slave)


@router.delete("/slaves/{slave_id}")
async def remove_slave(slave_id: str, request: Request):
    slave = get_slave(slave_id)
    if slave is None:
        raise HTTPException(404, f"no slave with id={slave_id!r}")

    engine = _find_engine(request, slave_id)
    if engine is not None:
        await stop_engine(engine, request.app.state.bus)
        request.app.state.engines.remove(engine)

    delete_slave(slave_id)
    return {"status": "ok"}


@router.get("/trades")
async def get_trades(limit: int = 200, since: float | None = None):
    return {"trades": trade_registry.list_trades_with_copies(limit, since)}


@router.get("/config")
async def get_config():
    with db_cursor() as cur:
        rows = cur.execute("SELECT key, value FROM config").fetchall()
    return {"config": {row["key"]: row["value"] for row in rows}}


@router.patch("/config")
async def patch_config(payload: Dict[str, str]):
    for key, value in payload.items():
        set_config_value(key, value)
    return {"status": "ok"}


@router.get("/risk")
async def get_risk():
    return {
        "kill_switch_enabled": get_config_value("kill_switch_enabled") == "true",
        "trading_hours_enabled": get_config_value("trading_hours_enabled") == "true",
        "trading_hours_start": get_config_value("trading_hours_start") or "00:00",
        "trading_hours_end": get_config_value("trading_hours_end") or "23:59",
    }


@router.patch("/risk")
async def patch_risk(payload: RiskUpdate):
    if payload.kill_switch_enabled is not None:
        set_config_value("kill_switch_enabled", "true" if payload.kill_switch_enabled else "false")
        logger.warning("kill switch %s via API", "ENABLED" if payload.kill_switch_enabled else "disabled")
    if payload.trading_hours_enabled is not None:
        set_config_value("trading_hours_enabled", "true" if payload.trading_hours_enabled else "false")
    if payload.trading_hours_start is not None:
        set_config_value("trading_hours_start", payload.trading_hours_start)
    if payload.trading_hours_end is not None:
        set_config_value("trading_hours_end", payload.trading_hours_end)
    return await get_risk()


@router.get("/status")
async def get_status(request: Request):
    receiver = request.app.state.receiver
    slaves = await _all_slave_summaries(request)
    return {
        "uptime_seconds": round(time.time() - request.app.state.start_time, 1),
        "master": {
            "sub_port": settings.zmq_sub_port,
            "pull_port": settings.zmq_pull_port,
            "last_heartbeat_by_account": receiver.last_heartbeat,
        },
        "slaves": slaves,
    }


@router.get("/logs")
async def get_logs(lines: int = 200, level: Optional[str] = None):
    rows = list_logs(limit=lines, level=level.upper() if level else None)
    formatted = [
        f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(r['ts']))} {r['level']:<8} {r['logger']}: {r['message']}"
        for r in reversed(rows)  # list_logs returns newest-first; display wants oldest-first like a tailed file
    ]
    return {"lines": formatted}
