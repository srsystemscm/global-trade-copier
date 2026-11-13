import time
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


class Signal(BaseModel):
    """A trade event published by a master bridge EA."""

    ticket: int
    symbol: str
    action: Literal["OPEN", "MODIFY", "CLOSE"]
    direction: Optional[Literal["BUY", "SELL"]] = None
    lots: Optional[float] = None
    price: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None
    atr: Optional[float] = None  # master instrument's own ATR at signal time (iATR()), for autonomous mode
    # Which strategy on the master opened this. Several strategies routinely
    # run on one account and trade the same symbol; magic is what separates
    # them, and slave routing keys on it. Optional so pre-magic bridge EAs
    # keep working -- their signals simply fall through to the default route.
    magic: Optional[int] = None
    comment: Optional[str] = None
    # Which master account sent this, and part of the key that separates
    # masters. MT4 ticket numbers are per-account, so two masters can
    # legitimately issue the same ticket number for different trades --
    # keyed without the account, one master's CLOSE can close another's leg.
    # Never None: in SQLite two NULLs never compare equal, so a NULL here
    # would silently disable the uniqueness it exists to provide.
    account: str = ""
    ts: float = Field(default_factory=lambda: time.time())
    # When the HUB first saw this signal, stamped by SignalBus.publish_raw().
    #
    # Separate from `ts`, which is the master EA's own TimeGMT(): that clock
    # belongs to another machine, has whole-second resolution, and on a VPS
    # pair routinely sits a second or two off this one. Measuring copy
    # latency against it produces differences that are mostly clock skew --
    # negative ones included -- so signal-to-fill is measured from here
    # instead, and both halves stay on one clock.
    received_at: Optional[float] = None

    @field_validator("account", mode="before")
    @classmethod
    def _account_is_never_null(cls, v):
        """Bridge EAs predating the account field send nothing at all."""
        return v or ""

    @field_validator("sl", "tp", mode="after")
    @classmethod
    def _zero_means_unset(cls, v: Optional[float]) -> Optional[float]:
        """MQL4's OrderStopLoss()/OrderTakeProfit() return 0.0 when no level
        is set, and StringFormat has no way to emit JSON null -- so "no take
        profit" arrives as tp=0.0.

        Taken literally that's a target at a price of zero, and the
        autonomous engine back-calculates its ATR multiple from
        |tp - price|, i.e. the entire price of the instrument. Seen live: a
        master with no TP produced a GC target of 7703 against a 4040 entry.
        A price of zero is never a real level, so it means unset.
        """
        if v is not None and v == 0.0:
            return None
        return v


class Heartbeat(BaseModel):
    account: Optional[str] = None
    ts: float = Field(default_factory=lambda: time.time())
