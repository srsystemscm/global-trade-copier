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
    BrokerSymbolSpec,
    MarginPreview,
    OrderRequest,
    OrderResult,
    Position,
)
from app.atr import Bar
from app.config import settings
from app.retry import retry_async

logger = logging.getLogger(__name__)


def _level(value) -> Optional[float]:
    """A protective level as reported by the bridge EA.

    MQL4's OrderStopLoss() and MQL5's POSITION_SL both return 0.0 when no
    level is set, and the EAs format numbers rather than emitting JSON null.
    Taken literally that is a stop at a price of zero -- the same trap the
    Signal model already guards on the way in. Absent, null and 0.0 all mean
    "no level", so they all become None.
    """
    if value is None:
        return None
    level = float(value)
    return level if level != 0.0 else None


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
        return OrderResult(
            slave_ticket=int(reply["slave_ticket"]),
            price=_level(reply.get("price")),
            lots=reply.get("lots"),
        )

    async def close(self, slave_ticket: int) -> Optional[float]:
        """Returns the price the leg closed at, when the EA reports one.

        An already-closed leg has no close price to give -- it was closed by
        something outside the copier -- so that path returns None rather than
        inventing one.
        """
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
            return None
        return _level(reply.get("price"))

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
        """Balance and equity, via the EA's ACCOUNT command.

        `account_type` is accepted for interface parity and ignored: an MT
        terminal is logged into exactly one account.

        This is what lets a mirror slave size by a percentage of its own
        capital instead of by a multiple of the master's lots. Before
        ACCOUNT existed it could not -- the bridge had no way to say what the
        account was worth, which is the whole reason mirror mode only ever
        had risk_scale.
        """
        reply = await self._ask_tolerating_old_ea({"cmd": "ACCOUNT"}, "ACCOUNT")
        return AccountSummary(
            balance=float(reply["balance"]), equity=float(reply["equity"])
        )

    async def get_symbol_spec(self, symbol: str) -> BrokerSymbolSpec:
        """One symbol's specification and current price, via SYMBOLS.

        Not part of the BrokerAdapter interface -- see BrokerSymbolSpec.
        Callers check for the method, because only the MT bridges answer it.
        """
        reply = await self._ask_tolerating_old_ea(
            {"cmd": "SYMBOLS", "symbols": [symbol]}, "SYMBOLS"
        )
        rows = reply.get("symbols") or []
        if not rows or not rows[0].get("exists"):
            raise AdapterRejectedError(
                f"{symbol} is not carried by this slave's server"
            )
        row = rows[0]
        return BrokerSymbolSpec(
            symbol=row["symbol"],
            digits=int(row["digits"]),
            tick_size=float(row["tick_size"]) or float(row.get("point") or 0.0),
            tick_value=float(row["tick_value"]),
            volume_step=float(row["volume_step"]),
            volume_min=float(row["volume_min"]),
            volume_max=float(row["volume_max"]),
            bid=float(row.get("bid") or 0.0),
            ask=float(row.get("ask") or 0.0),
            trade_mode=str(row.get("trade_mode") or ""),
            contract_size=float(row.get("contract_size") or 0.0),
            expiry=str(row.get("expiry") or ""),
        )

    async def preview_margin(self, order: OrderRequest) -> MarginPreview:
        """What the broker would hold for this order, from its own sum.

        Via the EA's MARGIN command, which wraps MQL5's OrderCalcMargin. It
        prices a STANDALONE position: netting against existing opposite
        exposure is not modelled, so the figure is the worst case. That is
        the right direction for a ceiling, and it reads high on an account
        already positioned the other way.
        """
        reply = await self._ask_tolerating_old_ea(
            {
                "cmd": "MARGIN",
                "symbol": order.symbol,
                "direction": order.direction,
                "lots": order.size,
            },
            "MARGIN",
        )
        margin = float(reply["margin"])
        used = float(reply.get("margin_used") or 0.0)
        return MarginPreview(
            initial_change=margin,
            initial_after=used + margin,
            per_unit=float(reply.get("margin_per_lot") or 0.0) or None,
            free_before=float(reply.get("margin_free") or 0.0) or None,
        )

    async def _ask_tolerating_old_ea(self, command: dict, name: str) -> dict:
        """A read whose command may postdate the EA on the chart.

        An EA compiled before these commands answers "unknown cmd", which is
        a configuration state rather than a fault -- a hub upgraded ahead of
        the EAs on someone's desktop is perfectly ordinary. Turned into
        NotImplementedError so callers can degrade, with the recompile named
        once here rather than at every call site.

        retries=0: these are reads taken on the signal path, and a dead slave
        needs to say so quickly rather than spend the retry budget.
        """
        try:
            return await self._request(command, retries=0)
        except AdapterRejectedError as exc:
            if "unknown cmd" in str(exc).lower():
                logger.warning(
                    "slave=%s EA predates the %s command -- recompile "
                    "bridge_ea/SlaveBridge.mq5 (1.03 or later) in this terminal "
                    "to enable account-based sizing for this slave",
                    self.slave_id, name,
                )
                raise NotImplementedError(
                    f"slave EA predates the {name} command"
                ) from exc
            raise

    async def get_price_history(
        self, symbol: str, period: int, month_slot: int = 0,
        contract_id: Optional[int] = None, bar_size: Optional[str] = None
    ) -> List[Bar]:
        raise NotImplementedError("MT4Adapter (mirror mode) does not support price history / ATR")

    async def get_quote(
        self, symbol: str, month_slot: int = 0, contract_id: Optional[int] = None
    ) -> float:
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
                    sl=_level(p.get("sl")),
                    tp=_level(p.get("tp")),
                )
            )
        return positions
