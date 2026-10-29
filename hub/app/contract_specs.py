from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class ContractSpec:
    symbol: str
    tick_size: float
    tick_value: float
    point_value: float
    min_size: float = 1.0
    # IBKR listing exchange. The CME Group "micro" complex is spread across
    # four exchanges, and IBKR's qualifyContracts() fails outright on the
    # wrong one -- it is not a hint, it is part of the contract's identity.
    # Schwab doesn't need this (it routes by account), so it only shows up
    # once you point an IBKR slave at these symbols.
    exchange: str = "CME"


# Indicative CME-complex futures specs: the micro preset (the project's
# default per its design decisions) plus the full-size contracts that have
# been needed so far. Exchange specs can change -- verify against CME's
# published contract specs before trading real size, and use `overrides` in
# a slave's config to correct/extend these.
MICRO_FUTURES_SPECS: Dict[str, ContractSpec] = {
    "MGC": ContractSpec("MGC", tick_size=0.10, tick_value=1.00, point_value=10.0, exchange="COMEX"),  # Micro Gold
    "MES": ContractSpec("MES", tick_size=0.25, tick_value=1.25, point_value=5.0, exchange="CME"),     # Micro E-mini S&P 500
    "MNQ": ContractSpec("MNQ", tick_size=0.25, tick_value=0.50, point_value=2.0, exchange="CME"),     # Micro E-mini Nasdaq-100
    "MYM": ContractSpec("MYM", tick_size=1.00, tick_value=0.50, point_value=0.50, exchange="CBOT"),   # Micro E-mini Dow
    "M2K": ContractSpec("M2K", tick_size=0.10, tick_value=0.50, point_value=5.0, exchange="CME"),     # Micro E-mini Russell 2000
    "MCL": ContractSpec("MCL", tick_size=0.01, tick_value=1.00, point_value=100.0, exchange="NYMEX"), # Micro WTI Crude Oil

    # Full-size. GC is 100 troy oz -- 10x MGC, so roughly $430k notional per
    # contract at $4,300 gold. Size it deliberately; dollar_notional will
    # floor to zero (and skip the trade) for any sensible notional.
    "GC":  ContractSpec("GC",  tick_size=0.10, tick_value=10.00, point_value=100.0, exchange="COMEX"), # Gold
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
            exchange=o.get("exchange", "CME"),
        )
    return MICRO_FUTURES_SPECS.get(symbol)
