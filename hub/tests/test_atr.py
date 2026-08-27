import pytest

from app.atr import Bar, compute_atr, true_range


def test_true_range_uses_largest_component():
    bar = Bar(high=105, low=102, close=104)
    # gap up from a much lower previous close should dominate the plain high-low range
    assert true_range(bar, prev_close=90) == pytest.approx(105 - 90)
    # normal case: high-low range dominates
    assert true_range(bar, prev_close=103) == pytest.approx(3)


def test_compute_atr_constant_series():
    bars = [Bar(high=10, low=8, close=9) for _ in range(6)]
    # every bar's true range is high-low=2 (prev close is always 9, |hi-9|=1, |lo-9|=1)
    assert compute_atr(bars, period=4) == pytest.approx(2.0)


def test_compute_atr_requires_enough_bars():
    bars = [Bar(high=10, low=8, close=9) for _ in range(3)]
    with pytest.raises(ValueError):
        compute_atr(bars, period=5)
