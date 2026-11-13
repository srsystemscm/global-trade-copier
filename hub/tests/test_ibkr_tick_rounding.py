"""Order prices must land on the contract's minimum price variation.

IBKR rejects anything else with "Error 110: The price does not conform to the
minimum price variation for this contract" -- and rejects it asynchronously,
so the entry fills while both bracket legs die moments later. Seen live on
2026-10-29: a stop at 3954.8454525079565 on GC, which moves in 0.10 steps.
"""
import pytest

from app.adapters.ibkr import IBKRAdapter

r = IBKRAdapter._round_to_tick


def test_the_price_that_was_rejected_live():
    assert r(3954.8454525079565, "GC") == 3954.8


def test_target_rounds_too():
    assert r(7703.0085929445495, "GC") == 7703.0


@pytest.mark.parametrize("raw,expected", [
    (4040.74, 4040.7), (4040.75, 4040.8), (4040.76, 4040.8), (4040.70, 4040.7),
])
def test_gc_snaps_to_ten_cent_ticks(raw, expected):
    assert r(raw, "GC") == expected


def test_result_is_an_exact_multiple_of_the_tick():
    """Rounding the multiple rather than the price -- 0.10 isn't exact in
    binary floating point, and a near-miss is still Error 110."""
    for raw in (3954.8454525079565, 4123.456789, 1.05, 99999.99999):
        out = r(raw, "GC")
        assert abs(round(out / 0.10) * 0.10 - out) < 1e-9


def test_mes_uses_its_own_quarter_point_tick():
    assert r(5123.30, "MES") == 5123.25
    assert r(5123.40, "MES") == 5123.50


def test_equities_fall_back_to_cents():
    assert r(373.5149, "GLD") == 373.51


def test_none_passes_through():
    assert r(None, "GC") is None
