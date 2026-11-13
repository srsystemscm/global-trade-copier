"""Contract roll: when to move, and in what order.

The ordering test is the important one. Strategies are separated by sitting
one contract month apart, so rolling the front position first would land it on
the month the next strategy still holds -- and on a netting account they merge,
which defeats the separation exactly when it matters.
"""
import pytest

from app.contract_roll import roll_sequence, volume_crossover


# --- trigger: volume crossover --------------------------------------------

def test_does_not_roll_while_the_front_still_leads():
    """Real figures from COMEX gold: GCZ6 29,179 vs GCG7 2,098."""
    d = volume_crossover(29179, 2098)
    assert d.should_roll is False
    assert "front month still leads" in d.reason


def test_rolls_once_the_next_month_takes_over():
    d = volume_crossover(2098, 29179)
    assert d.should_roll is True
    assert "out-trading" in d.reason


def test_equal_volume_rolls_at_the_default_ratio():
    assert volume_crossover(5000, 5000).should_roll is True


def test_min_ratio_can_demand_a_clear_lead():
    assert volume_crossover(5000, 5000, min_ratio=1.5).should_roll is False
    assert volume_crossover(5000, 8000, min_ratio=1.5).should_roll is True


def test_dead_front_month_rolls_regardless():
    assert volume_crossover(0, 10).should_roll is True


@pytest.mark.parametrize("front,nxt", [(None, 100), (100, None), (None, None)])
def test_missing_volume_never_rolls(front, nxt):
    """A roll is two market orders per position. Guessing is worse than being
    a day late."""
    d = volume_crossover(front, nxt)
    assert d.should_roll is False
    assert "not rolling on a guess" in d.reason


# --- order: outermost expiry first ----------------------------------------

def _p(expiry):
    return ({"k": expiry}, {"contract_expiry": expiry})


def test_outermost_expiry_rolls_first():
    seq = roll_sequence([_p("20261229"), _p("20270428"), _p("20270224")])
    assert [p[1]["contract_expiry"] for p in seq] == ["20270428", "20270224", "20261229"]


def test_front_month_is_always_last_to_move():
    """It moves into the month the next strategy has just vacated."""
    seq = roll_sequence([_p("20270224"), _p("20261229")])
    assert seq[-1][1]["contract_expiry"] == "20261229"


def test_positions_without_an_expiry_sort_last():
    seq = roll_sequence([({}, {}), _p("20261229")])
    assert seq[0][1].get("contract_expiry") == "20261229"
    assert seq[-1][1] == {}


def test_single_position_is_unchanged():
    assert len(roll_sequence([_p("20261229")])) == 1


def test_empty_is_fine():
    assert roll_sequence([]) == []
