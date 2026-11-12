"""Routing a master signal to slave targets.

Several strategies routinely run on one master account and trade the same
symbol. On MT4 they're separate tickets; on a netting broker like IBKR they
are not -- two strategies long GC become one 2-lot position, and long plus
short cancel outright. The autonomous engine also manages a stop per tracked
position, so two stops against one netted position can fire and flip the
account.

So each strategy gets its own contract month: same root, same contract size
(strategies stay risk-equivalent), different conId, cannot net. `magic` is the
link -- it's what identifies the strategy on the master side.

Two modes, chosen by whether the slave config has a `routes` key:

- `routes` present: strict. A signal whose (symbol, magic) matches no route is
  refused. That's deliberate -- on a live account, an unrecognised strategy
  silently landing on slot 0 and netting against a known one is the worst
  available outcome.
- `routes` absent: the legacy `symbol_map` behaviour, every signal routed to
  slot 0. Keeps existing slaves (and all mirror-mode slaves) working.
"""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.models import Signal
from app.symbol_mapper import map_symbol


@dataclass
class Route:
    """One resolved destination for a signal on one slave."""

    target: str
    month_slot: int = 0
    magic: Optional[int] = None
    # Which master this route accepts signals from. None matches any, which
    # is the right default for a single-master hub; with several masters,
    # magic alone cannot tell two of them apart when they run the same EA.
    account: Optional[str] = None
    sizing: Optional[Dict[str, Any]] = None
    risk_management: Optional[Dict[str, Any]] = None
    # ATR is a property of the strategy that set the stop, not of the
    # account the copy lands in. Two strategies on one slave can use
    # different periods, and a slave-wide setting would silently apply one
    # of them to both -- translating the other's stop against an ATR it
    # never meant. None falls back to the slave's own default.
    atr_period: Optional[int] = None
    # The bars that period counts. Same reasoning: a strategy measuring its
    # ATR on H4 and a slave measuring the same multiple on daily bars are
    # comparing two different volatilities, and nothing in the numbers would
    # say so. None means the adapter's own default (daily).
    bar_size: Optional[str] = None
    # ETF legs fill in the evening session, where IBKR refuses market
    # orders outright. Both of these are needed: a limit order without
    # outside_rth waits for the open, and outside_rth on a market order is
    # rejected.
    order_type: str = "market"
    outside_rth: bool = False
    # How far through the quote to place the limit, as a fraction. Crossing
    # the spread is the point -- a limit sitting at the bid in a thin
    # evening book is a signal that quietly never becomes a position.
    limit_offset_pct: float = 0.0
    # Which way this route trades. None takes both, which is right for a
    # futures leg that can be short as easily as long. An ETF leg usually
    # cannot: the long-side proxies here have no inverse twin, so without
    # this a SELL on the master would SHORT a leveraged ETF -- overnight,
    # where borrow is scarce and the position is the opposite of the one
    # intended.
    direction: Optional[str] = None
    # Which way to trade the TARGET, when that differs from the signal.
    # Inverse ETFs are long-only instruments: you express a short view by
    # BUYING one. So a route matching direction "SELL" trades
    # target_direction "BUY" on a different symbol. None follows the signal,
    # which is right for a futures leg that simply shorts the same contract.
    target_direction: Optional[str] = None
    # How much of the configured risk this strategy carries on this slave.
    # None falls back to the slave's own risk_scale, then to 1.0.
    risk_scale: Optional[float] = None
    # Whether to rest broker-side stop/target orders for this route. ETF
    # legs set this false: those orders do not execute in extended hours, so
    # resting them achieves nothing while leaving GTC orders to strand when
    # the entry is cancelled. Such a leg is closed by the master's CLOSE.
    broker_brackets: bool = True
    # Candidate contracts for THIS one position, in order of preference.
    # Not to be confused with the config's plural `targets`, which fans one
    # signal out into several positions. `target` remains the first
    # candidate, so anything keyed on "the route's instrument" -- the
    # deferral and its log lines -- is unchanged.
    candidates: List[str] = field(default_factory=list)
    # Per-strategy netting policy, overriding the slave's. A pyramiding
    # strategy may stack three legs where everything else on the slave
    # stacks none.
    netting: Optional[Dict[str, Any]] = None


def routing_is_strict(slave_config: Dict[str, Any]) -> bool:
    return slave_config.get("routes") is not None


def resolve_routes(slave_config: Dict[str, Any], signal: Signal) -> List[Route]:
    """Destinations for this signal on this slave, or [] if it isn't routed.

    An entry with no `magic` matches any strategy for that symbol -- an
    explicit catch-all, opted into rather than implied.
    """
    routes_cfg = slave_config.get("routes")
    if routes_cfg is None:
        return [Route(target=t) for t in map_symbol(slave_config, signal.symbol)]

    matched: List[Route] = []
    for entry in routes_cfg:
        if entry.get("symbol") != signal.symbol:
            continue
        magic = entry.get("magic")
        if magic is not None and signal.magic != int(magic):
            continue
        account = entry.get("account")
        if account is not None and signal.account != str(account):
            continue
        direction = entry.get("direction")
        if direction is not None and signal.direction != str(direction).upper():
            continue

        # Two different ideas, deliberately separate keys:
        #   "target":  ["NG", "MNG"]   one position, preferred contract first
        #   "targets": ["MGC", "GLD"]  one position on each
        # Allowing both at once would make "three names" ambiguous between
        # three positions and three fallbacks, so it is refused outright.
        fan_out = entry.get("targets")
        target_entry = entry.get("target")
        if fan_out is not None and isinstance(target_entry, (list, tuple)):
            raise ValueError(
                f"route for {entry.get('symbol')!r} gives both a `targets` "
                f"fan-out and a `target` preference list; use one or the other"
            )

        if fan_out is not None:
            groups = [[t] for t in ([fan_out] if isinstance(fan_out, str) else fan_out)]
        elif target_entry is None:
            groups = []
        elif isinstance(target_entry, str):
            groups = [[target_entry]]
        else:
            groups = [[str(t) for t in target_entry]]

        for group in groups:
            target = group[0]
            matched.append(
                Route(
                    target=target,
                    month_slot=int(entry.get("month_slot", 0)),
                    magic=None if magic is None else int(magic),
                    account=None if account is None else str(account),
                    sizing=entry.get("sizing"),
                    risk_management=entry.get("risk_management"),
                    atr_period=(None if entry.get("atr_period") is None
                                else int(entry["atr_period"])),
                    bar_size=entry.get("bar_size"),
                    order_type=entry.get("order_type", "market"),
                    outside_rth=bool(entry.get("outside_rth", False)),
                    limit_offset_pct=float(entry.get("limit_offset_pct", 0.0)),
                    direction=(None if entry.get("direction") is None
                               else str(entry["direction"]).upper()),
                    target_direction=(None if entry.get("target_direction") is None
                                      else str(entry["target_direction"]).upper()),
                    risk_scale=(None if entry.get("risk_scale") is None
                                else float(entry["risk_scale"])),
                    broker_brackets=bool(entry.get("broker_brackets", True)),
                    candidates=list(group),
                    netting=entry.get("netting"),
                )
            )
    return matched


def configured_targets(slave_config: Dict[str, Any]) -> List[Tuple[str, int]]:
    """Every (target, month_slot) this config can route a signal to.

    Independent of any signal, because startup has none: a target reachable
    only by a SELL on one magic still has to resolve before that signal
    arrives. Covers the singular `target` as a string and as a preference
    list, and the plural `targets` fan-out, since any of them can name an
    instrument that does not exist at the broker.
    """
    out: List[Tuple[str, int]] = []
    seen = set()
    for entry in slave_config.get("routes", []) or []:
        slot = int(entry.get("month_slot", 0))
        names = []
        fan_out = entry.get("targets")
        target_entry = entry.get("target")
        if fan_out is not None:
            names += [fan_out] if isinstance(fan_out, str) else list(fan_out)
        if isinstance(target_entry, str):
            names.append(target_entry)
        elif isinstance(target_entry, (list, tuple)):
            names += [str(t) for t in target_entry]
        for name in names:
            key = (str(name), slot)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def describe_refusal(slave_config: Dict[str, Any], signal: Signal) -> str:
    """Why a signal wasn't routed, phrased so the fix is obvious."""
    if routing_is_strict(slave_config):
        known = sorted(
            {
                (e.get("symbol"), e.get("magic"), e.get("account"), e.get("direction"))
                for e in slave_config.get("routes", [])
            }
        )
        return (
            f"no route for symbol={signal.symbol} magic={signal.magic} "
            f"account={signal.account!r} -- unrouted signals are "
            f"refused so an unknown strategy can't land on another strategy's contract. "
            f"Configured routes (symbol, magic, account, direction): {known}"
        )
    return f"no symbol_map entry for master symbol={signal.symbol}"
