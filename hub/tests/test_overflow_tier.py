"""A refused candidate falls through to the next one.

The candidate list was built for cost ("one NG beats ten QG, if the account
can carry a whole NG"), and the netting and margin checks ran after a
candidate had already been chosen -- so a refusal refused the whole signal
and the rest of the list was never reached.

That is wrong for the case the list now has to serve. BTC runs BITU and
SBIT as the matched +/-2x pair, with BITX and BTCZ behind them so that a
third same-direction signal has somewhere to go rather than stacking onto a
netted position that is already at its leg limit. A book full on BITU is
exactly when the overflow product earns its place.

Both checks therefore move inside the loop. Only an exhausted list refuses
the signal, and a signal turned away by policy is still reported as a
refusal rather than as an arithmetic failure.
"""
import asyncio

from app.adapters.base import MarginPreview
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from tests import engine_harness as harness
from tests.conftest_fakes import FakeBroker

ACCT = "ICM-44964870"


class _Broker(FakeBroker):
    def __init__(self, margin_by_symbol=None):
        super().__init__(balance=250_000.0, quote=100.0)
        # symbol -> initMarginAfter, so one candidate can be too expensive
        # while the next is not
        self.margin_by_symbol = margin_by_symbol or {}
        self.previewed = []

    async def get_price_history(self, symbol, period, month_slot=0,
                               contract_id=None, bar_size=None):
        from app.adapters.base import Bar
        return [Bar(high=101.0, low=99.0, close=100.0) for _ in range(40)]

    async def get_quote(self, symbol, month_slot=0, contract_id=None):
        return 100.0

    async def preview_margin(self, order):
        self.previewed.append(order.symbol)
        after = self.margin_by_symbol.get(order.symbol, 1_000.0)
        return MarginPreview(initial_change=after, initial_after=after)


def _cfg(target, netting=None):
    cfg = {
        "mode": "autonomous",
        "atr_period": 14,
        "sizing": {"mode": "pct_risk", "risk_pct": 0.02,
                   "capital_base": "balance", "rounding": "floor"},
        "routes": [{"symbol": "BTCUSD", "target": target, "month_slot": 0}],
    }
    if netting is not None:
        cfg["netting"] = netting
    return cfg


def _sig(ticket, direction="BUY"):
    stop = 90.0 if direction == "BUY" else 110.0
    return Signal(ticket=ticket, symbol="BTCUSD", action="OPEN",
                  direction=direction, lots=1.0, price=100.0, sl=stop,
                  account=ACCT, atr=2.0, magic=1001)


def _run(broker, cfg, signals):
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine("s", broker, queue, cfg)
    events = []
    engine._emit = lambda name, **kw: events.append((name, kw)) or _noop()

    async def go():
        engine.start()
        try:
            settled = await harness.feed(engine, signals, broker)
            assert settled, "the engine was still busy when the test gave up"
        finally:
            await engine.stop()

    asyncio.run(go())
    return broker, engine, events


async def _noop():
    return None


# ---------------- netting sends the signal to the next product ----------

def test_a_full_leg_limit_overflows_to_the_next_candidate():
    """Two legs on BITU, then the third goes to BITX instead of nowhere."""
    broker, _, _ = _run(_Broker(), _cfg(["BITU", "BITX"],
                                        netting={"max_legs": 2}),
                        [_sig(1), _sig(2), _sig(3)])
    symbols = [o["symbol"] for o in broker.opened]
    assert symbols == ["BITU", "BITU", "BITX"], symbols


def test_the_overflow_product_has_its_own_capacity():
    broker, _, _ = _run(_Broker(), _cfg(["BITU", "BITX"],
                                        netting={"max_legs": 1}),
                        [_sig(1), _sig(2), _sig(3)])
    symbols = [o["symbol"] for o in broker.opened]
    assert symbols == ["BITU", "BITX"], symbols
    # the third has nowhere left to go, and is refused rather than stacked
    assert len(symbols) == 2


def test_the_first_candidate_is_still_preferred():
    """Overflow is a fallback, not a round robin: cost is why the list is
    ordered in the first place."""
    broker, _, _ = _run(_Broker(), _cfg(["BITU", "BITX"]),
                        [_sig(1), _sig(2)])
    assert [o["symbol"] for o in broker.opened] == ["BITU", "BITU"]


def test_an_opposing_signal_is_not_sent_to_the_overflow_product():
    """allow_opposing is false because a sell into a netted long reduces it
    while being tracked as a short. Routing that to the next product would
    open the position the policy exists to prevent, on a different ticker."""
    broker, _, _ = _run(_Broker(), _cfg(["BITU", "BITX"]),
                        [_sig(1, "BUY"), _sig(2, "SELL")])
    assert [o["symbol"] for o in broker.opened] == ["BITU"]


# ---------------- margin sends it on too --------------------------------

def test_a_margin_refusal_overflows_to_the_next_candidate():
    """The point of a cheaper alternative. 60k of margin against a 20%
    ceiling on 250k is refused; 30k is not."""
    broker, _, _ = _run(
        _Broker(margin_by_symbol={"BITU": 60_000.0, "BITX": 30_000.0}),
        _cfg(["BITU", "BITX"], netting={"max_margin_pct": 0.20}),
        [_sig(1)])
    assert [o["symbol"] for o in broker.opened] == ["BITX"]
    assert broker.previewed == ["BITU", "BITX"], broker.previewed


def test_margin_is_only_asked_about_when_a_cap_exists():
    """whatIfOrder is a round trip per candidate, so a list must not turn
    every signal into several of them."""
    broker, _, _ = _run(_Broker(), _cfg(["BITU", "BITX"]), [_sig(1)])
    assert broker.previewed == []


# ---------------- an exhausted list ------------------------------------

def test_nothing_opens_when_every_candidate_is_refused():
    broker, _, _ = _run(_Broker(), _cfg(["BITU", "BITX"],
                                        netting={"max_legs": 1}),
                        [_sig(1), _sig(2), _sig(3)])
    assert len(broker.opened) == 2


def test_an_exhausted_list_is_reported_as_a_refusal():
    """Turned away by policy, not by arithmetic -- which is the distinction
    worth seeing in the ledger afterwards."""
    _, _, events = _run(_Broker(), _cfg(["BITU"], netting={"max_legs": 1}),
                        [_sig(1), _sig(2)])
    names = [n for n, _ in events]
    assert "netting_refused" in names


def test_a_single_candidate_route_behaves_exactly_as_before():
    broker, _, _ = _run(_Broker(), _cfg("BITU", netting={"max_legs": 1}),
                        [_sig(1), _sig(2)])
    assert [o["symbol"] for o in broker.opened] == ["BITU"]


# ---------------- a rule is not a capacity limit ------------------------

def test_the_aggregate_risk_cap_does_not_overflow():
    """The subtle one. The cap exists because several systems on one symbol
    are a single correlated bet, and BITU and BITX track the same
    underlying. Overflowing to escape it would defeat the cap while looking
    exactly like compliance."""
    broker, _, _ = _run(
        _Broker(), _cfg(["BITU", "BITX"],
                        netting={"max_aggregate_risk_pct": 0.045}),
        [_sig(1), _sig(2), _sig(3)])
    symbols = [o["symbol"] for o in broker.opened]
    # 2% a leg: two fit under 4.5%, and the third must not escape to BITX
    assert symbols == ["BITU", "BITU"], symbols


def test_an_unmeasurable_cap_does_not_overflow():
    """It will be just as unmeasurable on the next candidate, so trying the
    list is N round trips to the same answer."""
    broker = _Broker()

    async def no_preview(order):
        broker.previewed.append(order.symbol)
        raise NotImplementedError("this adapter cannot preview margin")

    broker.preview_margin = no_preview
    broker, _, _ = _run(broker, _cfg(["BITU", "BITX"],
                                     netting={"max_margin_pct": 0.20}),
                        [_sig(1)])
    assert broker.opened == []
    assert broker.previewed == ["BITU"], broker.previewed


def test_max_legs_and_margin_are_the_overflowable_pair():
    """Stated as a property so the split is visible in one place rather than
    only in behaviour: capacity overflows, rules do not."""
    from app.autonomous_copy_engine import Refusal
    assert Refusal("x").overflowable is False, "refusals default to strict"
    assert Refusal("x", overflowable=True).overflowable is True
