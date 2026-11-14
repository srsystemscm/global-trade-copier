import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import magic_filter, master_filter, trade_registry
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


def _validated_master_filter(config: Optional[Dict[str, Any]]):
    """Parse a config's master selection, or answer 400 saying what is wrong.

    Same reasoning as the magic selection: a list that cannot be read must
    not reach the database, or the slave fails to start on some later
    restart rather than on the request that broke it.
    """
    if config is None:
        return None
    try:
        return master_filter.parse(config)
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


def _describe_sizing(sizing: Optional[Dict[str, Any]]) -> str:
    """One readable phrase for one sizing block.

    An absent block is not "none": a mirror slave without one converts the
    master's lots by point value, which is a real sizing decision and the
    one a reader most needs told, because nothing on screen would
    otherwise distinguish it from a slave sizing itself.
    """
    sizing = sizing or {}
    mode = sizing.get("mode")
    if not mode:
        return "mirrors the master's lots"
    if mode == "pct_risk":
        pct = sizing.get("risk_pct")
        base = sizing.get("capital_base", "balance")
        ceiling = sizing.get("max_risk_pct")
        text = f"{pct:.2%} of {base}" if isinstance(pct, (int, float)) else f"pct_risk of {base}"
        if isinstance(ceiling, (int, float)):
            text += f", capped {ceiling:.2%}"
        return text
    if mode == "dollar_notional":
        return f"${sizing.get('notional')} notional"
    if mode == "fixed_contracts":
        return f"{sizing.get('contracts')} contract(s) fixed"
    if mode == "lot_multiplier":
        return f"master lots x{sizing.get('multiplier')}"
    return str(mode)


def _tally(blocks, label: str) -> str:
    """Several sizing blocks described as one line."""
    blocks = list(blocks)
    counts: Dict[str, int] = {}
    for block in blocks:
        described = _describe_sizing(block)
        counts[described] = counts.get(described, 0) + 1
    if len(counts) == 1:
        return f"{len(blocks)} {label}, all {next(iter(counts))}"
    parts = ", ".join(f"{n}x {d}" for d, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return f"{len(blocks)} {label}: {parts}"


def _per_target_sizing(config: Dict[str, Any]) -> Dict[str, Any]:
    """The {"MGC": {...}, "GLD": {...}} form, if that is what this is.

    resolve_sizing_config accepts sizing either as one shared block or as
    a block per target symbol, told apart by whether the values are
    dicts. Read as a single block, the per-target form has no `mode` at
    all -- so it described itself as "mirrors the master's lots", which on
    an autonomous slave is both wrong and reassuring, the worst pair.
    """
    sizing = config.get("sizing") or {}
    return {k: v for k, v in sizing.items() if isinstance(v, dict)}


def _sizing_summary(config: Dict[str, Any]) -> str:
    """How this slave sizes, in one line, in all three shapes it takes.

    `ibkr-etf-1` carries sizing on each of its twenty routes and none at
    the slave level, so reporting the slave-level block alone showed
    `null` -- which reads as unconfigured when it is configured twenty
    times over. That is worse than saying nothing.
    """
    per_target = _per_target_sizing(config)
    if per_target:
        return _tally(per_target.values(), "targets")

    if config.get("sizing"):
        return _describe_sizing(config["sizing"])

    routes = config.get("routes") or []
    if not routes:
        return _describe_sizing(None)
    return _tally((r.get("sizing") for r in routes), "routes")


def _describe_route_sizing(block: Optional[Dict[str, Any]]) -> str:
    """A route's sizing, which may be whole, absent, or a partial override.

    merge_sizing lets a route carry only the keys it changes, so
    `{"max_risk_pct": 0.035}` means "the slave's sizing, with a higher
    ceiling". Read as a block in its own right it has no `mode` and so
    described itself as the no-sizing case -- the master's lots -- on a
    slave that risks a percentage of its own balance. One such route
    exists today, on ETHUSD, and nothing on screen would have shown that
    its ceiling is more than double the others'.
    """
    if not block:
        return "slave default"
    if block.get("mode"):
        return _describe_sizing(block)

    bits = []
    if isinstance(block.get("risk_pct"), (int, float)):
        bits.append(f"{block['risk_pct']:.2%}")
    if block.get("capital_base"):
        bits.append(f"of {block['capital_base']}")
    if isinstance(block.get("max_risk_pct"), (int, float)):
        bits.append(f"capped {block['max_risk_pct']:.2%}")
    for key in ("contracts", "notional", "multiplier", "rounding"):
        if block.get(key) is not None:
            bits.append(f"{key} {block[key]}")
    return "slave default, " + ", ".join(bits) if bits else "slave default"


def _route_rows(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Each route, flattened to what a reader needs to judge it.

    The slave-level summary says "20 routes, all 1.00% of balance", which
    answers how much and not what. On futures that is the half that
    matters: which contract month, whether the broker holds the stop, and
    whether the order may work outside regular hours are all per route,
    and all of them decide what a copy actually becomes.
    """
    rows: List[Dict[str, Any]] = []
    for route in config.get("routes") or []:
        target = route.get("target")
        rows.append({
            "symbol": route.get("symbol"),
            "magic": route.get("magic"),
            # A route may name several targets and take the first that
            # resolves, so this is a list as often as a string.
            "target": target if isinstance(target, list) else [target],
            "direction": route.get("direction"),
            "target_direction": route.get("target_direction"),
            "order_type": route.get("order_type") or "market",
            "outside_rth": bool(route.get("outside_rth")),
            # False means the copier holds the stop itself and the broker
            # has no resting order -- worth seeing, because it decides
            # whether the position is protected while the hub is down.
            "broker_brackets": route.get("broker_brackets"),
            "month_slot": route.get("month_slot"),
            "sizing": _describe_route_sizing(route.get("sizing")),
            "atr_period": route.get("atr_period"),
            "risk_management": route.get("risk_management") or None,
        })
    return rows


def _slave_summary_extras(slave) -> Dict[str, Any]:
    """Settings that decide whether, and into what, a signal is copied."""
    try:
        selection = magic_filter.parse(slave.config)
        magic_summary = selection.describe()
        magic_dict = selection.as_dict()
    except ValueError as exc:
        # A filter that will not parse is refused at PATCH time, so this is
        # a config written before that check or edited by hand. Saying so
        # beats showing "every magic" over a selection that is not running.
        magic_summary = f"unreadable: {exc}"
        magic_dict = None

    try:
        masters = master_filter.parse(slave.config)
        master_summary = masters.describe()
        master_dict = masters.as_dict()
    except ValueError as exc:
        master_summary = f"unreadable: {exc}"
        master_dict = None

    roll = slave.config.get("contract_roll") or None
    return {
        "master_filter": master_dict,
        "master_filter_summary": master_summary,
        "magic_filter": magic_dict,
        "magic_filter_summary": magic_summary,
        "contract_roll": roll,
        "routes": _route_rows(slave.config),
        # How much of the configured risk a leg carries. 1.0 everywhere by
        # default, which means "the EA's own target risk, unchanged".
        "risk_scale": slave.config.get("risk_scale"),
        # Per EA. The futures and ETF legs have far less leverage than the
        # CFD account the strategies were sized on, so this is set per
        # magic rather than per slave.
        "risk_scale_by_magic": slave.config.get("risk_scale_by_magic") or None,
    }


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
        # The account this slave actually trades, as the broker reports it.
        # None until the terminal has said; the UI and the hub both read
        # that as "not known yet" rather than as an answer.
        "account": getattr(getattr(engine, "adapter", None), "account", None) or None,
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
        # A line a reader can act on without opening the JSON, and the only
        # place the per-route case is represented at all.
        "sizing_summary": _sizing_summary(slave.config),
        # Whether editing slave-level sizing would do anything. Routes carry
        # their own and win over the shared block, so offering a slave-level
        # control on a route-based slave is a setting that saves cleanly and
        # changes nothing -- the worst kind.
        "sizing_per_route": bool(
            _per_target_sizing(slave.config)
            or (not slave.config.get("sizing") and slave.config.get("routes"))),
        **_slave_summary_extras(slave),
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
    _validated_master_filter(payload.config)

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


@router.get("/slaves/{slave_id}")
async def get_one_slave(slave_id: str):
    """One slave's STORED CONFIG, whole.

    PATCH replaces config_json rather than merging into it, so anything
    editing a slave has to send the entire object back. Without a way to
    read it first, an editor would have to reconstruct it from the
    summary -- which omits routes, mirror_map, host and ports -- and
    saving would quietly delete every one of them.
    """
    slave = get_slave(slave_id)
    if slave is None:
        raise HTTPException(404, f"no slave with id={slave_id!r}")
    return {
        "id": slave.id,
        "name": slave.name,
        "broker_type": slave.broker_type,
        "enabled": slave.enabled,
        "paused": slave.paused,
        "config": slave.config,
    }


@router.patch("/slaves/{slave_id}")
async def patch_slave(slave_id: str, payload: SlaveUpdate, request: Request):
    slave = get_slave(slave_id)
    if slave is None:
        raise HTTPException(404, f"no slave with id={slave_id!r}")

    takes = _validated_magic_filter(payload.config)
    from_masters = _validated_master_filter(payload.config)

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
            request.app.state.bus.set_master_filter(slave_id, from_masters)

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
