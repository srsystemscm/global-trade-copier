import pytest
from app.contract_specs import get_contract_spec
from app.sizing import capital_base_amount, compute_size, resolve_sizing_config

MGC = get_contract_spec("MGC")


def test_fixed_contracts():
    result = compute_size(
        sizing_config={"mode": "fixed_contracts", "contracts": 3},
        master_lots=0.1, entry_price=2385.0, stop_price=None,
        account_balance=10000, account_equity=10000, contract_spec=MGC,
    )
    assert not result.skipped
    assert result.size == 3


def test_lot_multiplier():
    result = compute_size(
        sizing_config={"mode": "lot_multiplier", "multiplier": 10},
        master_lots=0.5, entry_price=2385.0, stop_price=None,
        account_balance=10000, account_equity=10000, contract_spec=MGC,
    )
    assert result.size == 5


def test_dollar_notional_future_uses_point_value():
    # notional / (price * point_value) = 47700 / (2385 * 10) = 2 contracts
    result = compute_size(
        sizing_config={"mode": "dollar_notional", "notional": 47700},
        master_lots=0.1, entry_price=2385.0, stop_price=None,
        account_balance=10000, account_equity=10000, contract_spec=MGC,
    )
    assert result.size == 2


def test_dollar_notional_equity_is_share_count():
    result = compute_size(
        sizing_config={"mode": "dollar_notional", "notional": 5000},
        master_lots=0.1, entry_price=238.5, stop_price=None,
        account_balance=10000, account_equity=10000, contract_spec=None,
    )
    assert result.size == 21  # round(5000 / 238.5 = 20.96), default rounding is nearest


def test_pct_risk_futures_uses_tick_value():
    # stop_distance=10.5 -> 105 ticks @ $1/tick = $105 risk/contract
    # risk_amount = balance(50000) * 1% = $500 -> floor(500/105) = 4
    result = compute_size(
        sizing_config={"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance"},
        master_lots=0.1, entry_price=2385.0, stop_price=2374.5,
        account_balance=50000, account_equity=52000, contract_spec=MGC,
    )
    assert result.size == 5  # 4.5+ rounds up under the default


def test_pct_risk_capital_base_equity_vs_balance():
    kwargs = dict(
        sizing_config={"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "equity"},
        master_lots=0.1, entry_price=2385.0, stop_price=2374.5, contract_spec=MGC,
    )
    # risk_amount = equity(10500) * 1% = $105 -> exactly 1 contract's worth of risk
    result = compute_size(account_balance=50000, account_equity=10500, **kwargs)
    assert result.size == 1


def test_pct_risk_missing_stop_skips():
    result = compute_size(
        sizing_config={"mode": "pct_risk", "risk_pct": 0.01},
        master_lots=0.1, entry_price=2385.0, stop_price=None,
        account_balance=50000, account_equity=50000, contract_spec=MGC,
    )
    assert result.skipped
    assert result.size == 0


def test_below_one_unit_returns_one_under_default_rounding():
    """A master lot rarely maps to a whole contract count. The configured
    practice is >.5 up, <.5 down, never below one -- so a sub-1 result is
    taken at one unit rather than skipped.

    This is the sharp edge of that rule: the trade goes on at more size than
    the model asked for. max_risk_pct is the guard against it.
    """
    result = compute_size(
        sizing_config={"mode": "fixed_contracts", "contracts": 0.5},
        master_lots=0.1, entry_price=2385.0, stop_price=None,
        account_balance=10000, account_equity=10000, contract_spec=MGC,
    )
    assert not result.skipped
    assert result.size == 1


def test_capital_base_amount_variants():
    assert capital_base_amount("balance", balance=100, equity=150, fixed_offset=20, fixed_amount=999) == 100
    assert capital_base_amount("equity", balance=100, equity=150, fixed_offset=20, fixed_amount=999) == 150
    assert capital_base_amount("balance_plus_fixed", balance=100, equity=150, fixed_offset=20, fixed_amount=999) == 120
    assert capital_base_amount("fixed_amount", balance=100, equity=150, fixed_offset=20, fixed_amount=999) == 999


def test_resolve_sizing_config_shared_vs_per_symbol_override():
    cfg = {
        "sizing": {
            "mode": "fixed_contracts",
            "contracts": 1,
            "MGC": {"mode": "dollar_notional", "notional": 1000},
        }
    }
    assert resolve_sizing_config(cfg, "GLD")["mode"] == "fixed_contracts"
    assert resolve_sizing_config(cfg, "MGC")["mode"] == "dollar_notional"


# --- GC sizing, rounding modes, and the risk ceiling -----------------------
# GC is 100 troy oz, so a $5k notional buys 0.0116 contracts. Under the
# default "nearest_min1" rounding that becomes ONE contract -- about 86x the
# intended exposure, roughly $430k notional. That is the cost of the
# min-one-contract rule, and why max_risk_pct exists.

from app.contract_specs import get_contract_spec
from app.sizing import compute_size

GC = get_contract_spec("GC")


def _gc(sizing, stop_price=None):
    return compute_size(
        sizing_config=sizing, master_lots=0.10, entry_price=4300.0,
        stop_price=stop_price, contract_spec=GC,
        account_balance=1_000_000.0, account_equity=1_000_000.0,
    )


def test_tiny_notional_on_gc_becomes_one_contract_by_default():
    r = _gc({"mode": "dollar_notional", "notional": 5000})
    assert not r.skipped
    assert r.size == 1


def test_floor_rounding_skips_the_undersized_trade_instead():
    r = _gc({"mode": "dollar_notional", "notional": 5000, "rounding": "floor"})
    assert r.skipped is True
    assert r.size == 0


def test_fixed_contracts_on_gc_places_one():
    r = _gc({"mode": "fixed_contracts", "contracts": 1})
    assert not r.skipped and r.size == 1


def test_rounds_half_up():
    """>.5 up, <.5 down -- the configured practice."""
    assert _gc({"mode": "fixed_contracts", "contracts": 2.6}).size == 3
    assert _gc({"mode": "fixed_contracts", "contracts": 2.4}).size == 2
    assert _gc({"mode": "fixed_contracts", "contracts": 2.5}).size == 3


def test_unknown_rounding_mode_is_rejected():
    with pytest.raises(ValueError, match="rounding"):
        _gc({"mode": "fixed_contracts", "contracts": 1, "rounding": "nearest"})


# --- max_risk_pct: when even one contract is too much ----------------------

def test_risk_ceiling_skips_when_one_contract_is_too_big():
    """A 60-point stop on GC risks $6,000 (60 / 0.10 tick * $10). Against a
    $1M base that's 0.6%, over a 0.5% ceiling -- so the trade is skipped
    rather than taken at a size the account shouldn't carry. This is the
    volatility case: stops widen, the smallest tradeable size stops fitting.
    """
    r = _gc({"mode": "fixed_contracts", "contracts": 1, "max_risk_pct": 0.005},
            stop_price=4240.0)
    assert r.skipped is True
    assert "ceiling" in r.reason


def test_risk_ceiling_allows_a_trade_that_fits():
    r = _gc({"mode": "fixed_contracts", "contracts": 1, "max_risk_pct": 0.01},
            stop_price=4240.0)
    assert not r.skipped and r.size == 1


def test_risk_ceiling_needs_a_stop_to_measure_against():
    r = _gc({"mode": "fixed_contracts", "contracts": 1, "max_risk_pct": 0.005})
    assert r.skipped is True
    assert "requires a stop price" in r.reason


def test_no_ceiling_configured_means_no_check():
    r = _gc({"mode": "fixed_contracts", "contracts": 1}, stop_price=4240.0)
    assert not r.skipped and r.size == 1
