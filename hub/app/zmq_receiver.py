import asyncio
import json
import logging
import time

import zmq
import zmq.asyncio

from app.config import settings
from app.signal_bus import SignalBus

logger = logging.getLogger(__name__)


class ZmqReceiver:
    """Owns the two inbound ZMQ sockets:

    - SUB :5555  -- master bridge EAs PUB/connect here and publish trade signals
    - PULL :5557 -- master bridge EAs PUSH/connect here and send heartbeats
    """

    def __init__(self, bus: SignalBus) -> None:
        self.bus = bus
        self.ctx = zmq.asyncio.Context.instance()
        self.sub_sock: zmq.asyncio.Socket | None = None
        self.pull_sock: zmq.asyncio.Socket | None = None
        self._tasks: list[asyncio.Task] = []
        self.last_heartbeat: dict[str, float] = {}
        # Which build each master is running, as the master itself reports
        # it. Sent by TradeCopierBridge 1.04 and up; a master on anything
        # older simply never appears here, and "unknown" is the honest
        # answer for it rather than a guess.
        self.bridge_version: dict[str, str] = {}

    def _bind(self) -> None:
        self.sub_sock = self.ctx.socket(zmq.SUB)
        self.sub_sock.bind(f"tcp://{settings.zmq_bind_host}:{settings.zmq_sub_port}")
        self.sub_sock.setsockopt(zmq.SUBSCRIBE, b"")

        self.pull_sock = self.ctx.socket(zmq.PULL)
        self.pull_sock.bind(f"tcp://{settings.zmq_bind_host}:{settings.zmq_pull_port}")

        logger.info(
            "ZMQ receiver bound: SUB tcp://%s:%s  PULL tcp://%s:%s",
            settings.zmq_bind_host, settings.zmq_sub_port,
            settings.zmq_bind_host, settings.zmq_pull_port,
        )

    async def _sub_loop(self) -> None:
        assert self.sub_sock is not None
        while True:
            msg = await self.sub_sock.recv()
            await self.bus.publish_raw(msg)

    async def _pull_loop(self) -> None:
        assert self.pull_sock is not None
        while True:
            msg = await self.pull_sock.recv()
            try:
                data = json.loads(msg.decode("utf-8"))
                account = str(data.get("account", "unknown"))
                version = data.get("bridge_version")
            except (json.JSONDecodeError, UnicodeDecodeError):
                account = "unknown"
                version = None
            self.last_heartbeat[account] = time.time()
            if version:
                previous = self.bridge_version.get(account)
                self.bridge_version[account] = str(version)
                if previous and previous != str(version):
                    # Worth a line in the log: a master's build changing
                    # under a running hub means somebody recompiled and
                    # reattached, and that is the moment behaviour changes.
                    logger.info(
                        "master account=%s is now running bridge %s (was %s)",
                        account, version, previous,
                    )
            logger.debug("heartbeat from account=%s", account)

    async def start(self) -> None:
        self._bind()
        self._tasks = [
            asyncio.create_task(self._sub_loop(), name="zmq-sub-loop"),
            asyncio.create_task(self._pull_loop(), name="zmq-pull-loop"),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.sub_sock:
            self.sub_sock.close(linger=0)
        if self.pull_sock:
            self.pull_sock.close(linger=0)
        logger.info("ZMQ receiver stopped")
