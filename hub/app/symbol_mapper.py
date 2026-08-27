from typing import Any, Dict, List


def map_symbol(slave_config: Dict[str, Any], master_symbol: str) -> List[str]:
    """Per-slave symbol_map config, e.g.:

        {"symbol_map": {"XAUUSD": ["MGC", "GLD"], "US30": "MYM"}}

    One master symbol can fan out to multiple slave targets on the same
    slave account (a single XAUUSD signal opening both a futures and an
    ETF position). Returns an empty list if the slave has no mapping for
    this master symbol.
    """
    targets = slave_config.get("symbol_map", {}).get(master_symbol)
    if not targets:
        return []
    if isinstance(targets, str):
        return [targets]
    return list(targets)
