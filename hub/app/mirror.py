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


def resolve_target(
    slave_config: Dict[str, Any], master_symbol: str, master_lots: float
) -> Tuple[Optional[MirrorTarget], Optional[str]]:
    """Which instrument this signal becomes on this slave, and how many lots.

    Returns (target, None) to proceed, or (None, reason) to refuse. The reason
    is written for whoever reads the log at the time, so it names the figures.
    """
    scale = float(slave_config.get("risk_scale", 1.0) or 0.0)
    if scale <= 0:
        return None, (
            f"risk_scale is {scale}, which is not a tradeable fraction; refusing "
            f"rather than sending a zero or inverted size"
        )

    mirror_map = slave_config.get("mirror_map") or {}
    entry = mirror_map.get(master_symbol)

    if entry is None:
        if mirror_map and not slave_config.get("mirror_map_passthrough", False):
            # A configured map IS the route list. Passing an unmapped symbol
            # through to a broker that does not carry it only produces
            # "symbol not offered" from the far end, one round trip later.
            return None, (
                f"no mirror_map entry for {master_symbol}; this slave only mirrors "
                f"the {len(mirror_map)} mapped symbol(s). Set "
                f"mirror_map_passthrough to send unmapped symbols through unchanged."
            )
        spec: Dict[str, Any] = {}
        symbol = master_symbol
    elif isinstance(entry, str):
        spec = {}
        symbol = entry
    elif isinstance(entry, dict):
        spec = entry
        symbol = str(spec.get("target") or master_symbol)
    else:
        return None, (
            f"mirror_map[{master_symbol}] should be a target symbol or an object, "
            f"got {type(entry).__name__}"
        )

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

    max_net = policy.get("max_net_lots")
    if max_net is not None and abs(net + signed) > float(max_net) + 1e-9:
        return (
            f"{net:+g} lots of {symbol} are open and this leg would make "
            f"{net + signed:+g}, over the netting.max_net_lots ceiling of "
            f"{float(max_net):g}"
        )

    return None
