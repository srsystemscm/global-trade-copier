"""Deliberate pyramiding, within stated limits.

Same-direction legs netting into one position is wanted here -- weekly
trend, then pullback, then breakout -- and it already works arithmetically,
because each leg's stop and close are sized to that leg. On a netted
position of 10 made of 4 and 6, the 4-leg's stop sells 4 and leaves a 6
position behind a correctly sized 6-stop.

What has to be bounded is how far it goes, and what must be refused outright
is the OPPOSING leg: a sell against a netted long does not open a short, it
reduces the long, while the engine would record a short and put its stop on
the wrong side of the entry.
"""
import asyncio

from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from tests.conftest_fakes import FakeBroker

ACCT = "ICM-44964870"


def _cfg(netting=None, route_netting=None, risk_pct=0.02):
    route = {"symbol": "XAUUSD", "target": "MGC", "month_slot": 0}
    if route_netting is not None:
        route["netting"] = route_netting
    cfg = {
        "mode": "autonomous",
        "atr_period": 14,
        "sizing": {"mode": "pct_risk", "risk_pct": risk_pct,
                   "capital_base": "equity", "rounding": "nearest_min1",
                   "max_risk_pct": 0.03},
        "routes": [route],
    }
    if netting is not None:
        cfg["netting"] = netting
    return cfg


def _sig(ticket, direction="BUY"):
    px = 3985.0
    stop = px - 112.0 if direction == "BUY" else px + 112.0
    return Signal(ticket=ticket, symbol="XAUUSD", action="OPEN",
                  direction=direction, lots=1.0, price=px, sl=stop,
                  account=ACCT, atr=74.6, magic=1001)


class _Broker(FakeBroker):
    def __init__(self, balance=250_000.0):
        super().__init__(balance=balance, quote=3985.0)

    async def get_price_history(self, symbol, period, month_slot=0,
                               contract_id=None, bar_size=None):
        from app.adapters.base import Bar
        return [Bar(high=4022.3, low=3947.7, close=3985.0) for _ in range(40)]

    async def get_quote(self, symbol, month_slot=0, contract_id=None):
        return 3985.0


def _run(signals, cfg):
    broker = _Broker()
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine("s", broker, queue, cfg)

    async def go():
        engine.start()
        try:
            for s in signals:
                await queue.put(s)
                for _ in range(120):
                    await asyncio.sleep(0.01)
                    if len(broker.opened) >= signals.index(s) + 1:
                        break
            await asyncio.sleep(0.15)
        finally:
            await engine.stop()

    asyncio.run(go())
    return broker, engine


# ---------------- same direction stacks ---------------------------------

def test_same_direction_legs_stack_by_default():
    """The current behaviour, kept: this is the pyramiding that is wanted."""
    broker, engine = _run([_sig(1), _sig(2)], _cfg())
    assert len(broker.opened) == 2
    assert len({k[1] for k in engine._positions}) == 2, "both legs tracked"


def test_each_leg_is_stopped_for_its_own_size():
    """Why netting is safe at all: the 4-leg's stop sells 4, not the whole
    netted position, so the remaining legs keep correctly sized stops."""
    broker, _ = _run([_sig(1), _sig(2)], _cfg())
    for fill in broker.opened:
        assert fill["sl"] is not None
        assert fill["size"] > 0
    # the two legs were sized independently, not as one doubled position
    assert broker.opened[0]["size"] == broker.opened[1]["size"]


# ---------------- the opposing leg is refused ---------------------------

def test_an_opposing_leg_is_refused_by_default():
    """A sell against a netted long reduces it rather than opening a short,
    while the engine would track a short with its stop above the entry."""
    broker, engine = _run([_sig(1, "BUY"), _sig(2, "SELL")], _cfg())
    assert len(broker.opened) == 1, "opened an opposing leg into a netted long"
    assert len(engine._positions) == 1


def test_an_opposing_leg_can_be_allowed_explicitly():
    broker, _ = _run([_sig(1, "BUY"), _sig(2, "SELL")],
                     _cfg(netting={"allow_opposing": True}))
    assert len(broker.opened) == 2


def test_the_first_leg_in_either_direction_is_fine():
    broker, _ = _run([_sig(1, "SELL")], _cfg())
    assert len(broker.opened) == 1


# ---------------- how many may stack ------------------------------------

def test_max_legs_caps_the_stack():
    """'two, maybe three systems' has to be expressible."""
    broker, _ = _run([_sig(1), _sig(2), _sig(3)], _cfg(netting={"max_legs": 2}))
    assert len(broker.opened) == 2


def test_max_legs_counts_only_the_same_side():
    broker, _ = _run([_sig(1, "BUY"), _sig(2, "SELL")],
                     _cfg(netting={"max_legs": 1, "allow_opposing": True}))
    assert len(broker.opened) == 2, "a short should not consume a long's slot"


# ---------------- how much they may risk together -----------------------

def test_the_aggregate_cap_is_across_legs_not_per_leg():
    """Three systems at 2% each is 6% on one symbol, and on correlated
    entries that is a single 6% bet -- which per-signal limits cannot see."""
    broker, _ = _run([_sig(1), _sig(2), _sig(3)],
                     _cfg(netting={"max_aggregate_risk_pct": 0.045}))
    # 2% a leg: two fit under 4.5%, the third would make 6%
    assert len(broker.opened) == 2


def test_a_generous_aggregate_cap_lets_them_all_through():
    broker, _ = _run([_sig(1), _sig(2), _sig(3)],
                     _cfg(netting={"max_aggregate_risk_pct": 0.10}))
    assert len(broker.opened) == 3


def test_an_unstopped_leg_makes_the_cap_unenforceable_and_says_so():
    """A leg with no stop has no bounded loss. Treating that as zero risk
    would let the cap be bypassed by the riskiest leg there is."""
    broker = _Broker()
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine(
        "s", broker, queue, _cfg(netting={"max_aggregate_risk_pct": 0.10}))

    async def go():
        engine.start()
        try:
            await queue.put(_sig(1))
            for _ in range(120):
                await asyncio.sleep(0.01)
                if broker.opened:
                    break
            # strip the stop off the tracked leg, as a restored position
            # without one would look
            for pos in engine._positions.values():
                pos["sl"] = None
            await queue.put(_sig(2))
            await asyncio.sleep(0.2)
        finally:
            await engine.stop()

    asyncio.run(go())
    assert len(broker.opened) == 1


# ---------------- scope --------------------------------------------------

def test_a_route_policy_overrides_the_slave_policy():
    """One pyramiding strategy on a slave where nothing else stacks."""
    broker, _ = _run([_sig(1), _sig(2), _sig(3)],
                     _cfg(netting={"max_legs": 1},
                          route_netting={"max_legs": 3}))
    assert len(broker.opened) == 3


def test_no_policy_means_no_limit_so_nothing_changes_silently():
    broker, _ = _run([_sig(1), _sig(2), _sig(3)], _cfg())
    assert len(broker.opened) == 3
