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
    account: Optional[str] = None
    ts: float = Field(default_factory=lambda: time.time())


class Heartbeat(BaseModel):
    account: Optional[str] = None
    ts: float = Field(default_factory=lambda: time.time())
