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
    """Talks to a slave MT4 bridge EA over a ZMQ REQ/REP pair, speaking
    OPEN / MODIFY / CLOSE / PING / POSITIONS.

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
            # Compact separators, not send_json(): json.dumps() defaults to
            # `", "` / `": "`, and SlaveBridge.mq4 parses by hand. The EA now
            # skips whitespace, but keeping the wire compact means the format
            # it sees is deterministic (and the frames are smaller).
            await self.sock.send_string(json.dumps(command, separators=(",", ":")))
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

    async def _request(self, command: dict, retries: Optional[int] = None) -> dict:
        return await retry_async(
            lambda: self._request_once(command),
            retries=self.max_retries if retries is None else retries,
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

    async def close(self, slave_ticket: int) -> None:
        reply = await self._request({"cmd": "CLOSE", "ticket": slave_ticket})
        if reply.get("already_closed"):
            # Not an error: the leg was closed by hand or by its own SL/TP
            # before the master's CLOSE arrived. The EA reports this as
            # success so the caller still records the close in the registry,
            # but it's worth a line in the log since it means someone or
            # something touched the slave account outside the copier.
            logger.warning(
                "MT4Adapter[%s] slave_ticket=%s was already closed before the CLOSE arrived",
                self.slave_id, slave_ticket,
            )

    async def modify(self, slave_ticket: int, sl: Optional[float], tp: Optional[float]) -> None:
        reply = await self._request({"cmd": "MODIFY", "ticket": slave_ticket, "sl": sl, "tp": tp})
        if reply.get("already_closed"):
            logger.warning(
                "MT4Adapter[%s] slave_ticket=%s was already closed, ignoring MODIFY",
                self.slave_id, slave_ticket,
            )

    async def get_status(self) -> bool:
        try:
            await self._request({"cmd": "PING"})
            return True
        except AdapterError:
            return False

    async def get_account_summary(self, account_type: str) -> AccountSummary:
        raise NotImplementedError("MT4Adapter (mirror mode) does not support account summaries")

    async def get_price_history(self, symbol: str, period: int, month_slot: int = 0) -> List[Bar]:
        raise NotImplementedError("MT4Adapter (mirror mode) does not support price history / ATR")

    async def get_quote(self, symbol: str, month_slot: int = 0) -> float:
        raise NotImplementedError("MT4Adapter (mirror mode) does not support quotes")

    async def get_open_positions(self, account_type: Optional[str] = None) -> List[Position]:
        """Open market positions on the slave account, via the EA's POSITIONS
        command. `account_type` is accepted for interface parity and ignored:
        an MT4 terminal is a single account.

        This is what lets reconciliation run for MT4 slaves at all. Before the
        POSITIONS command existed this raised NotImplementedError, so
        reconcile_slave() skipped MT4 entirely and a slave_trades row that
        drifted from the account (a leg closed by hand, an SL that fired while
        the hub was down) could never be corrected.
        """
        try:
            # retries=0 deliberately. This is a read behind GET /status, which
            # the dashboard polls every 5s; retrying a dead slave 3x with
            # backoff made each poll take ~20s and pile requests up. A trade
            # command is worth retrying, a status read is not -- it just needs
            # to say "unknown" quickly.
            reply = await self._request({"cmd": "POSITIONS"}, retries=0)
        except AdapterRejectedError as exc:
            # An EA built before POSITIONS existed answers "unknown cmd".
            # Degrade to the old behaviour rather than failing the caller --
            # a hub upgraded ahead of the EAs on someone's desktop is a
            # perfectly ordinary state.
            if "unknown cmd" in str(exc).lower():
                logger.warning(
                    "slave=%s EA predates the POSITIONS command -- recompile bridge_ea/SlaveBridge.mq4 "
                    "to enable reconciliation and live P&L for this slave",
                    self.slave_id,
                )
                raise NotImplementedError("slave EA predates the POSITIONS command") from exc
            raise

        positions = []
        for p in reply.get("positions", []):
            positions.append(
                Position(
                    symbol=p["symbol"],
                    quantity=float(p["quantity"]),
                    average_price=float(p["average_price"]),
                    unrealized_pnl=p.get("unrealized_pnl"),
                )
            )
        return positions
