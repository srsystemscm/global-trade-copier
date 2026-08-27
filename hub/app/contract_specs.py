from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ContractSpec:
    symbol: str
    tick_size: float
    tick_value: float
    point_value: float
    min_size: float = 1.0


# Indicative CME micro-futures specs (the default preset per the project's
# design decisions). Exchange specs can change -- verify against CME's
# published contract specs before trading real size, and use `overrides` in
# a slave's config to correct/extend these.
MICRO_FUTURES_SPECS: Dict[str, ContractSpec] = {
    "MGC": ContractSpec("MGC", tick_size=0.10, tick_value=1.00, point_value=10.0),   # Micro Gold
    "MES": ContractSpec("MES", tick_size=0.25, tick_value=1.25, point_value=5.0),    # Micro E-mini S&P 500
    "MNQ": ContractSpec("MNQ", tick_size=0.25, tick_value=0.50, point_value=2.0),    # Micro E-mini Nasdaq-100
    "MYM": ContractSpec("MYM", tick_size=1.00, tick_value=0.50, point_value=0.50),   # Micro E-mini Dow
    "M2K": ContractSpec("M2K", tick_size=0.10, tick_value=0.50, point_value=5.0),    # Micro E-mini Russell 2000
    "MCL": ContractSpec("MCL", tick_size=0.01, tick_value=1.00, point_value=100.0),  # Micro WTI Crude Oil
}


def get_contract_spec(symbol: str, overrides: Optional[Dict[str, dict]] = None) -> Optional[ContractSpec]:
    """Returns the futures contract spec for `symbol`, or None if it's not a
    known future (i.e. it should be treated as an equity/ETF instead).
    """
    if overrides and symbol in overrides:
        o = overrides[symbol]
        return ContractSpec(
            symbol,
            tick_size=o["tick_size"],
            tick_value=o["tick_value"],
            point_value=o["point_value"],
            min_size=o.get("min_size", 1.0),
        )
    return MICRO_FUTURES_SPECS.get(symbol)
