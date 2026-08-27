from app.symbol_mapper import map_symbol


def test_fanout_to_multiple_targets():
    cfg = {"symbol_map": {"XAUUSD": ["MGC", "GLD"]}}
    assert map_symbol(cfg, "XAUUSD") == ["MGC", "GLD"]


def test_single_string_target():
    cfg = {"symbol_map": {"US30": "MYM"}}
    assert map_symbol(cfg, "US30") == ["MYM"]


def test_no_mapping_for_symbol():
    cfg = {"symbol_map": {}}
    assert map_symbol(cfg, "XAUUSD") == []


def test_missing_symbol_map_key_entirely():
    assert map_symbol({}, "XAUUSD") == []
