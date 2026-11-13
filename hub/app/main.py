import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api_routes import router as api_router
from app.config import settings
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
from app.watchdog import MasterWatchdog
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
