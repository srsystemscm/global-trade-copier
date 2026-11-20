"""A magic's leading digit names the timeframe its strategy runs on.

Chris's scheme, 2026-11-15: the STARTING number says which timeframe.

    1  daily      121003 is a daily system
    2  H4
    3  H1
    4  weekly

A rule rather than a list, because a list has to be edited before a new
strategy works and nobody remembers to. A strategy deployed on Monday
measures correctly on Monday.

WHY IT MATTERS, and the two directions are not symmetric. A weekly ATR is
roughly five times a daily one. Translate a weekly stop multiple against
a daily ATR and the stop is about a fifth of its width; risk sizing
divides by stop distance, so the position is several times too large --
while the dollar risk per trade still reads correct on every screen,
which is what makes it invisible. The H1/H4 case errs the other way:
stop too wide, position too small, costing opportunity rather than
money.
"""
import pytest

from app.sizing import (DEFAULT_BAR_SIZE_BY_MAGIC_PREFIX,
                        bar_size_for_magic_prefix, resolve_bar_size)


# ------------------------------------------------- the scheme ------------

@pytest.mark.parametrize("magic,expected", [
    (121003, "1 day"),    # the real daily system
    (221003, "4 hours"),
    (321003, "1 hour"),
    (421003, "1 week"),
])
def test_the_leading_digit_names_the_timeframe(magic, expected):
    assert bar_size_for_magic_prefix(magic) == expected


def test_the_shipped_map_is_the_scheme_as_stated():
    """Written down so a change to it is a visible change, not a drift."""
    assert DEFAULT_BAR_SIZE_BY_MAGIC_PREFIX == {
        "1": "1 day", "2": "4 hours", "3": "1 hour", "4": "1 week",
    }


def test_the_digit_count_does_not_matter():
    """Production magics are six digits; the test scripts use four. Only
    the first digit is read, so both work."""
    assert bar_size_for_magic_prefix(1001) == "1 day"
    assert bar_size_for_magic_prefix(121003) == "1 day"
    assert bar_size_for_magic_prefix(1) == "1 day"


# ------------------------------------------------- never guess -----------

@pytest.mark.parametrize("magic", [5555, 7001, 9999, 0, 60000])
def test_a_digit_outside_the_scheme_is_not_guessed(magic):
    """These are test-script magics and whatever comes later. Inventing a
    timeframe for them would be worse than the daily default they get
    now."""
    assert bar_size_for_magic_prefix(magic) is None


def test_no_magic_means_no_opinion():
    assert bar_size_for_magic_prefix(None) is None


def test_the_rule_can_be_switched_off():
    """A master numbering differently must not have its magics
    reinterpreted by someone else's convention."""
    assert bar_size_for_magic_prefix(421003, {}) is None


def test_the_map_is_overridable_per_slave():
    """The convention lives in config, not in engine code: changing a
    trading convention should not need a code change."""
    assert bar_size_for_magic_prefix(421003, {"4": "1 day"}) == "1 day"


# ------------------------------------------------- precedence ------------

def test_an_explicit_table_overrides_the_rule():
    """The escape hatch: one strategy whose magic starts 1-4 but whose
    timeframe does not follow the scheme."""
    config = {"bar_size_by_magic": {"421003": "1 day"}}
    assert resolve_bar_size(config, None, 421003) == "1 day"


def test_a_route_overrides_both():
    config = {"bar_size_by_magic": {"421003": "1 day"}}
    assert resolve_bar_size(config, "1 hour", 421003) == "1 hour"


def test_the_rule_beats_a_slave_wide_default():
    """Otherwise one `bar_size` on the slave flattens every timeframe
    back to one, which is the bug this replaces."""
    assert resolve_bar_size({"bar_size": "1 hour"}, None, 421003) == "1 week"


def test_switching_the_rule_off_falls_back_to_the_slave_default():
    config = {"bar_size_by_magic_prefix": {}, "bar_size": "1 hour"}
    assert resolve_bar_size(config, None, 421003) == "1 hour"


def test_with_the_rule_off_and_nothing_else_it_is_the_adapter_default():
    assert resolve_bar_size({"bar_size_by_magic_prefix": {}}, None, 421003) is None


# ------------------------------------------------- it is safe on ---------

def test_turning_it_on_cannot_regress_a_daily_strategy():
    """The reason this ships enabled. 121003 already resolved to the
    adapter's daily default; under the rule it resolves to daily
    explicitly. Same ATR either way."""
    assert bar_size_for_magic_prefix(121003) == "1 day"
    from app.adapters.mt4 import _mql_timeframe
    assert _mql_timeframe("1 day") == _mql_timeframe(None) == "D1"


def test_every_timeframe_the_scheme_names_reaches_both_brokers():
    """A map entry no adapter understands would let the table be set and
    the ATR still come back daily."""
    from app.adapters.ibkr import _duration_for
    from app.adapters.mt4 import _mql_timeframe

    for bar_size in DEFAULT_BAR_SIZE_BY_MAGIC_PREFIX.values():
        assert _mql_timeframe(bar_size), f"MT cannot express {bar_size!r}"
        # And IBKR must be asked for enough history for a 14-period ATR,
        # which needs 15 bars of that size.
        assert _duration_for(14, bar_size), f"IBKR cannot express {bar_size!r}"
