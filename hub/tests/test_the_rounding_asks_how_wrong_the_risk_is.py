"""Rounding to the bigger contract is judged by what it costs.

`round_band` asks how close the exact figure is to a whole contract.
That is not a cost. The same 0.65 of a contract is +20.7% of the
intended risk at 1 contract and +5.2% at 6, because the fraction is
divided by a different total each time.

MEASURED, over the 18 band decisions on 2026-11-16:

    to ACCEPT the 3-contract cases, band must be >= 0.419
    to REJECT the 1-contract cases, band must be  < 0.290
    IMPOSSIBLE

No band width exists that does what Chris wanted. The clearest single
case: `GC wants 6.65339` -- rounding up to 7 costs 5.2% of the intended
risk, and the band refused it, because 0.653 is not within 0.25 of a
whole number. The band was not too wide or too narrow. It was measuring
the wrong thing.

`max_risk_error` asks the question directly: round to nearest, and step
down to the smaller contract only when that rounding is wrong by more
than the tolerance. Any tolerance in [12.2%, 19.5%) reproduces the
behaviour he asked for; 0.15 sits in the middle.
"""
import pytest

from app.contract_specs import ContractSpec
from app.sizing import compute_size


# GC: 100oz, $10 a tick at 0.10. One contract, so min_size 1.
GC = ContractSpec(symbol="GC", tick_size=0.1, tick_value=10.0,
                  point_value=100.0, min_size=1.0)


def _wants(units, *, tol=None, band=None):
    """Size a signal engineered to want exactly `units` GC contracts.

    Risk per contract is (entry - stop) * point_value, so the capital to
    stake is chosen to make the division come out where the test wants
    it. Working backwards from the number under test keeps the cases
    readable as the figures the log actually printed.
    """
    entry, stop = 3900.0, 3890.0          # $1000 of risk per contract
    balance = units * 1000.0 / 0.01       # at 1% risk
    return compute_size(
        sizing_config={"mode": "pct_risk", "risk_pct": 0.01,
                       "capital_base": "balance"},
        master_lots=1.0,
        entry_price=entry,
        stop_price=stop,
        account_balance=balance,
        account_equity=balance,
        contract_spec=GC,
        round_band=band,
        max_risk_error=tol,
    )


# ------------------------------------------- the cases from the log ----

# (units GC wanted, whether 0.15 should take it, what it rounds to)
LIVE = [
    (3.41875, True, 3),
    (1.65657, False, None),
    (3.40350, True, 3),
    (1.67331, False, None),
    (3.39093, True, 3),
    (3.38326, True, 3),
    (1.66013, False, None),
    (3.34723, True, 3),
    (1.28994, False, None),
    (3.30507, True, 3),
    (6.65339, True, 7),
]


@pytest.mark.parametrize("units,take,expect", LIVE,
                         ids=[f"{u:g}" for u, _, _ in LIVE])
def test_every_live_decision_comes_out_as_asked(units, take, expect):
    """THE POINT. Eleven real decisions, one tolerance, the answer Chris
    wanted on all of them -- which no band width can give."""
    got = _wants(units, tol=0.15)
    if take:
        assert not got.skipped, (
            f"{units:g} GC was refused: {got.reason}"
        )
        assert got.size == expect, f"took {got.size} GC, not {expect}"
    else:
        assert got.skipped, (
            f"{units:g} GC was taken at {got.size}, but rounding there is "
            "further off the intended risk than the tolerance allows"
        )


def test_the_case_the_band_got_most_wrong():
    """6.65 GC rounds up to 7 for 5.2% -- and the band refused it,
    because 0.653 is not within 0.25 of a whole number."""
    assert _wants(6.65339, band=0.25).skipped
    assert _wants(6.65339, tol=0.15).size == 7


def test_the_case_the_band_got_right_is_still_right():
    """1.66 GC rounds to 2 for +20.7%. The micro takes 17 of its own at
    no rounding cost at all, so stepping down is correct -- and the new
    rule has to keep agreeing."""
    assert _wants(1.65657, band=0.25).skipped
    assert _wants(1.65657, tol=0.15).skipped


# ------------------------------------------- the rule itself ----------

def test_it_rounds_to_the_nearest_not_down():
    """Half the point. A band accepts only near-misses on either side;
    this takes whichever whole contract is closer and then asks what
    that cost."""
    assert _wants(3.9, tol=0.15).size == 4
    assert _wants(3.1, tol=0.15).size == 3


def test_a_tolerance_of_zero_only_takes_an_exact_fit():
    got = _wants(3.0, tol=0.0)
    assert not got.skipped and got.size == 3
    assert _wants(3.05, tol=0.0).skipped


def test_it_never_rounds_up_to_one_from_below():
    """Below a whole contract the smaller instrument always fits better.
    The arithmetic agrees -- 0.65 -> 1 is +53% -- but a generous
    tolerance must not be able to talk it into one contract."""
    assert _wants(0.65239, tol=0.15).skipped
    assert _wants(0.9, tol=0.60).skipped, (
        "a wide tolerance rounded a sub-contract signal up to 1"
    )


def test_the_refusal_says_what_it_measured():
    """Each candidate reads its own contract's bars, so a reader needs
    the figure and the cost, not just the verdict."""
    got = _wants(1.65657, tol=0.15)
    assert "1.65657" in got.reason
    assert "%" in got.reason and "15%" in got.reason


def test_the_tolerance_wins_where_both_are_set():
    """A stale band left in a config must not quietly decide anything."""
    assert _wants(3.34723, tol=0.15, band=0.25).size == 3


def test_a_slave_with_neither_is_unchanged():
    """Nothing happens to a config that has not asked for either. This
    is the path every slave was on before any of it existed."""
    got = _wants(3.34723)
    assert not got.skipped
    assert got.size == 3          # "nearest_min1", the standing default


def test_the_band_still_works_on_its_own():
    """Kept, because configs carry it. 3.1 is within 0.25 of 3."""
    assert _wants(3.1, band=0.25).size == 3
    assert _wants(3.4, band=0.25).skipped
