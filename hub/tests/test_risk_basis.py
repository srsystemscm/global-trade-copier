"""What figure risk is measured against, when it is not the broker's.

Two settings, deliberately separate, because they answer different
questions and conflating them is how somebody ends up with a floor they
meant as an adjustment.

`balance_adjustment` corrects a figure that is WRONG. A 250k account pays
50k in tax; the broker reports 200k; the strategies are meant to keep
risking as if it were 250k, because a withdrawal is not a drawdown.

`min_risk_capital` overrides a figure that is RIGHT. That same account
then draws down to 175k. The broker is correct and sizing is told to
carry on at 250k anyway.

The second is not free, and the arithmetic is pinned below rather than
described: 1% of a 250k floor on a 175k account is 1.43% of what the
account actually holds, and it grows as the drawdown does. That is the
point of the setting and also how a drawdown accelerates.
"""
import pytest

from app.sizing import compute_size, resolve_min_risk_capital, risk_figures


# ------------------------------------------------------------- defaults

def test_nothing_configured_uses_the_brokers_own_figures():
    assert risk_figures({}, 200_000, 198_000) == (200_000, 198_000, None)


def test_an_absent_floor_is_zero_not_a_floor():
    assert resolve_min_risk_capital({}) == 0.0
    assert resolve_min_risk_capital({"min_risk_capital": None}) == 0.0


# ------------------------------------------- the adjustment (a correction)

def test_a_withdrawal_can_be_added_back():
    """The case the setting exists for, in the user's own numbers."""
    balance, equity, why = risk_figures(
        {"balance_adjustment": 50_000}, 200_000, 198_000)
    assert (balance, equity, why) == (250_000, 248_000, None)


def test_the_adjustment_is_signed():
    balance, _, _ = risk_figures({"balance_adjustment": -25_000}, 200_000, 200_000)
    assert balance == 175_000


def test_an_adjustment_that_empties_the_account_is_refused():
    """Not clamped to something tradeable: a figure that makes the account
    worthless is a typo, and sizing against a silently corrected one is
    worse than not trading."""
    balance, equity, why = risk_figures(
        {"balance_adjustment": -300_000}, 200_000, 198_000)
    assert balance is None and equity is None
    assert "not something risk can be measured against" in why


# ------------------------------------------------- the floor (an override)

def test_a_drawdown_below_the_floor_still_sizes_at_the_floor():
    balance, equity, why = risk_figures(
        {"min_risk_capital": 250_000}, 175_000, 174_000)
    assert (balance, equity, why) == (250_000, 250_000, None)


def test_the_floor_does_nothing_while_the_account_is_above_it():
    """It is a floor, not a target. An account above it sizes on itself."""
    balance, equity, _ = risk_figures(
        {"min_risk_capital": 250_000}, 400_000, 395_000)
    assert (balance, equity) == (400_000, 395_000)


def test_the_adjustment_is_applied_before_the_floor():
    """Order matters and is not arbitrary: the adjustment says what the
    account really is, and the floor is the least that figure may be. The
    other order would let a withdrawal consume the floor."""
    balance, _, _ = risk_figures(
        {"balance_adjustment": 50_000, "min_risk_capital": 250_000},
        175_000, 175_000)
    assert balance == 250_000, "225k adjusted, lifted to the 250k floor"

    balance, _, _ = risk_figures(
        {"balance_adjustment": 50_000, "min_risk_capital": 250_000},
        250_000, 250_000)
    assert balance == 300_000, "300k adjusted is above the floor, so unchanged"


# ------------------------------------- what it costs, stated as arithmetic

def test_the_floor_risks_more_of_the_account_than_it_says():
    """Pinned because it is the consequence somebody will be surprised by.

    1% against a 250k floor is 2,500. On an account actually holding
    175k that is 1.43%, and the gap widens as the drawdown does.
    """
    balance, _, _ = risk_figures({"min_risk_capital": 250_000}, 175_000, 175_000)
    risked = balance * 0.01
    assert risked == 2_500
    assert round(risked / 175_000, 4) == 0.0143


def test_the_floor_reaches_the_size_that_is_actually_sent():
    """Not just the helper: the figure has to arrive at compute_size, or
    the setting is a number in a config file that changes nothing."""
    on_the_floor = compute_size(
        sizing_config={"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance"},
        master_lots=0.0,
        entry_price=100.0,
        stop_price=90.0,
        account_balance=250_000,   # what risk_figures hands over
        account_equity=250_000,
        contract_spec=None,
        risk_scale=1.0,
    )
    on_the_truth = compute_size(
        sizing_config={"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance"},
        master_lots=0.0,
        entry_price=100.0,
        stop_price=90.0,
        account_balance=175_000,
        account_equity=175_000,
        contract_spec=None,
        risk_scale=1.0,
    )
    assert on_the_floor.size > on_the_truth.size, (
        "the floor must change the size, or it is decorative"
    )
    assert on_the_floor.size == 250, "2,500 of risk over a 10-point stop"
