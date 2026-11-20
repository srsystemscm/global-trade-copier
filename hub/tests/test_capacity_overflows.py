"""A full book on one instrument is a bottleneck, not a verdict.

Changed on 2026-11-15. The aggregate-risk cap refused as
`overflowable=False`, which stopped the whole candidate list: if MGC was
at its ceiling, the engine would not then try the next name on that
route -- it skipped the signal.

The reasoning written beside it was that a sibling product on the same
underlying is the same bet, so falling through to another ticker would
defeat the cap while looking like compliance.

That misread what a candidate list is for. A route names several
instruments because of ACCESS and CAPACITY: a weekly and a daily system
stacking safely needs 2-3 futures contracts plus an ETF leg, and US
margin is running out around the same point anyway. The cap is a
bottleneck on ONE instrument, not a budget for the underlying.

It is not an escape hatch either, and that is the part worth pinning:
the next candidate gets its own aggregate check against its own
positions, so it is only taken if that book genuinely has room.
"""
import asyncio

from app.adapters.base import AccountSummary
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from app.routing import Route
from tests.conftest_fakes import FakeBroker

ACCOUNT = AccountSummary(balance=100_000.0, equity=100_000.0)


def _engine(netting):
    return AutonomousCopyEngine(
        "test-capacity", FakeBroker(), asyncio.Queue(),
        {"mode": "autonomous", "netting": netting,
         "sizing": {"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance"},
         "risk_management": {"poll_interval_seconds": 3600}},
    )


def _signal():
    return Signal(ticket=1, symbol="XAUUSD", action="OPEN", direction="BUY",
                  lots=1.0, price=2000.0, sl=1990.0, atr=10.0, magic=1001,
                  account="44964870")


class _Sized:
    def __init__(self, size):
        self.size = size


def _chosen(symbol, direction="BUY", quote=2000.0, sl=1990.0, size=1):
    return {"symbol": symbol, "direction": direction, "quote": quote,
            "sl": sl, "sizing_result": _Sized(size)}


def _hold(engine, symbol, direction="BUY", entry=2000.0, sl=1990.0, size=1):
    """Put a position on this engine's books, as an open leg would be."""
    key = ("44964870", len(engine._positions) + 100, symbol)
    engine._positions[key] = {"symbol": symbol, "direction": direction,
                              "entry_price": entry, "sl": sl, "size": size}


def test_a_full_book_on_one_instrument_lets_the_next_be_tried():
    """The arithmetic, so a later reader can check it rather than trust it:
    MGC ticks 0.1 at $1.00, the stop is 10 points away, so one contract
    risks (10 / 0.1) x 1.00 = $100. At size 8 a leg risks $800, and the
    1.5% ceiling on 100k is $1500 -- so one held leg plus this one is
    $1600 and over.

    The refusal still happens. What changed is that it no longer kills the
    whole route."""
    engine = _engine({"max_aggregate_risk_pct": 0.015})
    _hold(engine, "MGC", size=8)
    refusal = engine._netting_check(
        _signal(), Route(target="MGC"), _chosen("MGC", size=8), ACCOUNT)

    assert refusal is not None, "the ceiling did not bind at all"
    assert refusal.overflowable, (
        "a full book on MGC still kills the signal -- the next name on the "
        "route is exactly the answer to a capacity limit"
    )
    assert "trying the next instrument" in refusal.reason


def test_the_next_instrument_is_checked_rather_than_waved_through():
    """The reason this is not an escape hatch: the fallback gets its own
    aggregate check against its own book. An EMPTY book has room."""
    engine = _engine({"max_aggregate_risk_pct": 0.015})
    _hold(engine, "MGC", size=8)
    _hold(engine, "MGC", size=8)
    # Same route, different instrument, nothing held on it. QG ticks 0.005
    # at $12.50, so this leg risks (0.1 / 0.005) x 12.50 = $250 -- room.
    assert engine._netting_check(
        _signal(), Route(target="QG"), _chosen("QG", quote=3.0, sl=2.9, size=1),
        ACCOUNT) is None


def test_a_fallback_whose_own_book_is_full_is_still_refused():
    """Overflow is not a bypass: a second instrument already at its own
    ceiling refuses too."""
    engine = _engine({"max_aggregate_risk_pct": 0.015})
    _hold(engine, "MGC", size=16)   # $1600 held, already past the $1500 cap
    refusal = engine._netting_check(
        _signal(), Route(target="MGC"), _chosen("MGC", size=1), ACCOUNT)
    assert refusal is not None and refusal.overflowable


def test_an_opposing_position_is_still_a_wall():
    """The guard against over-correcting. Opposing legs are a RULE, not a
    capacity limit: the next product would simply be where the opposing
    position got opened instead."""
    engine = _engine({"max_aggregate_risk_pct": 0.015})
    _hold(engine, "MGC", direction="SELL")
    refusal = engine._netting_check(
        _signal(), Route(target="MGC"), _chosen("MGC", direction="BUY"), ACCOUNT)
    assert refusal is not None
    assert not refusal.overflowable, (
        "falling through to another instrument would open the opposing "
        "position somewhere else, which is the thing being refused"
    )


def test_a_leg_with_no_stop_is_still_a_wall():
    """The cap cannot be computed at all. Trying another instrument does
    not make the held leg measurable."""
    engine = _engine({"max_aggregate_risk_pct": 0.015})
    _hold(engine, "MGC", sl=None)
    refusal = engine._netting_check(
        _signal(), Route(target="MGC"), _chosen("MGC"), ACCOUNT)
    assert refusal is not None
    assert not refusal.overflowable
    assert "no" in refusal.reason and "stop" in refusal.reason


def test_the_leg_count_cap_was_already_a_bottleneck_and_stays_one():
    engine = _engine({"max_legs": 2})
    _hold(engine, "MGC")
    _hold(engine, "MGC")
    refusal = engine._netting_check(
        _signal(), Route(target="MGC"), _chosen("MGC"), ACCOUNT)
    assert refusal is not None and refusal.overflowable


def test_stacking_under_the_ceiling_is_untouched():
    """Two systems on one instrument inside the cap still copy -- the
    change must not have turned a bottleneck into a block."""
    engine = _engine({"max_aggregate_risk_pct": 0.06, "max_legs": 3})
    _hold(engine, "MGC")
    assert engine._netting_check(
        _signal(), Route(target="MGC"), _chosen("MGC"), ACCOUNT) is None
