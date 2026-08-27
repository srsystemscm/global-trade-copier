import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.contract_specs import ContractSpec


@dataclass
class SizingResult:
    size: float
    skipped: bool = False
    reason: Optional[str] = None


def resolve_sizing_config(slave_config: Dict[str, Any], target_symbol: str) -> Dict[str, Any]:
    """Sizing config can be shared across all of a slave's targets, or
    overridden per target symbol, e.g.:

        {"sizing": {"mode": "fixed_contracts", "contracts": 1}}
        {"sizing": {"MGC": {"mode": "pct_risk", ...}, "GLD": {"mode": "dollar_notional", ...}}}
    """
    sizing = slave_config.get("sizing", {})
    if target_symbol in sizing:
        return sizing[target_symbol]
    return sizing


def capital_base_amount(
    capital_base: str, balance: float, equity: float, fixed_offset: float, fixed_amount: float
) -> float:
    if capital_base == "balance":
        return balance
    if capital_base == "equity":
        return equity
    if capital_base == "balance_plus_fixed":
        return balance + fixed_offset
    if capital_base == "fixed_amount":
        return fixed_amount
    raise ValueError(f"unknown capital_base {capital_base!r}")


def compute_size(
    *,
    sizing_config: Dict[str, Any],
    master_lots: float,
    entry_price: float,
    stop_price: Optional[float],
    account_balance: float,
    account_equity: float,
    contract_spec: Optional[ContractSpec],
) -> SizingResult:
    """Computes the slave order size (contracts for futures, shares for
    equities/ETFs). Always floors to a whole unit and skips the trade if
    that's less than 1 -- never place a 0-size order.
    """
    mode = sizing_config.get("mode")

    if mode == "fixed_contracts":
        size = float(sizing_config["contracts"])

    elif mode == "lot_multiplier":
        size = master_lots * float(sizing_config.get("multiplier", 1.0))

    elif mode == "dollar_notional":
        notional = float(sizing_config["notional"])
        if entry_price <= 0:
            return SizingResult(size=0, skipped=True, reason="entry price is zero")
        unit_value = entry_price * contract_spec.point_value if contract_spec else entry_price
        size = notional / unit_value

    elif mode == "pct_risk":
        if stop_price is None:
            return SizingResult(size=0, skipped=True, reason="pct_risk requires a stop price")
        capital_base = capital_base_amount(
            sizing_config.get("capital_base", "balance"),
            account_balance,
            account_equity,
            float(sizing_config.get("fixed_offset", 0.0)),
            float(sizing_config.get("fixed_amount", 0.0)),
        )
        risk_amount = capital_base * float(sizing_config["risk_pct"])
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return SizingResult(size=0, skipped=True, reason="stop distance is zero")

        if contract_spec:
            ticks = stop_distance / contract_spec.tick_size
            risk_per_unit = ticks * contract_spec.tick_value
        else:
            risk_per_unit = stop_distance  # $ risk per share

        if risk_per_unit <= 0:
            return SizingResult(size=0, skipped=True, reason="risk per unit is zero")
        size = risk_amount / risk_per_unit

    else:
        raise ValueError(f"unknown sizing mode {mode!r}")

    min_size = contract_spec.min_size if contract_spec else 1.0
    # round before flooring: tick sizes like 0.10 aren't exact in binary
    # floating point, so a "genuinely whole" unit count can land a hair
    # below the boundary (e.g. 104.99999999999999) and floor one unit too
    # many -- 6dp is far finer than any real contract/share count needs.
    units = round(size / min_size, 6)
    size = math.floor(units) * min_size
    if size < min_size:
        return SizingResult(size=0, skipped=True, reason=f"computed size {size} < min size {min_size}, skipping")
    return SizingResult(size=size)
