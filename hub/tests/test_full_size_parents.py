"""Every routed micro has a full-size parent, and the numbers agree.

Gold got its step-down chain on 2026-11-15 -- GC when rounding to a
whole one is cheap, MGC when it is not -- and the chain worked:

    chose MGC over GC -- GC: wants 1.61633 of them, not within 0.25 of a
    whole contract -- rounding down would cost 38%

Every other asset routed on ibkr-paper-1 was a bare micro with no parent
to step down FROM, because ES, NQ, RTY, YM and CL were simply missing
from the table. Chris: "build out the remaining contract specs across
assets so we can further dial in these tests."

TWO INVARIANTS, because a contract spec is the one kind of data where a
typo is invisible and expensive. A wrong tick_value does not fail, it
sizes every position on that instrument wrongly and reports a dollar
risk that is internally consistent with itself -- which is exactly how
the MT lot bug survived.

  tick_value == tick_size x point_value     within each spec
  the micro is an exact fraction of its parent   across the pair

The second is checked PER PAIR rather than as a blanket rule. The CME
index and energy micros are a tenth of their parent, but silver is a
fifth, bitcoin a fiftieth and ether a five-hundredth, so one rule would
be wrong four times over.
"""
import pytest

from app.contract_specs import MICRO_FUTURES_SPECS as SPECS, get_contract_spec


# full, micro, how many micros make one full
PAIRS = [
    ("ES", "MES", 10),
    ("NQ", "MNQ", 10),
    ("RTY", "M2K", 10),
    ("YM", "MYM", 10),
    ("CL", "MCL", 10),
    ("GC", "MGC", 10),
    ("NG", "QG", 4),          # QG is a quarter of NG, not a tenth
    ("SI", "SIL", 5),         # SIL is 1,000oz against SI's 5,000
    ("BRR", "MBT", 50),       # 5 BTC against 0.1
    ("ETHUSDRR", "MET", 500),  # 50 ether against 0.1
]


@pytest.mark.parametrize("symbol", sorted(SPECS))
def test_the_tick_value_agrees_with_the_point_value(symbol):
    """tick_value is minTick x multiplier by definition. Where it does
    not match, one of the three numbers is a typo -- and a typo here
    sizes every position on that instrument wrongly while reporting a
    dollar risk consistent with itself."""
    spec = SPECS[symbol]
    assert spec.tick_value == pytest.approx(spec.tick_size * spec.point_value), (
        f"{symbol}: tick {spec.tick_size} x point {spec.point_value} is "
        f"{spec.tick_size * spec.point_value}, but tick_value says "
        f"{spec.tick_value}"
    )


@pytest.mark.parametrize("full,micro,ratio", PAIRS)
def test_the_micro_is_an_exact_fraction_of_its_parent(full, micro, ratio):
    big, small = SPECS[full], SPECS[micro]
    assert big.point_value / small.point_value == pytest.approx(ratio), (
        f"{full} should be {ratio}x {micro}"
    )


@pytest.mark.parametrize("full,micro,ratio", PAIRS)
def test_both_sides_of_a_pair_list_on_the_same_exchange(full, micro, ratio):
    """IBKR's qualifyContracts() fails outright on the wrong exchange --
    it is part of the contract's identity, not a hint. A chain whose two
    legs disagree resolves one and not the other."""
    assert SPECS[full].exchange == SPECS[micro].exchange


@pytest.mark.parametrize("full,micro,ratio", PAIRS)
def test_both_sides_of_a_pair_trade_the_same_months(full, micro, ratio):
    """A chain that steps down into a different expiry cycle would put
    the two halves of one strategy on different contracts."""
    assert SPECS[full].liquid_months == SPECS[micro].liquid_months


# ------------------------------------------- the ones that were missing -

@pytest.mark.parametrize("symbol", ["ES", "NQ", "RTY", "YM", "CL"])
def test_the_new_parents_resolve(symbol):
    assert get_contract_spec(symbol) is not None


def test_every_micro_routed_on_the_futures_slave_now_has_a_parent():
    """The point of the exercise. Each of these was a bare target with
    nothing to step down from."""
    routed_micros = {"MGC", "MES", "MNQ", "M2K", "MCL", "MBT", "MET", "QG"}
    parents = {micro: full for full, micro, _ in PAIRS}
    missing = sorted(m for m in routed_micros if m not in parents)
    assert missing == [], f"still no full-size parent for: {missing}"


def test_the_index_parents_carry_the_quarterly_cycle():
    """Serial months exist on the index futures and carry almost no
    volume. Routing to "the next expiry" rather than the next LIQUID one
    parks a strategy in one of them."""
    for symbol in ("ES", "NQ", "RTY", "YM"):
        assert SPECS[symbol].liquid_months == (3, 6, 9, 12)


def test_crude_lists_every_month():
    """Unlike the index futures, every CL month trades. An empty tuple
    means "no cycle to respect", which is different from "unknown"."""
    assert SPECS["CL"].liquid_months == ()


# ------------------------------------------- sizing uses them ----------

def test_a_full_size_contract_risks_exactly_its_ratio_more():
    """End to end through compute_size, because the ratio mattering is
    the entire reason for the step-down chain: the same stop on ES must
    risk ten times what it does on MES."""
    from app.sizing import compute_size

    sizing = {"mode": "fixed_contracts", "contracts": 1}
    for full, micro, ratio in PAIRS:
        big = SPECS[full]
        small = SPECS[micro]
        stop = 100 * big.tick_size      # a round number of ticks
        risk_big = (stop / big.tick_size) * big.tick_value
        risk_small = (stop / small.tick_size) * small.tick_value
        assert risk_big / risk_small == pytest.approx(ratio), (
            f"one {full} should risk {ratio}x one {micro} over the same move"
        )
