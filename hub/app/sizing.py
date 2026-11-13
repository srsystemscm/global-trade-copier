import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from app.contract_specs import ContractSpec


@dataclass
class SizingResult:
    size: float
    skipped: bool = False
    reason: Optional[str] = None


# Keys that belong to one sizing mode alone. Dropped when a route switches
# mode, so a merged config never carries a size instruction from a mode that
# is no longer in force. Deliberately narrow: capital_base, fixed_offset and
# fixed_amount are NOT here, because the max_risk_pct ceiling reads them too
# whatever the mode is.
_MODE_ONLY_KEYS = {
    "fixed_contracts": {"contracts"},
    "lot_multiplier": {"multiplier"},
    "dollar_notional": {"notional"},
    "pct_risk": {"risk_pct"},
}


def merge_sizing(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Layer a route's sizing over the slave's default.

    The slave's sizing is the default and the route is the exception, so a
    slave-wide guard such as max_risk_pct stays in force unless the route
    deliberately changes it. To switch it off, a route sets it to null
    explicitly -- an absent key means "inherit", which is the whole point,
    so silence cannot mean "no ceiling".

    When the route changes mode, the previous mode's own size key is
    dropped: a leftover `contracts` sitting under a dollar_notional config
    is confusing in a log and one refactor away from being read.
    """
    if not override:
        return dict(base)
    merged = dict(base)
    base_mode = base.get("mode")
    new_mode = override.get("mode", base_mode)
    if new_mode != base_mode:
        for key in _MODE_ONLY_KEYS.get(base_mode, ()):  # type: ignore[arg-type]
            merged.pop(key, None)
    merged.update(override)
    return merged


def resolve_sizing_config(slave_config: Dict[str, Any], target_symbol: str) -> Dict[str, Any]:
    """Sizing config can be shared across all of a slave's targets, or
    overridden per target symbol, e.g.:

        {"sizing": {"mode": "fixed_contracts", "contracts": 1}}
        {"sizing": {"MGC": {"mode": "pct_risk", ...}, "GLD": {"mode": "dollar_notional", ...}}}
    """
    sizing = slave_config.get("sizing", {})
    if target_symbol in sizing:
        return sizing[target_symbol]
    # Falling back to the shared block. Strip any per-target entries out of
    # it: in the per-target form those are sibling symbols' configs, and
    # merging them in as if they were settings is nonsense.
    return {k: v for k, v in sizing.items() if not isinstance(v, dict)}


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


def resolve_balance_adjustment(slave_config: Dict[str, Any]) -> float:
    """A standing correction to what the broker reports, portfolio-wide.

    Signed, in account currency. The case it exists for: a 250k account
    pays 50k in tax, and its strategies are meant to keep risking as if it
    were still 250k, because the withdrawal is not a drawdown.

    Deliberately at the slave's top level rather than inside a sizing block.
    It is a fact about the account, not about one strategy's sizing mode, so
    a route must not be able to quietly forget to carry it -- and it applies
    whatever capital_base is in force, including "equity".

    Risk is measured against the adjusted figure. Margin is not: see
    AutonomousCopyEngine._risk_account.
    """
    return float(slave_config.get("balance_adjustment", 0.0) or 0.0)


def resolve_risk_scale(slave_config: Dict[str, Any], route_scale: Optional[float]) -> float:
    """How much of the configured risk this leg carries.

    The route (strategy) wins over the slave, so a slave can run at half
    size overall while one strategy on it runs at a quarter. 1.0 means
    unchanged, which is the default everywhere -- an absent setting must
    never quietly resize anything.
    """
    if route_scale is not None:
        return float(route_scale)
    return float(slave_config.get("risk_scale", 1.0))


def compute_size(
    *,
    sizing_config: Dict[str, Any],
    master_lots: float,
    entry_price: float,
    stop_price: Optional[float],
    account_balance: float,
    account_equity: float,
    contract_spec: Optional[ContractSpec],
    risk_scale: float = 1.0,
) -> SizingResult:
    """Computes the slave order size (contracts for futures, shares for
    equities/ETFs).

    Rounding to whole units is controlled by `sizing_config["rounding"]`:

    - "nearest_min1" (default) -- round half up, never below one unit.
    - "floor"                  -- round down, skip the trade below one unit.

    Never places a zero-size order either way.

    `sizing_config["max_risk_pct"]`, if set, is a ceiling checked against the
    *final* size: when even the minimum tradeable size risks more than that
    fraction of the capital base, the trade is skipped rather than taken at
    the wrong size. Requires a stop price to measure risk against.

    `risk_scale` scales the size the mode asked for: 1.0 carries the
    configured risk unchanged, 0.5 halves the position. It is applied before
    rounding, so the rounding and minimum-size rules still have the last
    word, and before the max_risk_pct ceiling, so scaling down can bring an
    otherwise-refused trade under it. Since stops come from ATR multiples
    and never from size, dollar risk scales linearly with it.
    """
    mode = sizing_config.get("mode")

    if mode == "fixed_contracts":
        if "contracts" not in sizing_config:
            return SizingResult(
                size=0, skipped=True,
                reason="fixed_contracts sizing has no `contracts` -- a route "
                       "switched mode without saying how big the position is",
            )
        size = float(sizing_config["contracts"])

    elif mode == "lot_multiplier":
        size = master_lots * float(sizing_config.get("multiplier", 1.0))

    elif mode == "dollar_notional":
        if "notional" not in sizing_config:
            return SizingResult(
                size=0, skipped=True,
                reason="dollar_notional sizing has no `notional` -- a route "
                       "switched mode without saying how big the position is",
            )
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
        if "risk_pct" not in sizing_config:
            return SizingResult(
                size=0, skipped=True,
                reason="pct_risk sizing has no `risk_pct` -- a route switched "
                       "mode without saying how much to risk",
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

    # Applied here, after the mode has decided a size and before any
    # rounding, so that a scaled size is still subject to the broker's
    # minimum and to max_risk_pct below.
    if risk_scale <= 0:
        return SizingResult(
            size=0, skipped=True,
            reason=f"risk_scale is {risk_scale}, which is not a tradeable fraction "
                   f"-- refusing rather than sending a zero or inverted size",
        )
    size = size * risk_scale

    min_size = contract_spec.min_size if contract_spec else 1.0
    # round before the rounding rule: tick sizes like 0.10 aren't exact in
    # binary floating point, so a "genuinely whole" unit count can land a
    # hair below the boundary (e.g. 104.99999999999999) and lose a unit --
    # 6dp is far finer than any real contract/share count needs.
    units = round(size / min_size, 6)

    if units <= 0:
        return SizingResult(size=0, skipped=True, reason=f"computed size {size} is not positive")

    rounding = sizing_config.get("rounding", "nearest_min1")
    if rounding == "floor":
        whole = math.floor(units)
        if whole < 1:
            return SizingResult(
                size=0, skipped=True, reason=f"computed size {size} < min size {min_size}, skipping"
            )
    elif rounding == "nearest_min1":
        # Round half up, floor of one. A master lot size rarely maps to a
        # whole number of futures contracts, and the practice this mirrors
        # is: >.5 round up, <.5 round down, but never below one contract.
        #
        # The floor is the sharp edge. A model asking for 0.3 contracts gets
        # 1 -- more than three times the intended size, and on a contract
        # like GC (100oz, ~$430k notional) that is a real risk difference,
        # not a rounding detail. Use "floor" to skip undersized trades
        # instead.
        whole = max(math.floor(units + 0.5), 1)
    else:
        raise ValueError(f"unknown sizing rounding {rounding!r} (expected 'nearest_min1' or 'floor')")

    final = whole * min_size

    # Ceiling check. The min-one-contract floor above can hand back more size
    # than the model asked for, and when volatility widens stops a single
    # contract alone can carry more risk than the account should take. This is
    # the escape hatch: skip the trade rather than take it at the wrong size.
    max_risk_pct = sizing_config.get("max_risk_pct")
    if max_risk_pct is not None:
        if stop_price is None:
            return SizingResult(size=0, skipped=True, reason="max_risk_pct requires a stop price")
        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            return SizingResult(size=0, skipped=True, reason="max_risk_pct: stop distance is zero")
        if contract_spec:
            risk_per_unit = (stop_distance / contract_spec.tick_size) * contract_spec.tick_value
        else:
            risk_per_unit = stop_distance
        capital_base = capital_base_amount(
            sizing_config.get("capital_base", "balance"),
            account_balance,
            account_equity,
            float(sizing_config.get("fixed_offset", 0.0)),
            float(sizing_config.get("fixed_amount", 0.0)),
        )
        risk = final * risk_per_unit
        ceiling = capital_base * float(max_risk_pct)
        if risk > ceiling:
            return SizingResult(
                size=0,
                skipped=True,
                reason=(
                    f"size {final} risks {risk:.2f}, over the {float(max_risk_pct):.2%} ceiling "
                    f"of {ceiling:.2f} -- volatility too wide for the smallest tradeable size"
                ),
            )

    return SizingResult(size=final)
