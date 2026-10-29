import time
from typing import Literal, Optional

from pydantic import BaseModel, Field


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


class Heartbeat(BaseModel):
    account: Optional[str] = None
    ts: float = Field(default_factory=lambda: time.time())
