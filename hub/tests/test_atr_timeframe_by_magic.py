"""A strategy's ATR timeframe follows the strategy, not the symbol.

Chris's weekly systems signal off weekly data and execute on the daily
bar at Monday's open, because MT4 has no weekly execution bar. The slave
still has to measure its ATR on W1: the master states how many ATRs away
its stop sat, and that multiple is only meaningful against the same
timeframe it was measured on.

WHAT GOES WRONG WITHOUT THIS, and it is not a rounding error. A weekly
ATR is roughly five times a daily one. Translate a weekly stop multiple
against a daily ATR and the stop comes out about a fifth of its intended
width. Risk sizing divides the risk amount by the stop distance, so the
position comes out several times too large -- the dollar risk per trade
still reads correct, which is what makes it invisible.

The H1/H4 case errs the other way (stop too wide, position too small),
which costs opportunity rather than money. The weekly case is the
dangerous one.

The leading-digit RULE (1 daily, 2 H4, 3 H1, 4 weekly) sits between this
table and the slave default -- see test_magic_names_its_timeframe.py.
This table is the exception to it, for a strategy whose magic starts 1-4
but whose timeframe does not follow the scheme.

bar_size existed only on the ROUTE, which names a (symbol, magic) pair.
Saying "these magics are weekly" that way needs a route per magic -- 40
routes becoming a hundred to vary one field -- and cannot work at all for
a MIRROR slave, which has no routes and still takes a selection of
strategies.
"""
import pytest

from app.sizing import resolve_bar_size


WEEKLY = {"bar_size_by_magic": {"2001": "1 week", "2002": "1 week"}}


# ------------------------------------------------- precedence ------------

def test_the_route_wins_over_the_table():
    """A route already names a (symbol, magic) pair, so it is the more
    specific statement."""
    assert resolve_bar_size(WEEKLY, "4 hours", 2001) == "4 hours"


def test_the_table_answers_when_the_route_does_not():
    assert resolve_bar_size(WEEKLY, None, 2001) == "1 week"
    assert resolve_bar_size(WEEKLY, None, 2002) == "1 week"


def test_a_slave_wide_default_answers_for_a_magic_outside_the_scheme():
    """Reworded when the leading-digit rule landed. It used to read "the
    slave default is the last word" with magic 1001 -- but 1001 starts
    with 1, so the rule now answers daily for it and OUTRANKS the slave
    default. That ordering is deliberate: a slave-wide bar_size must not
    flatten every strategy's timeframe back to one.

    9999 is outside the scheme, so the question this test means to ask --
    does the slave default still apply? -- can still be asked."""
    assert resolve_bar_size({"bar_size": "4 hours"}, None, 9999) == "4 hours"


def test_an_unlisted_magic_gets_the_default():
    """Not an error: most strategies are daily, and the table names the
    exceptions."""
    assert resolve_bar_size(WEEKLY, None, 9999) is None


def test_none_means_the_adapter_default_which_is_daily():
    """Every route omitting bar_size has always meant daily, and that must
    not change underneath them.

    Asked with a magic the scheme does not name. 2001 was used here before
    the leading-digit rule existed, and the rule now reads it as H4 --
    correctly, since 2 means H4."""
    assert resolve_bar_size({}, None, None) is None
    assert resolve_bar_size({}, None, 9999) is None


# ------------------------------------------------- the awkward cases ----

def test_an_integer_key_is_found_as_well_as_a_string():
    """JSON object keys are strings, but a table written by hand or built
    in Python may use ints. Missing one silently would put a weekly
    strategy on a daily ATR, which is the exact failure this prevents."""
    assert resolve_bar_size({"bar_size_by_magic": {2001: "1 week"}},
                            None, 2001) == "1 week"


def test_an_empty_value_means_the_default_rather_than_a_bad_timeframe():
    """A blank left in a config must not reach an adapter as a bar size it
    has to reject."""
    assert resolve_bar_size({"bar_size_by_magic": {"5": ""}}, None, 5) is None


def test_a_signal_with_no_magic_cannot_match_the_table():
    assert resolve_bar_size(WEEKLY, None, None) is None


def test_a_table_that_is_not_a_table_is_ignored_rather_than_crashing():
    """Config is hand-written. A list here should not take the engine
    down on the signal path."""
    assert resolve_bar_size({"bar_size_by_magic": ["1 week"]}, None, 9999) is None
    assert resolve_bar_size({"bar_size_by_magic": None}, None, 9999) is None
    # And a broken table must not stop the RULE answering, which is the
    # thing most strategies rely on.
    assert resolve_bar_size({"bar_size_by_magic": ["x"]}, None, 421003) == "1 week"


# ------------------------------------------------- it reaches the ATR ---

def test_the_engine_resolves_it_rather_than_reading_the_route_directly():
    """The whole point: a mirror slave has no routes, so reading
    route.bar_size alone could never serve one."""
    import inspect

    from app.autonomous_copy_engine import AutonomousCopyEngine
    src = inspect.getsource(AutonomousCopyEngine._open_one) \
        if hasattr(AutonomousCopyEngine, "_open_one") else ""
    whole = inspect.getsource(AutonomousCopyEngine)
    assert "resolve_bar_size(self.slave_config" in whole, (
        "the engine takes bar_size straight off the route again, so a "
        "per-magic timeframe cannot reach the ATR"
    )
    assert "bar_size = route.bar_size" not in whole


def test_the_weekly_timeframe_survives_to_both_adapters():
    """"1 week" has to be a size both brokers understand, or the table can
    be set and the ATR still comes back daily."""
    from app.adapters.ibkr import _duration_for
    from app.adapters.mt4 import _mql_timeframe

    assert _mql_timeframe("1 week") == "W1"
    # Enough history for a 14-period weekly ATR: compute_atr needs 15
    # bars, the engine asks for 19, so IBKR must be asked for ~19 weeks.
    duration = _duration_for(14, "1 week")
    days = int(duration.split()[0])
    assert days >= 15 * 7, f"{duration} is not 15 weekly bars of history"


def test_the_stored_atr_is_what_modify_and_trailing_reuse():
    """All ATR calculations from entry onward run off the entry's figure,
    which is what makes one timeframe setting enough. If either path
    recomputed, a weekly position would start trailing on a daily ATR
    halfway through its life."""
    import inspect

    from app.autonomous_copy_engine import AutonomousCopyEngine
    for method in (AutonomousCopyEngine._check_risk,
                   AutonomousCopyEngine._handle_modify):
        src = inspect.getsource(method)
        assert "get_price_history" not in src, (
            f"{method.__name__} recomputes the ATR, so it can disagree with "
            f"the timeframe the entry was measured on"
        )
        assert 'pos["atr"]' in src
