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
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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

        targets = entry.get("targets")
        if targets is None:
            target = entry.get("target")
            targets = [target] if target else []
        elif isinstance(targets, str):
            targets = [targets]

        for target in targets:
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
                )
            )
    return matched


def describe_refusal(slave_config: Dict[str, Any], signal: Signal) -> str:
    """Why a signal wasn't routed, phrased so the fix is obvious."""
    if routing_is_strict(slave_config):
        known = sorted(
            {
                (e.get("symbol"), e.get("magic"), e.get("account"))
                for e in slave_config.get("routes", [])
            }
        )
        return (
            f"no route for symbol={signal.symbol} magic={signal.magic} "
            f"account={signal.account!r} -- unrouted signals are "
            f"refused so an unknown strategy can't land on another strategy's contract. "
            f"Configured routes (symbol, magic, account): {known}"
        )
    return f"no symbol_map entry for master symbol={signal.symbol}"
