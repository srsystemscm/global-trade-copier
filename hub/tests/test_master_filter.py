"""Which masters a slave takes, and why that has to exist before 4-8 of them.

Until this, the bus fanned every signal to every slave and filtered on
magic alone. With one master that is correct. With several it is not, for
a reason that does not announce itself: **magic numbers are per-master.**
Every fxDreema project numbers from MagicStart = 1001, so two masters each
running their first strategy both stamp 1001, and to every slave those are
one strategy. An allow-list naming 1001 takes both; a per-EA risk scale on
1001 resizes both.

The shape deliberately mirrors magic_filter -- allow, deny, or neither --
so there is one idea here rather than two.
"""
import pytest

from app.master_filter import EVERY_MASTER, parse
from app.models import Signal


def _sig(account="44964870", magic=1001):
    return Signal(ticket=1, symbol="XAUUSD", action="OPEN", direction="BUY",
                  lots=1.0, price=4000.0, magic=magic, account=account)


# ------------------------------------------------------------- defaults

def test_no_filter_takes_every_master():
    assert parse({}) is EVERY_MASTER
    assert parse({}).accepts(_sig())[0] is True
    assert parse({}).describe() == "every master"


def test_a_bare_list_is_an_allow_list():
    """The terse form, matching magic_filter's."""
    f = parse({"master_filter": ["44964870"]})
    assert f.accepts(_sig("44964870"))[0] is True
    assert f.accepts(_sig("11558127"))[0] is False


# ------------------------------------------------------------- deciding

def test_a_master_not_on_the_allow_list_is_refused_with_a_reason():
    f = parse({"master_filter": {"allow": ["44964870"]}})
    ok, why = f.accepts(_sig("99999999"))
    assert ok is False
    assert "99999999" in why and "not among" in why


def test_the_deny_list_wins_over_nothing_else_being_said():
    f = parse({"master_filter": {"deny": ["11558127"]}})
    assert f.accepts(_sig("44964870"))[0] is True
    assert f.accepts(_sig("11558127"))[0] is False


def test_account_numbers_compare_as_strings():
    """They are identifiers, not quantities. A config written with JSON
    numbers must still match a signal carrying a string, or the filter
    silently refuses everything."""
    f = parse({"master_filter": [44964870]})
    assert f.accepts(_sig("44964870"))[0] is True


def test_two_masters_running_the_same_magic_are_separable():
    """The whole reason this module exists."""
    takes_a = parse({"master_filter": ["44964870"]})
    takes_b = parse({"master_filter": ["11558127"]})
    from_a, from_b = _sig("44964870", magic=1001), _sig("11558127", magic=1001)

    assert takes_a.accepts(from_a)[0] is True
    assert takes_a.accepts(from_b)[0] is False
    assert takes_b.accepts(from_b)[0] is True
    assert takes_b.accepts(from_a)[0] is False


# ------------------------------------------------- refusing bad config

def test_an_empty_allow_list_is_refused_rather_than_ignored():
    """`{"allow": []}` read as a boolean looks like "no allow-list", which
    would pass every signal it was written to stop. It means "nothing", so
    it is a mistake worth refusing at the API."""
    with pytest.raises(ValueError, match="stop every signal"):
        parse({"master_filter": {"allow": []}})


def test_an_account_in_both_lists_is_refused():
    with pytest.raises(ValueError, match="both allow and deny"):
        parse({"master_filter": {"allow": ["44964870"], "deny": ["44964870"]}})


def test_an_unknown_key_is_a_typo_and_is_refused():
    """A typo in an allow-list is a slave that silently stops trading."""
    with pytest.raises(ValueError, match="unrecognised key"):
        parse({"master_filter": {"allowed": ["44964870"]}})


def test_an_empty_account_number_is_refused():
    with pytest.raises(ValueError, match="empty account number"):
        parse({"master_filter": ["  "]})


# ------------------------------------------------------------- labels

def test_a_label_travels_for_display_and_decides_nothing():
    f = parse({"master_filter": {
        "allow": [{"account": "44964870", "label": "ICM main"}]}})
    assert f.accepts(_sig("44964870"))[0] is True
    assert "ICM main" in f.describe()
    assert f.as_dict()["allow"] == ["44964870"], "the label must not become a key"
