import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.api_routes import router as api_router
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from app.config import HUB_ROOT, settings
from app.db import init_db
from app.engine_manager import start_engines, stop_engine
from app.events import EventBus
from app.flex_reconcile import FlexReconciler
from app.hub_reporter import HubReporter
from app.logging_config import setup_logging
from app.notifier import Notifier
from app.schwab_routes import router as schwab_router
from app.signal_bus import SignalBus
from app.slaves import load_enabled_slaves, seed_slaves_if_empty
from app.watchdog import ConsumeWatchdog, MasterWatchdog
from app.ws_routes import router as ws_router
from app.zmq_receiver import ZmqReceiver

setup_logging()
logger = logging.getLogger(__name__)

START_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting trade copier hub")
    init_db()
    seed_slaves_if_empty()

    events = EventBus()
    notifier = Notifier()
    bus = SignalBus(events=events)
    receiver = ZmqReceiver(bus)
    await receiver.start()

    master_watchdog = MasterWatchdog(
        receiver,
        events,
        notifier,
        stale_after=settings.watchdog_stale_heartbeat_seconds,
        poll_interval=settings.watchdog_poll_interval_seconds,
    )
    master_watchdog.start()

    engines = await start_engines(load_enabled_slaves(), bus, events, notifier)

    # The broker's own end-of-day record, checked against this hub's
    # registry on a daily schedule. Inert unless a Flex token and query are
    # configured, which is how the hub has always run.
    flex = FlexReconciler(events=events, notifier=notifier)
    flex.start()

    app.state.bus = bus
    app.state.receiver = receiver
    app.state.engines = engines
    app.state.events = events
    app.state.notifier = notifier
    app.state.master_watchdog = master_watchdog
    app.state.flex = flex
    app.state.start_time = START_TIME

    # Watches that each engine is still DRAINING its queue, which no
    # other check covered: SlaveWatchdog watches the adapter and
    # MasterWatchdog watches the feed, and on 2026-11-16 four consume
    # loops died while both of those stayed green. Reads app.state for
    # the same reason the reporter does -- POST /slaves adds engines
    # after this point.
    consume_watchdog = ConsumeWatchdog(app.state, events, notifier)
    consume_watchdog.start()
    app.state.consume_watchdog = consume_watchdog

    # Last, and reading app.state rather than holding its own copy: the
    # engines list grows while the copier runs (POST /slaves), and a
    # snapshot built from a list captured here would stop mentioning new
    # slaves. Off unless a key is configured, in which case this does
    # nothing whatever -- see app/hub_reporter.
    reporter = HubReporter(events, app.state)
    await reporter.start()
    app.state.hub_reporter = reporter

    yield

    logger.info("shutting down trade copier hub")
    # Stopped first, so a report in flight cannot be holding the event bus
    # open while the engines are being torn down beneath it.
    await reporter.stop()
    await consume_watchdog.stop()
    await flex.stop()
    await master_watchdog.stop()
    for engine in engines:
        await stop_engine(engine, bus)
    await receiver.stop()


app = FastAPI(title="Global Trade Copier Hub", lifespan=lifespan)

# the Vite dev server runs on a different origin during development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(schwab_router)
app.include_router(api_router)
app.include_router(ws_router)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - START_TIME, 1),
    }


# --------------------------------------------------------------- the UI
#
# Served by the hub itself, from the build in ui/dist. It used to exist
# only behind Vite's dev server, which meant the dashboard was reachable
# only on a machine where somebody had run `npm run dev` -- so there was
# no interface at all on a VPS, and none here the moment that process
# stopped.
#
# Mounted LAST, because the catch-all below matches everything: FastAPI
# tries routes in the order they were added, so every API route above
# still wins.

UI_DIST = HUB_ROOT.parent / "ui" / "dist"

# Paths that belong to the API. A typo'd API call should answer 404, not a
# page: HTML arriving where JSON was expected is a confusing way to learn
# you misspelled a route.
_API_PREFIXES = (
    "slaves", "trades", "status", "config", "risk", "logs", "health",
    "schwab", "ws", "docs", "openapi.json", "redoc",
)

if (UI_DIST / "index.html").is_file():
    assets = UI_DIST / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_ui(full_path: str):
        """The dashboard, and client-side routes within it.

        React Router owns /dashboard, /copier and /settings, which exist
        only in the browser -- the server has no file for them, so each
        has to answer with index.html and let the app route it.
        """
        head = full_path.split("/", 1)[0]
        if head in _API_PREFIXES:
            raise HTTPException(status_code=404, detail=f"no such route: /{full_path}")

        candidate = (UI_DIST / full_path).resolve()
        # Stay inside the build directory: a path like ../../.env would
        # otherwise read whatever the process can see.
        if full_path and UI_DIST.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(UI_DIST / "index.html")

else:
    logger.warning(
        "no UI build at %s -- the dashboard will not be served. Run "
        "`npm run build` in ui/ to create it.", UI_DIST,
    )
