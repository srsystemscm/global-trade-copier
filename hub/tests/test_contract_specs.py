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
