"""The slave stakes what the master staked, scaled.

Before this, a slave carried its own risk_pct and the master's intention
was a number somebody had typed into the copier -- "2%", because that is
what the origin EAs are configured for. Two places to keep in step by
hand, and nothing to notice when they drifted.

Now the signal carries what the order ACTUALLY staked (risk_amount over
the master's balance) and that replaces the slave's percentage. risk_scale
then says how much of it this slave takes. So "0.5 on magic 1001" means
half of whatever that EA really risked, which is a sentence that stays
true when the EA is changed.

Replaces rather than multiplies, deliberately: a slave percentage AND a
master fraction would be two risk figures with no rule saying which wins.
"""
import pytest

from app.sizing import apply_master_risk


PCT = {"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance"}


def test_the_masters_fraction_replaces_the_configured_one():
    out = apply_master_risk(PCT, 0.02)
    assert out["risk_pct"] == 0.02
    assert PCT["risk_pct"] == 0.01, "the caller's config must not be mutated"


def test_the_configured_percentage_stands_when_the_master_cannot_say():
    """A bridge older than 1.04, an order with no stop, a balance of zero.

    The fallback is the point: without it a slave would silently stop
    sizing the moment an old master connected, which is worse than using
    a number somebody typed.
    """
    assert apply_master_risk(PCT, None)["risk_pct"] == 0.01


@pytest.mark.parametrize("bad", [0.0, -0.01])
def test_a_nonsense_fraction_is_ignored_rather_than_used(bad):
    """0 would size nothing and a negative one would invert the trade.
    Neither is a risk the master meant to take."""
    assert apply_master_risk(PCT, bad)["risk_pct"] == 0.01


def test_only_pct_risk_is_touched():
    """The other modes do not express risk as a fraction of anything, so
    there is nothing for a fraction to replace -- and quietly adding a
    risk_pct to a dollar_notional config would be read by nothing and
    confuse whoever found it."""
    notional = {"mode": "dollar_notional", "notional": 5000}
    assert apply_master_risk(notional, 0.02) == notional

    fixed = {"mode": "fixed_contracts", "contracts": 1}
    assert apply_master_risk(fixed, 0.02) == fixed


def test_an_absent_sizing_block_is_left_alone():
    assert apply_master_risk({}, 0.02) == {}
    assert apply_master_risk(None, 0.02) is None


def test_scale_and_master_fraction_compose():
    """The sentence this is meant to make true: 0.5 on a strategy the
    master risked 2% on stakes 1% of the slave's own account.

    Checked as arithmetic rather than by running an engine, because the
    two are applied in different places -- the fraction here, the scale
    inside compute_size -- and it is their product that matters.
    """
    sized = apply_master_risk(PCT, 0.02)
    effective = sized["risk_pct"] * 0.5
    assert effective == 0.01
