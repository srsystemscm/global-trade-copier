"""Two keys meant "the margin ceiling", and each engine read only one.

    netting.max_margin_pct   autonomous engine -- refuses the leg, and
                             steps down the candidate chain
    margin.max_pct_of_free   mirror engine     -- trims the size to fit

Found on 2026-11-16 when Chris asked whether the margin cap was
editable from the risk tab. It was not editable anywhere, and worse:

  ibkr-paper-1, ibkr-etf-1   netting.max_margin_pct 0.40, ENFORCED,
                             and the card showed no ceiling at all
  mt5-dwx-futures-1          margin.max_pct_of_free 0.25, displayed as a
                             badge, and NOT ENFORCED -- it runs
                             autonomous, which reads the other key

So one slave was capped and looked uncapped, another looked capped and
was not. Mode is switchable from that same card in two clicks, so a
slave could change which ceiling applied without anyone touching a
ceiling.

ONE RESOLVER, read by all three sites. The two engines keep their own
BEHAVIOUR -- trimming and refusing are different answers to the same
question, and changing either silently would be a behaviour change
wearing a rename's clothes. What they can no longer do is disagree about
whether a ceiling exists at all.
"""
import pytest

from app.sizing import resolve_margin_cap


# -------------------------------------------------- the resolver -------

def test_the_autonomous_key_is_read():
    assert resolve_margin_cap({"netting": {"max_margin_pct": 0.4}}) == 0.4


def test_the_mirror_key_is_read():
    """The legacy block still works. Existing configs must not need
    editing to stay capped -- that is the whole point of accepting both."""
    assert resolve_margin_cap({"margin": {"max_pct_of_free": 0.25}}) == 0.25


def test_a_route_overrides_the_slave():
    """A route already names a (symbol, magic) pair, so it is the more
    specific statement -- the same precedence the other caps use."""
    assert resolve_margin_cap(
        {"netting": {"max_margin_pct": 0.4}}, {"max_margin_pct": 0.1}) == 0.1


def test_netting_wins_over_the_legacy_block():
    """A slave carrying both is mid-migration. The canonical key is the
    one the UI writes, so it has to be the one that answers."""
    assert resolve_margin_cap({
        "netting": {"max_margin_pct": 0.4},
        "margin": {"max_pct_of_free": 0.25},
    }) == 0.4


def test_no_cap_anywhere_is_none_not_zero():
    """None means "no ceiling"; 0.0 would mean "nothing may be traded".
    Confusing them stops the slave dead."""
    assert resolve_margin_cap({}) is None
    assert resolve_margin_cap({"netting": {"max_legs": 3}}) is None
    assert resolve_margin_cap(None) is None


def test_a_zero_ceiling_is_honoured_rather_than_read_as_absent():
    assert resolve_margin_cap({"netting": {"max_margin_pct": 0.0}}) == 0.0


def test_a_malformed_block_does_not_raise_on_the_signal_path():
    """Config is hand-editable and this runs on every open."""
    assert resolve_margin_cap({"netting": ["nope"]}) is None
    assert resolve_margin_cap({"margin": None}) is None


# -------------------------------------------------- both engines use it -

def test_the_autonomous_engine_resolves_rather_than_reading_one_key():
    import inspect

    from app.autonomous_copy_engine import AutonomousCopyEngine
    src = inspect.getsource(AutonomousCopyEngine)
    assert "resolve_margin_cap(self.slave_config, route.netting)" in src
    assert 'policy.get("max_margin_pct")' not in src, (
        "it still reads one key directly, so a mirror-configured slave "
        "switched to autonomous loses its ceiling"
    )


def test_the_mirror_engine_resolves_too():
    import inspect

    from app.copy_engine import CopyEngine
    src = inspect.getsource(CopyEngine)
    assert "resolve_margin_cap(self.slave_config)" in src
    assert 'self.slave_config.get("margin")\n' not in src, (
        "it still reads the legacy block alone, so a slave configured "
        "with the canonical key is not capped"
    )


def test_the_api_reports_the_ceiling_actually_in_force():
    """The card read margin.max_pct_of_free directly, which is why the
    two capped slaves displayed nothing and the uncapped one displayed
    25%."""
    import inspect

    from app import api_routes
    src = inspect.getsource(api_routes._slave_summary)
    assert '"margin_cap_pct": resolve_margin_cap(slave.config)' in src


# -------------------------------------------------- the real configs ----

@pytest.mark.parametrize("config,expected", [
    ({"netting": {"allow_opposing": True, "max_margin_pct": 0.4}}, 0.4),
    ({"netting": {"max_legs": 3, "max_aggregate_risk_pct": 0.06,
                  "allow_opposing": False, "max_margin_pct": 0.4}}, 0.4),
    ({"margin": {"max_pct_of_free": 0.25}}, 0.25),
    ({"netting": {"allow_opposing": True}}, None),
])
def test_the_four_live_slaves_resolve_as_intended(config, expected):
    """ibkr-paper-1, ibkr-etf-1, mt5-dwx-futures-1 and mt5-icm-1 as they
    stood when this was written. The Darwinex one is the case that was
    silently uncapped."""
    assert resolve_margin_cap(config) == expected
