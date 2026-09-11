from app.contract_specs import MICRO_FUTURES_SPECS, get_contract_spec


def test_known_future():
    spec = get_contract_spec("MGC")
    assert spec is not None
    assert spec.tick_size == 0.10
    assert spec.tick_value == 1.00


def test_unknown_symbol_is_not_a_future():
    assert get_contract_spec("GLD") is None


def test_override_takes_precedence():
    spec = get_contract_spec(
        "XYZ", overrides={"XYZ": {"tick_size": 0.5, "tick_value": 2.0, "point_value": 4.0}}
    )
    assert spec.tick_size == 0.5
    assert spec.min_size == 1.0


def test_all_micro_presets_present():
    for sym in ["MGC", "MES", "MNQ", "MYM", "M2K", "MCL"]:
        assert sym in MICRO_FUTURES_SPECS


# --- IBKR listing exchange -------------------------------------------------
# The CME Group micro complex spans four exchanges. IBKR's qualifyContracts()
# rejects a contract with the wrong one outright, so this is part of the
# contract's identity, not a hint. Schwab never needed it (it routes by
# account), which is why a hardcoded "CME" survived until an IBKR slave was
# actually pointed at these symbols.

import pytest

from app.contract_specs import get_contract_spec


@pytest.mark.parametrize(
    "symbol,exchange",
    [
        ("MGC", "COMEX"),   # Micro Gold -- the default XAUUSD mapping
        ("MCL", "NYMEX"),   # Micro WTI Crude
        ("MYM", "CBOT"),    # Micro E-mini Dow
        ("MES", "CME"),
        ("MNQ", "CME"),
        ("M2K", "CME"),
    ],
)
def test_micro_futures_listing_exchange(symbol, exchange):
    spec = get_contract_spec(symbol)
    assert spec is not None
    assert spec.exchange == exchange


def test_exchange_can_be_overridden_per_slave():
    spec = get_contract_spec(
        "MGC",
        overrides={"MGC": {"tick_size": 0.1, "tick_value": 1.0, "point_value": 10.0, "exchange": "QBALGO"}},
    )
    assert spec.exchange == "QBALGO"


def test_override_without_exchange_falls_back_to_cme():
    spec = get_contract_spec(
        "MES", overrides={"MES": {"tick_size": 0.25, "tick_value": 1.25, "point_value": 5.0}}
    )
    assert spec.exchange == "CME"


def test_equities_have_no_spec_so_they_route_as_stock():
    """GLD (the ETF mapping we use for the first IBKR test) must NOT resolve
    to a futures spec -- that's what sends it down the Stock/SMART path and
    sidesteps the ContFuture order-placement gap entirely."""
    assert get_contract_spec("GLD") is None
