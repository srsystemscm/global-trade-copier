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


# --- full-size contracts ---------------------------------------------------

def test_gc_is_full_size_comex_gold():
    """GC is 100 troy oz on COMEX -- 10x MGC. Routing it to CME (the old
    hardcoded default) would fail qualifyContracts outright."""
    spec = get_contract_spec("GC")
    assert spec is not None
    assert spec.exchange == "COMEX"
    assert spec.point_value == 100.0
    assert spec.tick_size == 0.10
    assert spec.tick_value == 10.00


def test_gc_and_mgc_differ_by_ten_times():
    assert get_contract_spec("GC").point_value == 10 * get_contract_spec("MGC").point_value


# --- liquid month cycles ---------------------------------------------------
# Strategies on one IBKR account are separated by contract month, so routing
# must land on months that actually trade. Measured on COMEX gold via the
# Gateway: GCZ6 29,179 contracts/day, GCG7 2,098, GCJ7 174 -- while the serial
# months between them (GCX6 7, GCF7 23, GCH7 20) are effectively dead.

def test_gold_cycle_is_feb_apr_jun_aug_dec():
    for sym in ("GC", "MGC"):
        assert get_contract_spec(sym).liquid_months == (2, 4, 6, 8, 12), sym


def test_equity_index_micros_are_quarterly():
    for sym in ("MES", "MNQ", "MYM", "M2K"):
        assert get_contract_spec(sym).liquid_months == (3, 6, 9, 12), sym


def test_crude_has_no_restriction():
    """Empty means every month is liquid -- CL trades all twelve."""
    assert get_contract_spec("MCL").liquid_months == ()


def test_serial_months_are_excluded_for_gold():
    cycle = get_contract_spec("GC").liquid_months
    for serial in (1, 3, 5, 7, 9, 10, 11):   # Jan Mar May Jul Sep Oct Nov
        assert serial not in cycle


def test_override_can_supply_a_cycle():
    spec = get_contract_spec("GC", overrides={
        "GC": {"tick_size": 0.1, "tick_value": 10.0, "point_value": 100.0,
               "exchange": "COMEX", "liquid_months": [6, 12]}})
    assert tuple(spec.liquid_months) == (6, 12)
