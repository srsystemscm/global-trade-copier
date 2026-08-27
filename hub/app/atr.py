from dataclasses import dataclass
from typing import Sequence


@dataclass
class Bar:
    high: float
    low: float
    close: float


def true_range(bar: Bar, prev_close: float) -> float:
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def compute_atr(bars: Sequence[Bar], period: int = 14) -> float:
    """Simple moving average of true range over `period` bars.

    Classic Wilder ATR uses a smoothed (exponential-like) average; a plain
    average is close enough for sizing/risk purposes here and is far
    simpler to keep correct.
    """
    if len(bars) < period + 1:
        raise ValueError(f"need at least {period + 1} bars to compute a {period}-period ATR, got {len(bars)}")

    trs = [true_range(bars[i], bars[i - 1].close) for i in range(1, len(bars))]
    recent = trs[-period:]
    return sum(recent) / len(recent)
