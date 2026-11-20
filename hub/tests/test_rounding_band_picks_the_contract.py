"""Take the big contract when rounding is cheap; step down when it is not.

Measured on the 2026-11-15 runs, target risk about $8,923:

                GC exact  taken  cost    MGC exact  taken  cost
  D  121003         0.71      -     -          7.1      7     2%
  H4 221003         1.61      1   38%         16.1     16     0%
  H1 321003         3.34      3   10%         33.4     33     1%

Rounding is the whole problem, and its cost depends on WHERE the exact
figure falls, not on how large it is. 1.61 GC floors to 1 and throws
away 38%; 1.90 rounds to 2 at a cost of 5%. Count cannot tell those
apart, which is why the min_contracts threshold this replaces sent both
to the micros.

Chris's rule, in his own figures: "using the H4 example, the GC exact
was 1.61, for this I'd accept when >1.75, round up. when <1.25 round
down. favoring the larger single contracts despite losing some risk (up
and down) on the rounding."

So a band around each whole number: inside it, take that number -- down
from just above, UP from just below. Outside, step down to the smaller
contract, where the same money divides ten times finer.

A combination fill (1 GC + 3 MGC) was considered and rejected. One
master ticket mapping to two slave tickets on two instruments touches
MODIFY, CLOSE, netting, the roll and reconciliation, and the
half-filled case is the one that motivates it. Chris: "one instrument
easier to track and roll."
"""
import math

import pytest

from app.contract_specs import ContractSpec, get_contract_spec
from app.sizing import compute_size

GC = get_contract_spec("GC")
MGC = get_contract_spec("MGC")

BAL = 866268.0
SIZING = {"mode": "pct_risk", "risk_pct": 0.0103, "capital_base": "balance",
          "rounding": "floor"}
BAND = 0.25


def _size(spec, stop, band=None, sizing=None):
    entry = 3880.0
    return compute_size(
        sizing_config=sizing or SIZING, master_lots=0.13, entry_price=entry,
        stop_price=entry - stop, account_balance=BAL, account_equity=BAL,
        contract_spec=spec, risk_scale=1.0, round_band=band)


def _exact(spec, stop):
    """The unrounded contract count the risk model asked for."""
    return (BAL * 0.0103) / ((stop / spec.tick_size) * spec.tick_value)


# ------------------------------------------------- Chris's own example --

H4_STOP = 55.50      # GC exact 1.61 -- the run that lost 38% to rounding


def test_the_h4_case_steps_down_because_one_point_six_one_is_mid_band():
    assert _exact(GC, H4_STOP) == pytest.approx(1.61, abs=0.01)
    got = _size(GC, H4_STOP, BAND)
    assert got.skipped
    assert "38%" in got.reason, "the reason should say what rounding would cost"
    assert "smaller contract" in got.reason


def test_and_the_micro_then_lands_on_target():
    """16 MGC against 1 GC: the same money, a tenth of the error."""
    taken = _size(MGC, H4_STOP).size
    assert taken == 16.0
    assert abs(taken / _exact(MGC, H4_STOP) - 1) < 0.01


# ------------------------------------------------- the band itself -----

@pytest.mark.parametrize("exact,expected", [
    (1.10, 1),      # just above a whole one -> round DOWN
    (1.24, 1),
    (1.26, None),   # mid-band -> step down
    (1.61, None),   # Chris's case
    (1.74, None),
    (1.76, 2),      # just below the next -> round UP
    (1.90, 2),
    (4.06, 4),
    (8.92, 9),
])
def test_the_band_decides_by_distance_to_a_whole_contract(exact, expected):
    """Driven directly, so the rule is readable without the risk model
    in the way. `expected` None means "step down"."""
    spec = ContractSpec(symbol="X", tick_size=1.0, tick_value=1.0,
                        point_value=1.0, min_size=1.0)
    sizing = {"mode": "pct_risk", "risk_pct": exact / 1000.0,
              "capital_base": "balance", "rounding": "floor"}
    got = compute_size(sizing_config=sizing, master_lots=1.0,
                       entry_price=1000.0, stop_price=999.0,
                       account_balance=1000.0, account_equity=1000.0,
                       contract_spec=spec, risk_scale=1.0, round_band=BAND)
    if expected is None:
        assert got.skipped, f"{exact} should have stepped down"
    else:
        assert not got.skipped and got.size == expected


def test_rounding_up_is_allowed_and_raises_risk_above_target():
    """Chris's explicit trade-off -- "losing some risk (up and down)".
    Stated here because it is the half that costs money rather than
    opportunity, and max_risk_pct is what bounds it."""
    spec = ContractSpec(symbol="X", tick_size=1.0, tick_value=1.0,
                        point_value=1.0, min_size=1.0)
    sizing = {"mode": "pct_risk", "risk_pct": 0.0019, "capital_base": "balance",
              "rounding": "floor"}
    got = compute_size(sizing_config=sizing, master_lots=1.0, entry_price=1000.0,
                       stop_price=999.0, account_balance=1000.0,
                       account_equity=1000.0, contract_spec=spec,
                       risk_scale=1.0, round_band=BAND)
    assert got.size == 2.0, "1.9 should round up"
    assert got.size > 1.9, "which is more risk than the model asked for"


def test_it_never_rounds_up_to_one_from_below():
    """At 0.9 the band would say "take 1", 11% over, while the micro
    takes 9 of its own at no cost. Below a whole contract the smaller
    instrument always fits better, so that steps down like any other."""
    spec = ContractSpec(symbol="X", tick_size=1.0, tick_value=1.0,
                        point_value=1.0, min_size=1.0)
    sizing = {"mode": "pct_risk", "risk_pct": 0.0009, "capital_base": "balance",
              "rounding": "floor"}
    got = compute_size(sizing_config=sizing, master_lots=1.0, entry_price=1000.0,
                       stop_price=999.0, account_balance=1000.0,
                       account_equity=1000.0, contract_spec=spec,
                       risk_scale=1.0, round_band=BAND)
    assert got.skipped


# ------------------------------------------------- the last candidate --

def test_the_last_candidate_is_never_held_to_the_band():
    """THE DANGEROUS CASE. If the band applied to the micro too, a size
    landing mid-band on the smallest contract would step down to nothing
    and fill nothing at all. The engine passes None there; this pins what
    None means."""
    for stop in (125.54, 55.50, 26.69):
        assert not _size(MGC, stop, band=None).skipped


def test_no_band_configured_changes_nothing():
    """Every existing route omits it. None of them may move."""
    for stop in (26.69, 55.50, 125.54):
        for spec in (GC, MGC):
            assert _size(spec, stop).size == _size(spec, stop, band=None).size


# ------------------------------------------------- the ceiling ---------

def test_a_rounded_up_size_is_still_checked_against_the_ceiling():
    """The band reaches `final` by its own path. Extracting the
    max_risk_pct gate was the point: one rounding rule reaching the check
    and another bypassing it is how the second quietly stops being
    guarded."""
    spec = ContractSpec(symbol="X", tick_size=1.0, tick_value=1.0,
                        point_value=1.0, min_size=1.0)
    sizing = {"mode": "pct_risk", "risk_pct": 0.0019, "capital_base": "balance",
              "rounding": "floor", "max_risk_pct": 0.0019}
    got = compute_size(sizing_config=sizing, master_lots=1.0, entry_price=1000.0,
                       stop_price=999.0, account_balance=1000.0,
                       account_equity=1000.0, contract_spec=spec,
                       risk_scale=1.0, round_band=BAND)
    assert got.skipped and "ceiling" in got.reason, (
        "rounding up walked past max_risk_pct"
    )


# ------------------------------------------------- the wiring ----------

def test_the_engine_exempts_the_last_candidate():
    import inspect

    from app.autonomous_copy_engine import AutonomousCopyEngine
    src = inspect.getsource(AutonomousCopyEngine._open_one)
    assert "has_smaller_alternative=(candidate is not candidates[-1])" in src
    sized = inspect.getsource(AutonomousCopyEngine._size_candidate)
    assert 'sizing_config.get("round_band")' in sized
    assert "if has_smaller_alternative else None" in sized
