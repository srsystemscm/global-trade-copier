"""Risk scale per EA, because leverage is not the same on every venue.

The origin EAs each risk a fixed share of the account they run on -- 2% as
tested. That is the right number on a CFD account and the wrong one once
the same signal lands on a futures or ETF leg, where far less leverage is
available. The knob that fixes it belongs to the strategy, not to the
whole slave: one EA may need halving on the futures slave while the rest
run untouched.

Routes already carried `risk_scale` and already match on (symbol, magic),
so autonomous slaves could do this. Mirror slaves have no routes at all --
`mt5-dwx-futures-1` is a futures slave in mirror mode -- and could only be
scaled wholesale. `risk_scale_by_magic` is what closes that.

Three places can speak and they COMPOSE -- slave x magic x route, each
defaulting to 1.0. It was first-match-wins until 2026-11-15; see
test_a_magic_entry_composes_with_the_slave_wide_scale for why that was
wrong, and app/sizing.py:resolve_risk_scale for the worked example.

Getting this wrong silently resizes real trades, which is why so much of
this file is arithmetic stated twice.
"""
import pytest

from app.sizing import resolve_risk_scale


def test_nothing_configured_changes_nothing():
    """An absent setting must never quietly resize anything."""
    assert resolve_risk_scale({}, None, 1001) == 1.0


def test_the_slave_wide_scale_still_applies():
    assert resolve_risk_scale({"risk_scale": 0.5}, None, 1001) == 0.5


def test_a_magic_entry_composes_with_the_slave_wide_scale():
    """THE CENTRAL CHANGE of 2026-11-15, and the question Chris actually
    asked: do these compound?

    They do, and they must. The slave figure is the venue's intake
    calibration -- how much of ANY signal this account should take, given
    its leverage. The per-magic figure is how this one strategy compares
    to the others. A strategy tuned individually was precisely the one
    that used to ignore a recalibration of the whole slave, which is the
    opposite of useful.

    This asserted 0.25 (the magic replacing the slave). It is now 0.125.
    """
    config = {"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 0.25}}
    assert resolve_risk_scale(config, None, 1001) == 0.125


def test_the_worked_example_from_the_brief():
    """Chris's own arithmetic, kept as the readable statement of intent:

        "most EA are going to send a 2% risk of balance on signal. most
        slaves are going to be calibrated to 0.50 at the intake. on
        average each magic will then get a 1.0 risk scale. some might get
        0.50, which should effectively be 1/4 of the original signal."
    """
    config = {"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 1.0, "2002": 0.5}}
    signal_pct = 2.0

    assert resolve_risk_scale(config, None, 1001) == 0.5
    assert signal_pct * resolve_risk_scale(config, None, 1001) == 1.0

    assert resolve_risk_scale(config, None, 2002) == 0.25
    assert signal_pct * resolve_risk_scale(config, None, 2002) == 0.5
    # "a quarter of the original signal", in so many words.
    assert resolve_risk_scale(config, None, 2002) == 0.5 * 0.5


def test_a_magic_at_one_is_the_identity_rather_than_an_opt_out():
    """The common case: most strategies carry 1.0 and simply inherit the
    slave's calibration. If 1.0 meant "full size regardless", setting the
    table at all would undo the intake calibration for every strategy in
    it."""
    config = {"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 1.0}}
    assert resolve_risk_scale(config, None, 1001) == 0.5


def test_a_magic_not_in_the_table_falls_back():
    config = {"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 0.25}}
    assert resolve_risk_scale(config, None, 9999) == 0.5


def test_all_three_dials_compose():
    """A route names (symbol, magic), so it is the narrowest of the three
    -- but narrower now means "a further adjustment", not "the last word".

    REWORKED 2026-11-15. This asserted 0.75, the route replacing both
    others. Keeping the route as an absolute while the other two composed
    would have been the worst of both: two dials that multiply and a
    third that silently discards them.
    """
    config = {"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 0.25}}
    assert resolve_risk_scale(config, 0.75, 1001) == 0.5 * 0.25 * 0.75
    assert resolve_risk_scale(config, 0.75, 1001) == pytest.approx(0.09375)


def test_an_integer_key_is_found_as_well_as_a_string_one():
    """JSON object keys are strings; a table written by hand or built in
    Python may use integers. Missing one silently would leave the strategy
    at full size while the screen showed it halved."""
    assert resolve_risk_scale({"risk_scale_by_magic": {1001: 0.25}}, None, 1001) == 0.25
    assert resolve_risk_scale({"risk_scale_by_magic": {"1001": 0.25}}, None, 1001) == 0.25


def test_a_signal_with_no_magic_cannot_match_a_magic_entry():
    """Pre-magic bridge EAs send none. Matching them to any entry would
    apply one strategy's scale to every signal that lacks an id."""
    config = {"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 0.25}}
    assert resolve_risk_scale(config, None, None) == 0.5


def test_a_malformed_table_does_not_take_the_slave_down():
    """Config is hand-editable. A list where an object belongs should not
    raise inside the sizing path, which runs on every signal."""
    assert resolve_risk_scale({"risk_scale_by_magic": [1001]}, None, 1001) == 1.0


def test_zero_is_honoured_rather_than_treated_as_absent():
    """0.0 is a real instruction -- 'do not trade this strategy here'. The
    callers refuse on it explicitly; it must not read as 'unset' and become
    1.0, which would trade full size exactly where it was switched off."""
    assert resolve_risk_scale({"risk_scale_by_magic": {"1001": 0.0}}, None, 1001) == 0.0
