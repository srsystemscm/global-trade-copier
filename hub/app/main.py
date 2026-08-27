import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api_routes import router as api_router
from app.config import settings
from app.db import init_db
from app.engine_manager import start_engine_for_slave, stop_engine
from app.events import EventBus
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

    engines = [
        await start_engine_for_slave(slave, bus, events, notifier) for slave in load_enabled_slaves()
    ]

    app.state.bus = bus
    app.state.receiver = receiver
    app.state.engines = engines
    app.state.events = events
    app.state.notifier = notifier
    app.state.master_watchdog = master_watchdog
    app.state.start_time = START_TIME

    yield

    logger.info("shutting down trade copier hub")
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
