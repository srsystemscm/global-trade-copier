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
    assert result.size == 20  # floor(5000 / 238.5)


def test_pct_risk_futures_uses_tick_value():
    # stop_distance=10.5 -> 105 ticks @ $1/tick = $105 risk/contract
    # risk_amount = balance(50000) * 1% = $500 -> floor(500/105) = 4
    result = compute_size(
        sizing_config={"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance"},
        master_lots=0.1, entry_price=2385.0, stop_price=2374.5,
        account_balance=50000, account_equity=52000, contract_spec=MGC,
    )
    assert result.size == 4


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


def test_skips_when_computed_size_below_one():
    result = compute_size(
        sizing_config={"mode": "fixed_contracts", "contracts": 0.5},
        master_lots=0.1, entry_price=2385.0, stop_price=None,
        account_balance=10000, account_equity=10000, contract_spec=MGC,
    )
    assert result.skipped
    assert result.size == 0


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
