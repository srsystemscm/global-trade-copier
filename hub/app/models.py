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
    account: Optional[str] = None
    ts: float = Field(default_factory=lambda: time.time())

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
