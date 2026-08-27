import abc
from dataclasses import dataclass
from typing import List, Literal, Optional

from app.atr import Bar


class AdapterError(Exception):
    """Base class for all broker adapter failures."""


class AdapterConnectionError(AdapterError):
    """Could not reach the broker/slave terminal."""


class AdapterTimeoutError(AdapterError):
    """The broker/slave terminal did not reply in time."""


class AdapterRejectedError(AdapterError):
    """The broker/slave terminal rejected the command (e.g. OrderSend failed)."""


@dataclass
class OrderRequest:
    """A broker-agnostic instruction to open a position.

    Deliberately independent of the master Signal: one master signal can
    fan out to several of these (different symbol, size, SL/TP per slave
    target), so the adapter only ever needs to know what to open, not where
    it came from. `master_ticket` is carried along purely for logging/
    traceability on the slave side.
    """

    symbol: str
    direction: Literal["BUY", "SELL"]
    size: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    master_ticket: Optional[int] = None


@dataclass
class OrderResult:
    slave_ticket: int
    price: Optional[float] = None


@dataclass
class AccountSummary:
    balance: float
    equity: float


@dataclass
class Position:
    """A broker-reported open position, for dashboard P&L -- deliberately
    sourced from the broker rather than recomputed from cached entry data,
    since the broker is the source of truth for fees/multipliers/etc.
    """

    symbol: str
    quantity: float
    average_price: float
    market_value: Optional[float] = None
    unrealized_pnl: Optional[float] = None


class BrokerAdapter(abc.ABC):
    """Common interface every slave broker integration must implement.

    Copy Engines talk to slaves only through this interface, so swapping a
    broker (e.g. Schwab -> IBKR) is a config change, never a Copy Engine
    change. Grew from 6 methods in Phase 2 (mirror mode only needed order
    execution) to 10 in Phase 3/4, since autonomous mode also needs broker
    market data and account balances to compute its own SL/TP and size, and
    the dashboard needs live position P&L.
    """

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    async def open(self, order: OrderRequest) -> OrderResult: ...

    @abc.abstractmethod
    async def modify(self, slave_ticket: int, sl: Optional[float], tp: Optional[float]) -> None: ...

    @abc.abstractmethod
    async def close(self, slave_ticket: int) -> None: ...

    @abc.abstractmethod
    async def get_status(self) -> bool: ...

    @abc.abstractmethod
    async def get_account_summary(self, account_type: str) -> AccountSummary: ...

    @abc.abstractmethod
    async def get_price_history(self, symbol: str, period: int) -> List[Bar]: ...

    @abc.abstractmethod
    async def get_quote(self, symbol: str) -> float: ...

    @abc.abstractmethod
    async def get_open_positions(self, account_type: str) -> List[Position]: ...
