import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

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


def resolve_min_risk_capital(slave_config: Dict[str, Any]) -> float:
    """A floor under the figure RISK is measured against.

    The case it exists for, in the user's words: a 250k account pays 50k
    in tax and then draws down 25k. The withdrawal is not a loss and the
    drawdown is meant to be traded through at the size the account was
    built for, so sizing keeps using 250k rather than the 175k the broker
    reports.

    SAY WHAT THIS DOES, because it is not free. Below the floor, risk is
    a percentage of money that is not there -- 1% of a 250k floor on a
    175k account is 1.43% of what the account actually holds, and it grows
    as the drawdown does. That is the point of the setting, and it is also
    how a drawdown accelerates. It is deliberately separate from
    balance_adjustment: one corrects a figure that is wrong, the other
    overrides a figure that is right.

    Margin is untouched, exactly as with balance_adjustment: the broker
    holds margin against real equity, and a cap measured against a floor
    would believe it had room it does not have.

    0 (the default) means no floor.
    """
    return float(slave_config.get("min_risk_capital", 0.0) or 0.0)


def risk_figures(
    slave_config: Dict[str, Any], balance: float, equity: float
) -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """The (balance, equity) RISK is measured against, or a refusal.

    The standing adjustment first, then the floor. Shared by both engines
    so mirror and autonomous cannot drift: they size through one
    compute_size already, and the figures they hand it should come from
    one place too.
    """
    adjustment = resolve_balance_adjustment(slave_config)
    floor = resolve_min_risk_capital(slave_config)

    adjusted_balance = (balance or 0.0) + adjustment
    adjusted_equity = (equity or 0.0) + adjustment

    if adjustment and (adjusted_balance <= 0 or adjusted_equity <= 0):
        return None, None, (
            f"balance_adjustment of {adjustment:+,.0f} would put the account "
            f"at {adjusted_balance:,.0f} balance / {adjusted_equity:,.0f} "
            f"equity, which is not something risk can be measured against"
        )

    if floor > 0:
        adjusted_balance = max(adjusted_balance, floor)
        adjusted_equity = max(adjusted_equity, floor)

    if adjusted_balance <= 0 or adjusted_equity <= 0:
        return None, None, (
            f"the account reports {adjusted_balance:,.0f} balance / "
            f"{adjusted_equity:,.0f} equity, which is not something risk "
            f"can be measured against"
        )

    return adjusted_balance, adjusted_equity, None


def apply_master_risk(
    sizing_config: Dict[str, Any], master_fraction: Optional[float]
) -> Dict[str, Any]:
    """Size from what the MASTER actually staked, where it said.

    The origin EAs each risk a fixed share of the account they run on. A
    slave that copies them should stake the same share of ITS account,
    adjusted by whatever risk_scale says -- so the slave does not need a
    percentage of its own, and the one it used to carry was a second
    number that had to be kept in step with the EA by hand.

    MEASURED, not declared. master_fraction comes from risk_amount /
    balance in the signal, which is what the order really staked after a
    lot size was rounded and a stop was pulled to the broker's minimum.
    An EA changed without telling anyone changes this automatically.

    Falls back to the configured risk_pct when the master cannot say --
    a bridge older than 1.04, an order with no stop, a balance of zero.
    The fallback matters: without it, a slave would silently stop sizing
    the moment an old master connected, which is a worse failure than
    using a number somebody typed.

    Only touches pct_risk. The other modes do not express risk as a
    fraction of anything, so there is nothing for a fraction to replace.
    """
    if master_fraction is None or master_fraction <= 0:
        return sizing_config
    if (sizing_config or {}).get("mode") != "pct_risk":
        return sizing_config
    out = dict(sizing_config)
    out["risk_pct"] = float(master_fraction)
    return out


# Both keys mean "the ceiling on margin", and until 2026-11-16 each
# engine read only its own.
#
#   netting.max_margin_pct   autonomous  -- refuses, and steps down the
#                                           candidate chain
#   margin.max_pct_of_free   mirror      -- trims the size to fit
#
# So mt5-dwx-futures-1 carried margin.max_pct_of_free 0.25, ran
# AUTONOMOUS, and was not capped at all: a setting that saves cleanly
# and is never read. Both IBKR slaves carried netting.max_margin_pct and
# showed no ceiling on screen, because the card reads the other key.
# Mode is switchable from that card in two clicks, so a slave could
# change which cap applies without anyone touching the cap.
#
# One resolver now, read by all three sites. The two ENGINES keep their
# own behaviour -- trimming and refusing are different answers to the
# same question and changing either silently is not a rename -- but they
# can no longer disagree about whether a ceiling exists.
MARGIN_CAP_KEYS = ("max_margin_pct", "max_pct_of_free")


def resolve_margin_cap(
    slave_config: Optional[Dict[str, Any]],
    route_netting: Optional[Dict[str, Any]] = None,
) -> Optional[float]:
    """The margin ceiling in force, as a fraction, or None for no cap.

    Most specific first: a route's own netting block, then the slave's
    netting, then the legacy `margin` block. Either key is accepted in
    any of them, so existing configs keep working untouched and the UI
    can write one canonical place.
    """
    sources = (
        route_netting,
        (slave_config or {}).get("netting"),
        (slave_config or {}).get("margin"),
    )
    for src in sources:
        if not isinstance(src, dict):
            continue
        for key in MARGIN_CAP_KEYS:
            value = src.get(key)
            if value is not None:
                return float(value)
    return None


def resolve_risk_scale(
    slave_config: Dict[str, Any],
    route_scale: Optional[float] = None,
    magic: Optional[int] = None,
) -> float:
    """How much of the configured risk this leg carries.

    A PRODUCT of three independent dials, each defaulting to 1.0:

        slave `risk_scale`  x  `risk_scale_by_magic[magic]`  x  route scale

    Chris's model, in his own arithmetic: "most EA are going to send a 2%
    risk of balance on signal. most slaves are going to be calibrated to
    0.50 at the intake. on average each magic will then get a 1.0 risk
    scale. some might get 0.50, which should effectively be 1/4 of the
    original signal."

        2% signal  x  0.50 slave intake  x  1.00 magic  =  1%
        2% signal  x  0.50 slave intake  x  0.50 magic  =  0.5%  (a quarter)

    They are different questions and that is why they compose. The slave
    figure asks how much of ANY signal this venue should take, which is a
    property of the venue's leverage. The per-magic figure asks how this
    one strategy compares to the others, which is a property of the
    strategy. Neither answer should erase the other.

    THIS WAS FIRST-MATCH-WINS until 2026-11-15, and that gave 0.50 for the
    last line above rather than 0.25: a strategy with its own figure never
    saw the slave's intake calibration at all, so recalibrating a slave
    silently skipped every strategy that had been individually tuned --
    exactly the ones most likely to matter. Nothing live had either dial
    set when this changed, so no position was resized by the change
    itself.

    ZERO still means "do not trade this here", and survives the product
    intact: 0 times anything is 0, and a non-positive scale is refused
    rather than sent.

    The per-magic table exists because leverage is not the same on every
    venue. Mirror slaves have no routes at all, so without it they could
    only be scaled wholesale.

    JSON object keys are strings, so an integer magic is looked up both
    ways rather than silently missing a table written by hand.
    """
    # `is None` rather than `or 1.0`, so an explicit 0 -- "do not trade
    # anything here" -- is honoured rather than read as unset. A JSON null
    # left by a form that cleared the field means unset, and must not
    # reach float().
    declared = slave_config.get("risk_scale")
    scale = 1.0 if declared is None else float(declared)

    if magic is not None:
        by_magic = slave_config.get("risk_scale_by_magic") or {}
        if isinstance(by_magic, dict):
            for key in (str(magic), magic):
                if key in by_magic:
                    scale *= float(by_magic[key])
                    break

    if route_scale is not None:
        scale *= float(route_scale)

    return scale


# The leading digit of a magic names the timeframe the strategy runs on.
# Chris's scheme; the shipped default, overridable per slave by
# `bar_size_by_magic_prefix`, and switched off by setting that to {}.
DEFAULT_BAR_SIZE_BY_MAGIC_PREFIX = {
    "1": "1 day",
    "2": "4 hours",
    "3": "1 hour",
    "4": "1 week",
}


def bar_size_for_magic_prefix(
    magic: Optional[int], table: Optional[Dict[str, Any]] = None
) -> Optional[str]:
    """The timeframe a magic's FIRST digit names, or None.

    None for anything the table does not name. Never a guess: the test
    magics 5555, 7001 and 9999 all start outside 1-4, and inventing a
    timeframe for them would be worse than the default they get now.
    """
    if magic is None:
        return None
    if table is None:
        table = DEFAULT_BAR_SIZE_BY_MAGIC_PREFIX
    if not table:
        return None      # explicitly disabled
    digits = str(abs(int(magic)))
    if not digits:
        return None
    value = table.get(digits[0])
    return (str(value) or None) if value is not None else None


def resolve_bar_size(
    slave_config: Dict[str, Any],
    route_bar_size: Optional[str] = None,
    magic: Optional[int] = None,
) -> Optional[str]:
    """Which bars this leg's ATR is measured on.

    Four places can say, most specific first:

    1. the ROUTE, which already names a (symbol, magic) pair
    2. `bar_size_by_magic`, keyed by the EA's whole magic number -- the
       exception, for one strategy that does not follow the scheme
    3. the magic's LEADING DIGIT: 1 daily, 2 H4, 3 H1, 4 weekly. A rule
       rather than a list, so a strategy deployed today measures
       correctly today. Set `bar_size_by_magic_prefix` to change the map
       or to {} to switch it off
    4. `bar_size` on the slave

    None means the adapter's own default, which is daily -- what every
    route meant before any of this existed.

    THE PER-MAGIC TABLE IS THE POINT. A timeframe is a property of the
    strategy that set the stop, not of the instrument it lands on or of
    the account. A weekly system signals off weekly data and executes on
    the daily bar at Monday's open, because MT4 has no weekly execution
    bar -- but its stop was measured in weekly ATRs, so translating that
    multiple against a DAILY ATR makes the stop roughly a fifth of its
    intended width. Risk sizing divides by stop distance, so the position
    comes out several times too large, and nothing on any screen says so.

    Putting it on the route instead would need a route per magic, and
    could not serve a mirror slave at all -- those have no routes and
    still take a selection of strategies.

    JSON object keys are strings, so an integer magic is looked up both
    ways rather than silently missing a table written by hand.
    """
    if route_bar_size:
        return route_bar_size

    if magic is not None:
        by_magic = slave_config.get("bar_size_by_magic") or {}
        if isinstance(by_magic, dict):
            for key in (str(magic), magic):
                if key in by_magic:
                    value = by_magic[key]
                    # An empty string is not a timeframe. Returned as None
                    # so it means "the default" rather than reaching an
                    # adapter as a bar size it has to reject.
                    return str(value) or None

    # The scheme. Below the explicit table so one odd strategy can opt
    # out, above the slave default so a slave-wide bar_size does not
    # flatten every timeframe back to one.
    from_prefix = bar_size_for_magic_prefix(
        magic, slave_config.get("bar_size_by_magic_prefix")
    )
    if from_prefix:
        return from_prefix

    return slave_config.get("bar_size") or None


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
    round_band: Optional[float] = None,
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

    # NEAR A WHOLE CONTRACT, OR STEP DOWN TO A SMALLER ONE.
    #
    # Rounding is the whole problem, and its cost depends on WHERE the
    # exact figure falls, not on how large it is. 1.61 GC floors to 1 and
    # throws away 38% of the intended risk; 1.90 rounds to 2 at a cost of
    # 5%. Count alone cannot tell those two apart, which is why the
    # min_contracts threshold this replaces sent both to the micros.
    #
    # Chris's rule, in his own figures: "the GC exact was 1.61, for this
    # I'd accept when >1.75, round up. when <1.25 round down. favoring
    # the larger single contracts despite losing some risk (up and down)
    # on the rounding."
    #
    # ROUNDING UP RAISES RISK ABOVE TARGET, deliberately -- 1.76 becomes
    # 2, some 14% over. max_risk_pct is the backstop for anything
    # genuinely oversized.
    #
    # NEVER ROUNDS UP TO ONE from below. At 0.9 the band would say "take
    # 1", 11% over, while the micro takes 9 of its own at no cost at all.
    # Below a whole contract the smaller instrument always fits better,
    # so that case steps down like any other.
    #
    # Applied only where a smaller alternative exists: the caller passes
    # None for the last candidate, or a signal landing mid-band on the
    # micro would step down to nothing and fill nothing at all.
    if round_band is not None:
        band = float(round_band)
        down = math.floor(units)
        frac = units - down
        whole = None
        if down >= 1 and frac <= band:
            whole = down
        elif down >= 1 and frac >= 1.0 - band:
            whole = down + 1
        if whole is None:
            cost = (1 - down / units) * 100 if down >= 1 else 100.0
            return SizingResult(
                size=0, skipped=True,
                reason=(
                    f"wants {units:g} of them, not within {band:g} of a whole "
                    f"contract -- rounding down would cost {cost:.0f}% of the "
                    f"intended risk, so a smaller contract is the better fit"
                ),
            )
        final = whole * min_size
        return _within_ceiling(
            final, sizing_config=sizing_config, entry_price=entry_price,
            stop_price=stop_price, account_balance=account_balance,
            account_equity=account_equity, contract_spec=contract_spec,
        )

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
    return _within_ceiling(
        final, sizing_config=sizing_config, entry_price=entry_price,
        stop_price=stop_price, account_balance=account_balance,
        account_equity=account_equity, contract_spec=contract_spec,
    )


def _within_ceiling(
    final: float,
    *,
    sizing_config: Dict[str, Any],
    entry_price: float,
    stop_price: Optional[float],
    account_balance: float,
    account_equity: float,
    contract_spec: Optional[ContractSpec],
) -> SizingResult:
    """The max_risk_pct gate, shared by every rounding path.

    Extracted when round_band gained its own path to `final`. Leaving the
    gate inline would have meant one rounding rule reaching the ceiling
    check and another bypassing it -- the kind of split where the second
    path quietly stops being guarded.
    """
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
