"""Route sizing layers over the slave's default rather than replacing it.

Replacing meant a slave-wide guard like max_risk_pct was silently dropped by
any route that spelled out sizing at all. With MCL, MNG, MBT and MET about
to be added, that was four chances to forget the risk gate -- and a
forgotten gate looks exactly like a configured one from the outside.
"""
import pytest

from app.contract_specs import get_contract_spec
from app.sizing import compute_size, merge_sizing, resolve_sizing_config

SLAVE_DEFAULT = {"mode": "fixed_contracts", "contracts": 1,
                 "rounding": "nearest_min1", "max_risk_pct": 0.03}


# ---------------- the merge ----------------------------------------------

def test_a_route_inherits_the_slave_risk_gate():
    """The whole point: a new futures route cannot silently run ungated."""
    merged = merge_sizing(SLAVE_DEFAULT, {"contracts": 2})
    assert merged["max_risk_pct"] == 0.03
    assert merged["rounding"] == "nearest_min1"
    assert merged["contracts"] == 2


def test_a_route_can_still_override_the_gate():
    merged = merge_sizing(SLAVE_DEFAULT, {"max_risk_pct": 0.01})
    assert merged["max_risk_pct"] == 0.01


def test_switching_the_gate_off_has_to_be_explicit():
    """An absent key means inherit, so silence must not mean 'no ceiling'.
    Turning it off is a deliberate null."""
    assert merge_sizing(SLAVE_DEFAULT, {"contracts": 2})["max_risk_pct"] == 0.03
    assert merge_sizing(SLAVE_DEFAULT, {"max_risk_pct": None})["max_risk_pct"] is None


def test_no_route_sizing_leaves_the_default_alone():
    assert merge_sizing(SLAVE_DEFAULT, None) == SLAVE_DEFAULT
    assert merge_sizing(SLAVE_DEFAULT, {}) == SLAVE_DEFAULT


def test_the_merge_does_not_mutate_the_slave_config():
    """The slave's dict is shared across every route on it."""
    base = dict(SLAVE_DEFAULT)
    merge_sizing(base, {"contracts": 9, "max_risk_pct": 0.5})
    assert base == SLAVE_DEFAULT


def test_changing_mode_drops_the_old_mode_size_key():
    """A leftover `contracts` under a dollar_notional config is confusing in
    a log and one refactor away from being read."""
    merged = merge_sizing(SLAVE_DEFAULT,
                          {"mode": "dollar_notional", "notional": 5000})
    assert merged["mode"] == "dollar_notional"
    assert merged["notional"] == 5000
    assert "contracts" not in merged
    # but the guard and the rounding rule survive the mode change
    assert merged["max_risk_pct"] == 0.03


def test_shared_keys_survive_a_mode_change():
    """capital_base is read by the max_risk_pct ceiling too, whatever the
    mode, so it must not be treated as belonging to pct_risk."""
    base = {"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "equity",
            "max_risk_pct": 0.03}
    merged = merge_sizing(base, {"mode": "fixed_contracts", "contracts": 1})
    assert merged["capital_base"] == "equity"
    assert "risk_pct" not in merged


def test_the_same_mode_keeps_its_key():
    merged = merge_sizing(SLAVE_DEFAULT, {"mode": "fixed_contracts"})
    assert merged["contracts"] == 1


# ---------------- per-target slave sizing --------------------------------

def test_per_target_sizing_still_wins():
    cfg = {"sizing": {"MGC": {"mode": "fixed_contracts", "contracts": 2}}}
    assert resolve_sizing_config(cfg, "MGC")["contracts"] == 2


def test_the_fallback_does_not_leak_other_targets():
    """In the per-target form the siblings are other symbols' configs, not
    settings -- merging them in as if they were is nonsense."""
    cfg = {"sizing": {"MGC": {"mode": "fixed_contracts", "contracts": 2},
                      "max_risk_pct": 0.03}}
    fallback = resolve_sizing_config(cfg, "MCL")
    assert fallback == {"max_risk_pct": 0.03}
    assert "MGC" not in fallback


# ---------------- a half-specified route says so -------------------------

@pytest.mark.parametrize("override, missing", [
    ({"mode": "fixed_contracts"}, "contracts"),
    ({"mode": "dollar_notional"}, "notional"),
    ({"mode": "pct_risk"}, "risk_pct"),
])
def test_a_mode_switch_without_a_size_is_refused_clearly(override, missing):
    """Previously a KeyError, which surfaces as an engine traceback rather
    than as a refused trade naming the route that is wrong."""
    base = {"mode": "lot_multiplier", "multiplier": 1.0, "rounding": "floor"}
    r = compute_size(
        sizing_config=merge_sizing(base, override), master_lots=1.0,
        entry_price=100.0, stop_price=90.0, account_balance=100000,
        account_equity=100000, contract_spec=None,
    )
    assert r.skipped
    assert missing in r.reason


# ---------------- end to end ---------------------------------------------

def test_an_inherited_gate_actually_blocks_the_trade():
    """Not just present in the dict -- in force."""
    spec = get_contract_spec("GC")      # $100/point, so a wide stop is costly
    merged = merge_sizing(SLAVE_DEFAULT, {"contracts": 1})
    r = compute_size(
        sizing_config=merged, master_lots=1.0, entry_price=3985.0,
        stop_price=3868.0, account_balance=100000, account_equity=100000,
        contract_spec=spec,
    )
    assert r.skipped
    assert "3.00%" in r.reason

    # and the same route on micro gold passes
    r2 = compute_size(
        sizing_config=merged, master_lots=1.0, entry_price=3985.0,
        stop_price=3868.0, account_balance=100000, account_equity=100000,
        contract_spec=get_contract_spec("MGC"),
    )
    assert not r2.skipped
    assert r2.size == 1
