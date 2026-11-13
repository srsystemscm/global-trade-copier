"""When to roll a futures position, and in what order.

Positions held across an expiry have to move to the next contract. Two things
make this more than a loop:

1. **Trigger.** Calendar rules ("N days before expiry") don't match where the
   liquidity actually is. The market's own answer is the volume crossover:
   once the next month out-trades the front, the front is dying. Measured on
   COMEX gold, the front carried 29,179 contracts/day against the next at
   2,098 -- nowhere near a crossover, so nothing rolls yet.

2. **Order.** Strategies are separated by sitting one contract month apart.
   Roll the front position first and it lands on the month the next strategy
   is still holding, and on a netting account they merge -- the separation
   fails exactly when it's under stress. So a roll is a coordinated sweep per
   symbol, **outermost expiry first**, which always moves into empty space.

The decision functions are pure so they can be tested without a broker.
"""
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple


@dataclass
class RollDecision:
    should_roll: bool
    reason: str


def volume_crossover(
    front_volume: Optional[float],
    next_volume: Optional[float],
    min_ratio: float = 1.0,
) -> RollDecision:
    """Roll when the next month's volume reaches `min_ratio` x the front's.

    Missing volume means "don't roll": rolling is two market orders per
    position, and doing that on a guess is worse than being a day late.
    """
    if front_volume is None or next_volume is None:
        return RollDecision(False, "no volume figures available, not rolling on a guess")
    if front_volume <= 0:
        return RollDecision(True, f"front month has no volume ({front_volume}), it's done")
    ratio = next_volume / front_volume
    if ratio >= min_ratio:
        return RollDecision(
            True,
            f"next month out-trading front: {next_volume:.0f} vs {front_volume:.0f} "
            f"(ratio {ratio:.2f} >= {min_ratio})",
        )
    return RollDecision(
        False,
        f"front month still leads: {front_volume:.0f} vs {next_volume:.0f} (ratio {ratio:.2f})",
    )


def roll_sequence(positions: Sequence[Tuple[Any, dict]]) -> List[Tuple[Any, dict]]:
    """Positions ordered outermost expiry first.

    This ordering is the whole safety property. Front-first would move a
    position into the contract the next strategy still occupies; outermost
    -first always vacates into empty space ahead of it.

    Positions with no recorded expiry sort last -- they can't be placed in the
    sequence safely, so they move once everything known has cleared.
    """
    return sorted(
        positions,
        key=lambda kv: (kv[1].get("contract_expiry") or ""),
        reverse=True,
    )
