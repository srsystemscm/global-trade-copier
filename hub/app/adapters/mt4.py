import asyncio
import json
import logging
from typing import List, Optional

import zmq
import zmq.asyncio

from app.adapters.base import (
    AccountSummary,
    AdapterError,
    AdapterRejectedError,
    AdapterTimeoutError,
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    Position,
)
from app.atr import Bar
from app.config import settings
from app.retry import retry_async

logger = logging.getLogger(__name__)


class MT4Adapter(BrokerAdapter):
    """Talks to a slave MT4 bridge EA over a ZMQ REQ/REP pair.

    REQ/REP allows exactly one request in flight at a time -- if a reply
    never arrives the socket is stuck (it will refuse to send again) until
    it's rebuilt, so requests are serialized with a lock and the socket is
    recreated after a timeout. On timeout, the rebuilt request is also
    retried (with backoff) up to `max_retries` times before giving up --
    this is what lets the hub survive a slave EA restarting mid-flight
    without losing the in-flight command.
    """

    def __init__(
        self,
        slave_id: str,
        host: str,
        port: int,
        timeout_ms: int = 5000,
        max_retries: Optional[int] = None,
        retry_base_delay: Optional[float] = None,
    ) -> None:
        self.slave_id = slave_id
        self.host = host
        self.port = port
        self.timeout_s = timeout_ms / 1000
        self.max_retries = max_retries if max_retries is not None else settings.adapter_max_retries
        self.retry_base_delay = retry_base_delay if retry_base_delay is not None else settings.adapter_retry_base_delay
        self.ctx = zmq.asyncio.Context.instance()
        self.sock: Optional[zmq.asyncio.Socket] = None
        self._lock = asyncio.Lock()

    def _open_socket(self) -> None:
        if self.sock is not None:
            self.sock.close(linger=0)
        self.sock = self.ctx.socket(zmq.REQ)
        self.sock.setsockopt(zmq.LINGER, 0)
        self.sock.connect(f"tcp://{self.host}:{self.port}")

    async def connect(self) -> None:
        self._open_socket()
        logger.info("MT4Adapter[%s] connected to tcp://%s:%s", self.slave_id, self.host, self.port)

    async def disconnect(self) -> None:
        if self.sock is not None:
            self.sock.close(linger=0)
            self.sock = None

    async def _request_once(self, command: dict) -> dict:
        async with self._lock:
            assert self.sock is not None, "adapter not connected"
            await self.sock.send_json(command)
            try:
                raw = await asyncio.wait_for(self.sock.recv(), timeout=self.timeout_s)
            except asyncio.TimeoutError:
                logger.error(
                    "MT4Adapter[%s] timed out waiting for ACK on %s", self.slave_id, command.get("cmd")
                )
                self._open_socket()  # the REQ socket is now out of sync; must be rebuilt
                raise AdapterTimeoutError(
                    f"slave {self.slave_id} did not ACK {command.get('cmd')} in time"
                )

            reply = json.loads(raw.decode("utf-8"))
            if reply.get("status") != "ok":
                raise AdapterRejectedError(reply.get("message", "slave rejected command"))
            return reply

    async def _request(self, command: dict) -> dict:
        return await retry_async(
            lambda: self._request_once(command),
            retries=self.max_retries,
            base_delay=self.retry_base_delay,
            retryable=(AdapterTimeoutError,),
        )

    async def open(self, order: OrderRequest) -> OrderResult:
        reply = await self._request(
            {
                "cmd": "OPEN",
                "master_ticket": order.master_ticket,
                "symbol": order.symbol,
                "direction": order.direction,
                "lots": order.size,
                "sl": order.sl,
                "tp": order.tp,
            }
        )
        return OrderResult(slave_ticket=int(reply["slave_ticket"]), price=reply.get("price"))

    async def modify(self, slave_ticket: int, sl: Optional[float], tp: Optional[float]) -> None:
        await self._request({"cmd": "MODIFY", "ticket": slave_ticket, "sl": sl, "tp": tp})

    async def close(self, slave_ticket: int) -> None:
        await self._request({"cmd": "CLOSE", "ticket": slave_ticket})

    async def get_status(self) -> bool:
        try:
            await self._request({"cmd": "PING"})
            return True
        except AdapterError:
            return False

    async def get_account_summary(self, account_type: str) -> AccountSummary:
        raise NotImplementedError("MT4Adapter (mirror mode) does not support account summaries")

    async def get_price_history(self, symbol: str, period: int) -> List[Bar]:
        raise NotImplementedError("MT4Adapter (mirror mode) does not support price history / ATR")

    async def get_quote(self, symbol: str) -> float:
        raise NotImplementedError("MT4Adapter (mirror mode) does not support quotes")

    async def get_open_positions(self, account_type: str) -> List[Position]:
        raise NotImplementedError("MT4Adapter (mirror mode) does not report live positions/P&L")
