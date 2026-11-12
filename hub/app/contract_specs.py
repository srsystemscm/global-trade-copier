from dataclasses import dataclass
from typing import Dict, Optional, Tuple


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
    # Months (1-12) that actually carry volume. Serial months exist for most
    # of these roots but are near-dead: measured on COMEX gold, cycle months
    # ran 100-29,000 contracts/day while serials ran 0-23. Routing a strategy
    # to "the next expiry" instead of "the next *liquid* expiry" would park it
    # in one of those. Empty means every month is liquid (e.g. crude).
    liquid_months: Tuple[int, ...] = ()


# Indicative CME-complex futures specs: the micro preset (the project's
# default per its design decisions) plus the full-size contracts that have
# been needed so far. Exchange specs can change -- verify against CME's
# published contract specs before trading real size, and use `overrides` in
# a slave's config to correct/extend these.
MICRO_FUTURES_SPECS: Dict[str, ContractSpec] = {
    "MGC": ContractSpec("MGC", tick_size=0.10, tick_value=1.00, point_value=10.0, exchange="COMEX", liquid_months=(2, 4, 6, 8, 12)),  # Micro Gold
    "MES": ContractSpec("MES", tick_size=0.25, tick_value=1.25, point_value=5.0, exchange="CME", liquid_months=(3, 6, 9, 12)),     # Micro E-mini S&P 500
    "MNQ": ContractSpec("MNQ", tick_size=0.25, tick_value=0.50, point_value=2.0, exchange="CME", liquid_months=(3, 6, 9, 12)),     # Micro E-mini Nasdaq-100
    "MYM": ContractSpec("MYM", tick_size=1.00, tick_value=0.50, point_value=0.50, exchange="CBOT", liquid_months=(3, 6, 9, 12)),   # Micro E-mini Dow
    "M2K": ContractSpec("M2K", tick_size=0.10, tick_value=0.50, point_value=5.0, exchange="CME", liquid_months=(3, 6, 9, 12)),     # Micro E-mini Russell 2000
    "MCL": ContractSpec("MCL", tick_size=0.01, tick_value=1.00, point_value=100.0, exchange="NYMEX"), # Micro WTI Crude Oil
    # Micro Ether: 0.10 ether per contract, 0.50 index-point tick worth
    # 0.10 x 0.50 = $0.05, so a 1.00 move in ETH is $0.10. Verified against
    # CME's own contract spec -- several third-party fact cards quote $0.10
    # or $0.005 a tick and are wrong. Calendar spreads and BTIC tick finer
    # (0.10 index points, $0.01), which this does not model: the copier only
    # ever sends outrights.
    #
    # liquid_months is empty rather than a cycle. Crypto futures list
    # consecutive months and the front one carries the volume, unlike gold
    # where the serial months are near-dead and routing to "the next expiry"
    # would park a strategy in one.
    "MET": ContractSpec("MET", tick_size=0.50, tick_value=0.05, point_value=0.10, exchange="CME"),
    # Micro Bitcoin: 0.10 BTC, $5-per-bitcoin tick worth $0.50 a contract,
    # so a $1 move in BTC is $0.10. Spreads tick at $1/bitcoin ($0.10); not
    # modelled, the copier only sends outrights.
    "MBT": ContractSpec("MBT", tick_size=5.00, tick_value=0.50, point_value=0.10, exchange="CME"),
    # Micro Henry Hub Natural Gas: 1,000 MMBtu -- a tenth of full-size NG,
    # not the quarter it looks like next to the other micros. 0.001 tick on
    # 1,000 MMBtu is $1.00, so a 1.00 move is $1,000. NYMEX, like MCL:
    # IBKR's qualifyContracts() fails outright on the wrong exchange.
    # Real contract, but NOT CARRIED BY IBKR. Probed on 2026-11-12:
    # reqContractDetails for symbol MNG returns nothing on NYMEX or on any
    # exchange, while Schwab does trade it. So availability is per broker,
    # not per instrument: this spec is valid and routable on a Schwab slave,
    # and must not go in an IBKR route, where it would resolve to nothing at
    # the exact moment a tier list fell through to it. IBKR's small gas
    # contract is QG below.
    "MNG": ContractSpec("MNG", tick_size=0.001, tick_value=1.00, point_value=1000.0, exchange="NYMEX"),

    # The small natural gas contract IBKR actually carries: NYMEX MINY, a
    # QUARTER of NG at 2,500 MMBtu rather than the tenth MNG would have
    # been. Verified from IBKR's own contract details -- minTick 0.005 on a
    # 2500 multiplier is $12.50 a tick.
    "QG":  ContractSpec("QG",  tick_size=0.005, tick_value=12.50, point_value=2500.0, exchange="NYMEX"),

    # Full-size. GC is 100 troy oz -- 10x MGC, so roughly $430k notional per
    # contract at $4,300 gold. Size it deliberately; dollar_notional will
    # floor to zero (and skip the trade) for any sensible notional.
    # Full-size Henry Hub natural gas: 10,000 MMBtu, ten times MNG. The
    # reason to reach for it is cost -- the commission is per contract, so
    # one NG is far cheaper than ten MNG for the same exposure. Whether the
    # account can carry one contract's risk is what decides it, which is
    # what max_risk_pct answers.
    "NG":  ContractSpec("NG",  tick_size=0.001, tick_value=10.00, point_value=10000.0, exchange="NYMEX"),

    "GC":  ContractSpec("GC",  tick_size=0.10, tick_value=10.00, point_value=100.0, exchange="COMEX", liquid_months=(2, 4, 6, 8, 12)), # Gold
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
            liquid_months=tuple(o.get("liquid_months", ())),
        )
    return MICRO_FUTURES_SPECS.get(symbol)
