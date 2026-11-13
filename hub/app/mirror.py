"""What mirror mode decides before it sends an order.

Mirror mode used to decide almost nothing. It passed the master's symbol
through untranslated and multiplied the lots by a constant, which is exactly
right for two accounts at the same broker and falls apart the moment the
slave's instrument is not the master's. That moment has arrived: the futures
slave trades CME contracts while the master trades spot CFDs, so XAUUSD has
to become GC_Z and one BTCUSD lot has to become ten MBT_X contracts.

Two decisions live here, both pure functions over config and positions so
they can be tested without a broker or a terminal:

    resolve_target   which symbol, and how many lots of it
    netting_refusal  whether the account's existing exposure forbids it

The config key is `mirror_map`, deliberately not `symbol_map`: the
autonomous engine already owns `symbol_map` with a different value shape
(a target or list of targets, see app/symbol_mapper.py), and one key
meaning two things depending on which engine read it is how a config file
becomes a guessing game.

    "mirror_map": {
        "XAUUSD": "GC_Z",                       1 lot -> 1 lot
        "BTCUSD": {"target": "MBT_X",
                   "master_point_value": 1.0,   from the master's SYMBOLS
                   "point_value": 0.1,          from the slave's SYMBOLS
                   "volume_step": 1.0,
                   "volume_min": 1.0}
    }

A point value is what a one-point move is worth on one lot, which is
`tick_value / tick_size` from the SYMBOLS command and the same number as
contract_specs.point_value. Ratios are derived from the pair rather than
configured directly so that an inverted ratio is impossible to write by
accident -- the two figures are read off the two brokers and the division
happens here. `lot_ratio` is accepted for the cases where no point value
describes the relationship.
"""
import logging
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Tuple

from app.contract_specs import ContractSpec
from app.sizing import capital_base_amount, compute_size, resolve_balance_adjustment

logger = logging.getLogger(__name__)

DEFAULT_STEP = 0.01


@dataclass
class MirrorTarget:
    """Where a master signal lands on this slave, and how big."""

    symbol: str
    lots: float
    # One line explaining how `lots` was arrived at. Logged with every open,
    # because a size nobody can account for is a size nobody can trust.
    how: str


def floor_to_step(value: float, step: float) -> float:
    """Floor to the volume grid, never round.

    Flooring, because mirror sizing must never hand back more than it was
    asked for -- the same rule SlaveBridge.mq5's NormalizeVolume enforces at
    the other end, and the two have to agree or one of them is wrong.

    The quotient is snapped to an exact integer when it is within a millionth
    of one, or binary representation eats a whole step: 0.03 / 0.01 is
    2.9999999999999996 in doubles, and a bare floor would turn an exact 0.03
    into 0.02. A millionth of a step is far below any volume a broker
    distinguishes, so it can only absorb arithmetic noise.
    """
    if step <= 0:
        step = DEFAULT_STEP
    quotient = value / step
    nearest = round(quotient)
    whole = nearest if abs(quotient - nearest) < 1e-6 else math.floor(quotient)
    return round(whole * step, 8)


def _lot_ratio(spec: Dict[str, Any], symbol: str) -> Tuple[Optional[float], Optional[str]]:
    """Target lots per master lot: (ratio, None) or (None, reason)."""
    if "lot_ratio" in spec:
        ratio = float(spec["lot_ratio"] or 0.0)
        if ratio <= 0:
            return None, f"mirror_map[{symbol}].lot_ratio is {ratio}, which is not a size"
        return ratio, None

    master_pv = spec.get("master_point_value")
    target_pv = spec.get("point_value")
    if master_pv is None and target_pv is None:
        return 1.0, None
    # Half a ratio is worse than none: it would silently size as 1:1 while
    # looking configured.
    if master_pv is None or target_pv is None:
        return None, (
            f"mirror_map[{symbol}] gives only one side of the point-value ratio "
            f"(master_point_value={master_pv}, point_value={target_pv}). Both are "
            f"needed, or neither -- read them off each broker's SYMBOLS reply as "
            f"tick_value / tick_size."
        )
    master_pv = float(master_pv)
    target_pv = float(target_pv)
    if master_pv <= 0 or target_pv <= 0:
        return None, (
            f"mirror_map[{symbol}] point values must be positive, got "
            f"master_point_value={master_pv}, point_value={target_pv}"
        )
    return master_pv / target_pv, None


def resolve_symbol(
    slave_config: Dict[str, Any], master_symbol: str
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """Which instrument this signal becomes on this slave.

    Separate from the sizing so that risk-based sizing can use it without
    the lot arithmetic -- which would otherwise refuse a 0.1-lot master
    signal for being below a futures contract's minimum, before the risk
    model ever got a chance to say how many contracts 2% actually buys.

    Returns (symbol, spec, None) or (None, {}, reason).
    """
    mirror_map = slave_config.get("mirror_map") or {}
    entry = mirror_map.get(master_symbol)

    if entry is None:
        if mirror_map and not slave_config.get("mirror_map_passthrough", False):
            # A configured map IS the route list. Passing an unmapped symbol
            # through to a broker that does not carry it only produces
            # "symbol not offered" from the far end, one round trip later.
            return None, {}, (
                f"no mirror_map entry for {master_symbol}; this slave only mirrors "
                f"the {len(mirror_map)} mapped symbol(s). Set "
                f"mirror_map_passthrough to send unmapped symbols through unchanged."
            )
        return master_symbol, {}, None

    if isinstance(entry, str):
        return entry, {}, None

    if isinstance(entry, dict):
        return str(entry.get("target") or master_symbol), entry, None

    return None, {}, (
        f"mirror_map[{master_symbol}] should be a target symbol or an object, "
        f"got {type(entry).__name__}"
    )


def resolve_target(
    slave_config: Dict[str, Any], master_symbol: str, master_lots: float
) -> Tuple[Optional[MirrorTarget], Optional[str]]:
    """Which instrument this signal becomes, and how many lots of it.

    Lot mirroring: the master's size, converted by the instruments' point
    values. Used when the slave has no `sizing` block -- with one, the size
    comes from the slave's own capital instead; see plan_by_risk.

    Returns (target, None) to proceed, or (None, reason) to refuse. The reason
    is written for whoever reads the log at the time, so it names the figures.
    """
    scale = float(slave_config.get("risk_scale", 1.0) or 0.0)
    if scale <= 0:
        return None, (
            f"risk_scale is {scale}, which is not a tradeable fraction; refusing "
            f"rather than sending a zero or inverted size"
        )

    symbol, spec, reason = resolve_symbol(slave_config, master_symbol)
    if symbol is None:
        return None, reason

    ratio, reason = _lot_ratio(spec, master_symbol)
    if ratio is None:
        return None, reason

    step = float(spec.get("volume_step") or DEFAULT_STEP)
    floor = float(spec.get("volume_min") or step)

    raw = float(master_lots) * ratio * scale
    lots = floor_to_step(raw, step)

    parts = [f"{master_lots} master lots"]
    if ratio != 1.0:
        parts.append(f"x {ratio:g} ({master_symbol} -> {symbol} point value)")
    if scale != 1.0:
        parts.append(f"x {scale:g} risk_scale")
    if lots != raw:
        parts.append(f"floored to the {step:g} grid")
    how = " ".join(parts) + f" = {lots:g}"

    if lots < floor:
        return None, (
            f"{master_lots} master lots become {raw:g} on {symbol} "
            f"({how}), below its minimum of {floor:g}. Refusing rather than "
            f"rounding up, which would carry more risk than the master."
        )

    return MirrorTarget(symbol=symbol, lots=lots, how=how), None


@dataclass
class MirrorPlan:
    """A fully priced order for the slave's own instrument."""

    symbol: str
    lots: float
    sl: Optional[float]
    tp: Optional[float]
    how: str
    # The broker's volume grid, carried along because whatever trims this
    # size afterwards -- the margin ceiling -- has to land on it too, and
    # should not have to ask the broker a second time to find out what it is.
    volume_step: float = DEFAULT_STEP
    volume_min: float = DEFAULT_STEP


def _on_tick_grid(level: float, tick_size: float, digits: int) -> float:
    if tick_size <= 0:
        return round(level, digits)
    return round(round(level / tick_size) * tick_size, digits)


def plan_by_risk(
    slave_config: Dict[str, Any],
    target_symbol: str,
    direction: str,
    master_price: float,
    master_sl: Optional[float],
    master_tp: Optional[float],
    spec: Any,
    balance: float,
    equity: float,
) -> Tuple[Optional[MirrorPlan], Optional[str]]:
    """Size this signal as a percentage of the SLAVE's own capital.

    This is how risk has been porting on every other slave: ibkr-paper-1
    sizes 2% of its own equity per signal, and the master's lot count never
    enters into it. A mirror slave could not do that until the bridge could
    report its balance, so it only had risk_scale -- a multiple of a lot
    count, which means nothing once the slave's instrument is not the
    master's.

    The stop travels as a FRACTION of price, not as a level. A level is
    meaningless across instruments: US500 quoting 7594 and ES_Z quoting its
    own index would make the master's 7500 stop either absurd or already
    triggered. A fraction is scale-free, survives the two feeds disagreeing
    by a few points, and is what an ATR multiple effectively is anyway.

    Returns (plan, None) or (None, reason).
    """
    sizing = slave_config.get("sizing") or {}
    if not sizing.get("mode"):
        return None, "sizing has no mode, so there is no sizing model to apply"

    if not master_sl or master_price <= 0:
        return None, (
            f"no stop on the master signal (price={master_price}, sl={master_sl}), "
            f"so a risk-based size cannot be computed. Risk is distance times "
            f"point value; without a stop there is no distance."
        )

    stop_fraction = abs(master_price - master_sl) / master_price
    if stop_fraction <= 0:
        return None, "the master's stop is at its entry price, which is no distance"

    price = spec.price_for(direction)
    if price <= 0:
        return None, (
            f"{target_symbol} has no price yet (bid={spec.bid}, ask={spec.ask}); "
            f"it may have only just been added to Market Watch"
        )

    point_value = spec.point_value
    if point_value <= 0:
        return None, (
            f"{target_symbol} reports tick_size={spec.tick_size} and "
            f"tick_value={spec.tick_value}, which gives no point value to size on"
        )

    stop_distance = price * stop_fraction
    sign = 1.0 if direction == "BUY" else -1.0
    sl = _on_tick_grid(price - sign * stop_distance, spec.tick_size, spec.digits)
    tp = None
    if master_tp:
        # Carried across the same way, so the reward-to-risk the master chose
        # survives the translation.
        tp_fraction = abs(master_tp - master_price) / master_price
        tp = _on_tick_grid(
            price + sign * price * tp_fraction, spec.tick_size, spec.digits
        )

    # The broker's own figures, in the shape the shared sizing model expects.
    # min_size is the coarser of the step and the minimum: the step alone
    # would let a size through below what the broker will accept.
    contract_spec = ContractSpec(
        symbol=target_symbol,
        tick_size=spec.tick_size,
        tick_value=spec.tick_value,
        point_value=point_value,
        min_size=max(spec.volume_min or 0.0, spec.volume_step or DEFAULT_STEP),
    )

    # The standing portfolio-level correction, applied for the same reason
    # the autonomous engine applies it: it is a fact about the account, not
    # about one strategy's sizing mode. Added to both figures so it lands
    # whichever capital_base is in force.
    adjustment = resolve_balance_adjustment(slave_config)

    # compute_size, not arithmetic of our own. This is the same function
    # ibkr-paper-1 sizes through, so "port the risk the way it ports
    # everywhere else" is true by construction rather than by resemblance --
    # including rounding (nearest_min1 keeps a small signal at one contract
    # instead of refusing it) and the max_risk_pct ceiling that stops that
    # minimum contract carrying more than the account agreed to.
    result = compute_size(
        sizing_config=sizing,
        master_lots=0.0,
        entry_price=price,
        stop_price=sl,
        account_balance=balance + adjustment,
        account_equity=equity + adjustment,
        contract_spec=contract_spec,
        risk_scale=float(slave_config.get("risk_scale", 1.0) or 0.0),
    )
    if result.skipped or result.size <= 0:
        return None, (
            f"{target_symbol}: {result.reason or 'sizing produced nothing'} "
            f"(price {price}, stop {sl}, {stop_fraction:.3%} away, point value "
            f"{point_value:g})"
        )

    lots = float(result.size)
    # Measured off the rounded stop, which is the one compute_size sized
    # against and the one the order will carry -- quoting the unrounded
    # distance here would print a risk per lot nobody can reproduce.
    placed_distance = abs(price - sl)
    # The capital that was actually sized against, which is not necessarily
    # the balance: capital_base chooses, and this slave uses equity. Printing
    # the wrong one makes the line unreproducible, and a diagnostic nobody can
    # check is worse than none -- so it is derived the same way compute_size
    # derives it, and it names which base it is.
    base = sizing.get("capital_base", "balance")
    capital = capital_base_amount(
        base, balance + adjustment, equity + adjustment,
        float(sizing.get("fixed_offset", 0.0) or 0.0),
        float(sizing.get("fixed_amount", 0.0) or 0.0),
    )
    how = (
        f"{sizing.get('mode')} on {target_symbol}: a {stop_fraction:.3%} stop "
        f"({placed_distance:.{spec.digits}f} away at {price}) risks "
        f"{placed_distance * point_value:.2f}/lot of {capital:.2f} "
        f"({base}) -> {lots:g} lots"
    )
    if result.reason:
        how += f" ({result.reason})"

    if spec.volume_max and lots > spec.volume_max:
        # Not a refusal: SlaveBridge.mq5 1.04 opens this as a position group
        # of several deals. Worth saying out loud, because it is the only
        # place a reader can see that one signal became ten orders.
        how += (
            f" -- above {target_symbol}'s maximum of {spec.volume_max:g}, so the "
            f"bridge will open it as {math.ceil(lots / spec.volume_max)} deals "
            f"held together as one group"
        )

    return MirrorPlan(
        symbol=target_symbol, lots=lots, sl=sl, tp=tp, how=how,
        volume_step=spec.volume_step or DEFAULT_STEP,
        volume_min=max(spec.volume_min or 0.0, spec.volume_step or DEFAULT_STEP),
    ), None


def margin_fit(
    lots: float,
    preview: Any,
    policy: Optional[Dict[str, Any]],
    volume_step: float,
    volume_min: float,
) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """Trim a risk-correct size down to one the account can margin.

    Returns (lots, note, refusal). Exactly one of lots/refusal is set; note
    is present when the size was reduced.

    A risk model bounds the loss at the stop and says nothing whatever about
    margin, and the two come apart when the stop is tight: 2% risked over a
    1% stop is twice equity in notional, whatever the instrument. Measured on
    MBT_X -- 3,400 of margin a lot against a 7,161 notional, 47% -- the
    risk-correct size wanted more margin than the account had.

    Reduced rather than refused, because a trade at a size the account can
    carry is worth more than no trade, and reducing is the safe direction:
    it carries less risk than asked for, never more. The reduction is
    reported so a position sized by margin rather than by the risk model is
    never mistaken for one the risk model chose.

    `preview.per_unit` is what makes the arithmetic possible in one round
    trip: margin is linear in volume for these instruments, so the affordable
    size is (free x ceiling) / per_unit.
    """
    policy = policy or {}
    ceiling_pct = policy.get("max_pct_of_free")
    if ceiling_pct is None:
        return lots, None, None

    required = getattr(preview, "initial_change", None)
    free = getattr(preview, "free_before", None)
    per_unit = getattr(preview, "per_unit", None)
    if not required or not free:
        return None, None, (
            "a margin ceiling is configured but the slave did not report a "
            "usable margin figure, so the ceiling cannot be enforced. Refusing "
            "rather than trading past a limit that is switched on."
        )

    ceiling = float(free) * float(ceiling_pct)
    if required <= ceiling:
        return lots, None, None

    if not per_unit or per_unit <= 0:
        return None, None, (
            f"{required:.2f} of margin is needed and the ceiling is {ceiling:.2f} "
            f"({float(ceiling_pct):.0%} of {float(free):.2f} free), but the slave "
            f"reported no per-lot margin, so there is no way to work out an "
            f"affordable size"
        )

    affordable = floor_to_step(ceiling / float(per_unit), volume_step or DEFAULT_STEP)
    floor = max(volume_min or 0.0, volume_step or DEFAULT_STEP)
    if affordable < floor:
        return None, None, (
            f"even the minimum {floor:g} lots needs {floor * float(per_unit):.2f} of "
            f"margin, over the ceiling of {ceiling:.2f} ({float(ceiling_pct):.0%} of "
            f"{float(free):.2f} free). This instrument cannot be traded on this "
            f"account at this size."
        )

    note = (
        f"REDUCED by margin from {lots:g} to {affordable:g} lots: {lots:g} needed "
        f"{required:.2f} of margin at {float(per_unit):.2f}/lot, over the "
        f"{float(ceiling_pct):.0%} ceiling of {ceiling:.2f} on {float(free):.2f} "
        f"free. This leg carries {affordable / lots:.1%} of the risk the model asked "
        f"for"
    )
    return affordable, note, None


def netting_refusal(
    positions: Iterable[Any],
    symbol: str,
    direction: str,
    lots: float,
    policy: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Does what the account already holds forbid this leg?

    `positions` are the slave's own open positions as the adapter reports
    them, with signed `quantity`. They are NOT only the copier's: the futures
    slave runs its own strategies, and when this was first measured it held
    73 lots short MBT_X. A 200-lot long copy there does not add exposure, it
    reverses the account -- and on a hedging account both sides sit there
    independently, so nothing in the terminal objects.

    Returns a reason to refuse, or None to proceed. Policy lives under
    `netting` in the slave config:

        allow_opposing   default False -- permit a leg against existing
                         exposure in the same symbol
        max_legs         ceiling on same-side positions in the symbol
        max_net_lots     ceiling on |net exposure| after this leg
    """
    policy = policy or {}
    rows = [p for p in positions if getattr(p, "symbol", None) == symbol]
    if not rows:
        return None

    net = sum(float(getattr(p, "quantity", 0.0) or 0.0) for p in rows)
    signed = float(lots) if direction == "BUY" else -float(lots)

    if not policy.get("allow_opposing", False):
        opposing = (net > 0 and signed < 0) or (net < 0 and signed > 0)
        if opposing:
            return (
                f"the account already holds {net:+g} lots of {symbol} and this "
                f"signal is {direction} {lots:g}. That does not add exposure, it "
                f"offsets it -- and on a hedging account both legs sit there, so "
                f"the account's real position is {net + signed:+g} while this copy "
                f"is tracked as {direction} {lots:g} with its stop on the other "
                f"side. These positions are not necessarily the copier's. Set "
                f"netting.allow_opposing to override."
            )

    max_legs = policy.get("max_legs")
    if max_legs is not None:
        same_side = [
            p for p in rows
            if (float(getattr(p, "quantity", 0.0) or 0.0) > 0) == (signed > 0)
        ]
        if len(same_side) >= int(max_legs):
            return (
                f"already holding {len(same_side)} {direction} position(s) on "
                f"{symbol}, at the netting.max_legs limit of {int(max_legs)}"
            )

    # Either one number for every symbol, or a number per symbol. Per symbol
    # is usually what is meant: a ceiling that suits gold at 388,290 a
    # contract is meaningless for bitcoin at 7,161.
    max_net = policy.get("max_net_lots")
    if isinstance(max_net, dict):
        max_net = max_net.get(symbol)
    if max_net is not None and abs(net + signed) > float(max_net) + 1e-9:
        return (
            f"{net:+g} lots of {symbol} are open and this leg would make "
            f"{net + signed:+g}, over the netting.max_net_lots ceiling of "
            f"{float(max_net):g}"
        )

    return None
