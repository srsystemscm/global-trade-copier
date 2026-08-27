import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import trade_registry
from app.adapters.base import AdapterError
from app.config import settings
from app.db import db_cursor, get_config_value, list_logs, set_config_value
from app.engine_manager import start_engine_for_slave, stop_engine
from app.slaves import delete_slave, get_slave, insert_slave, list_all_slaves, set_paused, update_slave

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


class RiskUpdate(BaseModel):
    kill_switch_enabled: Optional[bool] = None
    trading_hours_enabled: Optional[bool] = None
    trading_hours_start: Optional[str] = None  # "HH:MM", UTC
    trading_hours_end: Optional[str] = None


def _find_engine(request: Request, slave_id: str):
    for engine in request.app.state.engines:
        if engine.slave_id == slave_id:
            return engine
    return None


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
        "sizing": slave.config.get("sizing") if mode == "autonomous" else None,
        "risk_management": slave.config.get("risk_management") if mode == "autonomous" else None,
    }

    if engine is not None:
        account_types = ["futures", "brokerage"] if slave.broker_type == "schwab" else [None]
        positions: List[Dict[str, Any]] = []
        try:
            for account_type in account_types:
                for pos in await engine.adapter.get_open_positions(account_type):
                    positions.append(
                        {
                            "symbol": pos.symbol,
                            "quantity": pos.quantity,
                            "average_price": pos.average_price,
                            "market_value": pos.market_value,
                            "unrealized_pnl": pos.unrealized_pnl,
                        }
                    )
            summary["positions"] = positions
        except NotImplementedError:
            summary["positions_error"] = "not supported for this broker"
        except AdapterError as exc:
            summary["positions_error"] = str(exc)

    return summary


@router.get("/slaves")
async def get_slaves(request: Request):
    return {"slaves": [await _slave_summary(request, slave) for slave in list_all_slaves()]}


@router.post("/slaves")
async def create_slave(payload: SlaveCreate, request: Request):
    if payload.broker_type not in KNOWN_BROKER_TYPES:
        raise HTTPException(400, f"unknown broker_type {payload.broker_type!r}")
    if get_slave(payload.id) is not None:
        raise HTTPException(409, f"slave id {payload.id!r} already exists")

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

    if payload.paused is not None:
        set_paused(slave_id, payload.paused)
        engine = _find_engine(request, slave_id)
        if engine is not None:
            engine.paused = payload.paused
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
async def get_trades(limit: int = 200):
    return {"trades": trade_registry.list_trades_with_copies(limit)}


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
    slaves = [await _slave_summary(request, slave) for slave in list_all_slaves()]
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
