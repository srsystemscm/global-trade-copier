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

Precedence is the whole of the risk here: three places can speak, and a
wrong order silently resizes real trades.
"""
import pytest

from app.sizing import resolve_risk_scale


def test_nothing_configured_changes_nothing():
    """An absent setting must never quietly resize anything."""
    assert resolve_risk_scale({}, None, 1001) == 1.0


def test_the_slave_wide_scale_still_applies():
    assert resolve_risk_scale({"risk_scale": 0.5}, None, 1001) == 0.5


def test_a_magic_entry_beats_the_slave_wide_scale():
    config = {"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 0.25}}
    assert resolve_risk_scale(config, None, 1001) == 0.25


def test_a_magic_not_in_the_table_falls_back():
    config = {"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 0.25}}
    assert resolve_risk_scale(config, None, 9999) == 0.5


def test_the_route_beats_the_magic_table():
    """A route already names (symbol, magic), so it is the more specific
    statement of the two and has to win -- otherwise configuring a route
    would appear to work and then be overridden by a slave-level table."""
    config = {"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 0.25}}
    assert resolve_risk_scale(config, 0.75, 1001) == 0.75


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
