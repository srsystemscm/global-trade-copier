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


class EntryNeverFilled(AdapterError):
    """A close was asked for on an entry that never filled.

    Not a failure: the order and its protective legs have been cancelled and
    nothing is left at the broker. It is raised rather than returned so the
    caller cannot mistake it for an ordinary close and record a position that
    never existed as one that was closed.
    """


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
    # "market" or "limit". Outside regular hours a market order is simply
    # refused by IBKR, so an ETF leg meant to fill in the evening session
    # has to be a limit with `outside_rth` set -- both, not either.
    order_type: str = "market"
    limit_price: Optional[float] = None
    outside_rth: bool = False
    # How far through the quote to price a limit, carried so the EXIT can be
    # priced the same way as the entry. Without it the adapter would have to
    # guess, and a market exit cannot fill outside regular hours.
    limit_offset_pct: float = 0.0
    # Whether to place broker-side stop/target orders at all. ETF legs set
    # this false: those orders do not execute in extended hours, so resting
    # them achieves nothing while leaving GTC orders to strand. Those legs
    # are closed by the master's CLOSE signal instead.
    broker_brackets: bool = True
    # Futures contract month: 0 = front, 1 = next liquid month, etc. Used to
    # keep strategies on one netting account from merging into one position.
    # Ignored by brokers/instruments that have no contract months.
    month_slot: int = 0
    # An exact contract to trade, overriding month_slot. Set when rolling: the
    # destination is a specific contract, not "whatever slot N is right now".
    contract_id: Optional[int] = None


@dataclass
class OrderResult:
    slave_ticket: int
    price: Optional[float] = None
    # What actually went on, after the broker's own volume rounding. None
    # when the broker doesn't say, in which case the requested size stands.
    lots: Optional[float] = None
    # Broker-side bracket legs, recorded so they survive a hub restart --
    # without them a restored position can't have its stop moved or cancelled.
    sl_order_id: Optional[int] = None
    tp_order_id: Optional[int] = None


@dataclass
class AccountSummary:
    balance: float
    equity: float


@dataclass
class ContractInfo:
    """One dated futures contract, identified concretely rather than by slot.

    Positions store this so a roll targets an exact contract: slot numbering
    shifts under you when the front month rolls, an expiry does not.
    """

    symbol: str          # root, e.g. "GC"
    contract_id: int     # broker contract id (IBKR conId)
    local_symbol: str    # e.g. "GCZ6"
    expiry: str          # YYYYMMDD


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
    # The protective levels the broker is actually holding, which is the only
    # way to confirm a MODIFY landed -- nothing else reads a leg's stop back.
    # 0 from MT4/MT5 means "no level set" and arrives here as None, so None
    # is "unprotected or unknown", never "a stop at zero". Left None by
    # adapters that cannot report it (IBKR holds its stops as separate
    # bracket orders rather than attributes of the position).
    sl: Optional[float] = None
    tp: Optional[float] = None


class BrokerAdapter(abc.ABC):
    """Common interface every slave broker integration must implement.

    Copy Engines talk to slaves only through this interface, so swapping a
    broker (e.g. Schwab -> IBKR) is a config change, never a Copy Engine
    change. Grew from 6 methods in Phase 2 (mirror mode only needed order
    execution) to 10 in Phase 3/4, since autonomous mode also needs broker
    market data and account balances to compute its own SL/TP and size, and
    the dashboard needs live position P&L.
    """

    # True for adapters holding a stateful broker session that, once dropped,
    # cannot be re-established by the adapter's own next call. The watchdog
    # rebuilds those; everything else is left alone. MT4Adapter rebuilds its
    # REQ socket on timeout and Schwab is stateless HTTP, so only IBKR --
    # whose single TCP session dies on IB Gateway's forced daily restart --
    # sets this.
    requires_session_recovery: bool = False

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @abc.abstractmethod
    async def open(self, order: OrderRequest) -> OrderResult: ...

    @abc.abstractmethod
    async def modify(self, slave_ticket: int, sl: Optional[float], tp: Optional[float]) -> None: ...

    @abc.abstractmethod
    async def close(self, slave_ticket: int) -> Optional[float]: ...

    @abc.abstractmethod
    async def get_status(self) -> bool: ...

    @abc.abstractmethod
    async def get_account_summary(self, account_type: str) -> AccountSummary: ...

    @abc.abstractmethod
    async def get_price_history(
        self, symbol: str, period: int, month_slot: int = 0,
        contract_id: Optional[int] = None, bar_size: Optional[str] = None
    ) -> List[Bar]: ...

    @abc.abstractmethod
    async def get_quote(
        self, symbol: str, month_slot: int = 0, contract_id: Optional[int] = None
    ) -> float: ...

    # --- contract months (futures only) ---------------------------------
    # Default to NotImplementedError so brokers and instruments without
    # contract months are simply skipped by the roller rather than special
    # -cased at every call site.

    async def resolve_contract(self, symbol: str, month_slot: int = 0) -> "ContractInfo":
        """The concrete contract a (symbol, month_slot) currently resolves to."""
        raise NotImplementedError(f"{type(self).__name__} has no contract months")

    async def next_contract_after(self, symbol: str, contract_id: int) -> Optional["ContractInfo"]:
        """The next liquid contract after `contract_id`, or None if it's the last."""
        raise NotImplementedError(f"{type(self).__name__} has no contract months")

    async def restore_position(self, slave_ticket: int, state: dict) -> None:
        """Rebuild this adapter's in-memory bookkeeping for a position that
        was open before the hub restarted.

        Without it an adapter that tracks positions in memory (IBKR, Schwab)
        can't modify or close anything it didn't open this process lifetime.
        Default is a no-op: MT4 holds no per-position state -- it addresses
        orders by the terminal's own ticket.
        """
        return None

    async def get_contract_volume(
        self, symbol: str, month_slot: int = 0, contract_id: Optional[int] = None
    ) -> Optional[float]:
        """Session volume for one contract. None when no figure is available."""
        raise NotImplementedError(f"{type(self).__name__} has no contract months")

    @abc.abstractmethod
    async def get_open_positions(self, account_type: str) -> List[Position]: ...
