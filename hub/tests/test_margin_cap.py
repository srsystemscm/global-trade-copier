"""Margin binds before risk does on some instruments.

One ETHUSDRR risks 2.95% of a 250k account but ties up $47,364 of initial
margin -- 19% of the account for a single position, and three netted legs
would be $142k against 250k. The aggregate RISK cap cannot see that: risk is
what the stop costs, margin is what the broker holds.

The figure compared is the broker's own initMarginAfter, not a change added
to a total tracked here: on a netting account what is held against a
combined position is not the sum of what each leg would cost alone.
"""
import asyncio

from app.adapters.base import AdapterRejectedError, MarginPreview
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from tests import engine_harness as harness
from tests.conftest_fakes import FakeBroker

ACCT = "ICM-44964870"
EQUITY = 250_000.0


class _Broker(FakeBroker):
    """Reports margin for a probe order, and records that it was asked."""

    def __init__(self, initial_after=47_364.0, raise_exc=None, omit=False):
        super().__init__(balance=EQUITY, quote=2043.5)
        self.initial_after = initial_after
        self.raise_exc = raise_exc
        self.omit = omit
        self.previews = 0

    async def get_price_history(self, symbol, period, month_slot=0,
                               contract_id=None, bar_size=None):
        from app.adapters.base import Bar
        return [Bar(high=2092.0, low=1995.0, close=2043.5) for _ in range(40)]

    async def get_quote(self, symbol, month_slot=0, contract_id=None):
        return 2043.5

    async def preview_margin(self, order):
        self.previews += 1
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.omit:
            return MarginPreview(initial_change=1.0, initial_after=None)
        return MarginPreview(initial_change=self.initial_after,
                             initial_after=self.initial_after)


class _NoPreview(_Broker):
    async def preview_margin(self, order):
        raise NotImplementedError("FakeBroker cannot preview margin")


def _cfg(netting=None):
    cfg = {
        "mode": "autonomous",
        "atr_period": 14,
        "sizing": {"mode": "pct_risk", "risk_pct": 0.02,
                   "capital_base": "equity", "rounding": "nearest_min1",
                   "max_risk_pct": 0.035},
        "routes": [{"symbol": "ETHUSD", "target": "ETHUSDRR", "month_slot": 0}],
    }
    if netting is not None:
        cfg["netting"] = netting
    return cfg


def _sig(ticket=1):
    return Signal(ticket=ticket, symbol="ETHUSD", action="OPEN",
                  direction="BUY", lots=1.0, price=2043.5, sl=1896.0,
                  account=ACCT, atr=98.3, magic=1001)


def _run(broker, cfg, signals=(1,)):
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine("s", broker, queue, cfg)

    async def go():
        engine.start()
        try:
            # Wait for the engine to finish rather than sleeping a fixed
            # span and hoping it was long enough. See tests/engine_harness.
            settled = await harness.feed(engine, [_sig(t) for t in signals],
                                         broker)
            assert settled, "the engine was still busy when the test gave up"
        finally:
            await engine.stop()

    asyncio.run(go())
    return broker


# ---------------- the cap --------------------------------------------

def test_no_cap_means_no_margin_round_trip():
    """whatIfOrder is a real call to the broker; it must not be made on
    every signal just in case."""
    broker = _run(_Broker(), _cfg())
    assert broker.opened, "nothing opened"
    assert broker.previews == 0, "asked the broker for margin with no cap set"


def test_a_position_inside_the_cap_is_allowed():
    """$47,364 against 250k is 19%, comfortably inside 25%."""
    broker = _run(_Broker(initial_after=47_364.0),
                  _cfg(netting={"max_margin_pct": 0.25}))
    assert broker.opened
    assert broker.previews == 1


def test_a_position_over_the_cap_is_refused():
    """The risk cap passes this: one contract risks 2.95% against a 3.5%
    gate. Only the margin figure stops it."""
    broker = _run(_Broker(initial_after=47_364.0),
                  _cfg(netting={"max_margin_pct": 0.15}))
    assert broker.opened == [], "opened a position over the margin ceiling"


def test_the_brokers_post_order_total_is_what_counts():
    """Not the change: on a netting account an order can offset what is
    held, so the after-figure can be lower than the change suggests."""
    broker = _Broker()

    async def preview(order):
        broker.previews += 1
        # a large addition, but the broker says the net result is small
        return MarginPreview(initial_change=90_000.0, initial_after=10_000.0)

    broker.preview_margin = preview
    _run(broker, _cfg(netting={"max_margin_pct": 0.10}))
    assert broker.opened, "judged on the change instead of the net result"


# ---------------- when it cannot be measured --------------------------

def test_an_adapter_that_cannot_preview_margin_refuses_rather_than_skips():
    """A cap that silently does nothing is worse than no cap: it reads as
    enforced."""
    broker = _run(_NoPreview(), _cfg(netting={"max_margin_pct": 0.25}))
    assert broker.opened == []


def test_a_missing_figure_refuses():
    broker = _run(_Broker(omit=True), _cfg(netting={"max_margin_pct": 0.25}))
    assert broker.opened == []


def test_a_broker_error_while_pricing_margin_refuses():
    broker = _run(_Broker(raise_exc=AdapterRejectedError("no margin data")),
                  _cfg(netting={"max_margin_pct": 0.25}))
    assert broker.opened == []


# ---------------- scope ------------------------------------------------

def test_a_route_can_set_its_own_margin_cap():
    cfg = _cfg(netting={"max_margin_pct": 0.05})
    cfg["routes"][0]["netting"] = {"max_margin_pct": 0.25}
    broker = _run(_Broker(initial_after=47_364.0), cfg)
    assert broker.opened, "the route's own cap did not take precedence"


def test_the_risk_cap_still_applies_independently():
    """Margin and risk are different limits; passing one is not passing the
    other."""
    cfg = _cfg(netting={"max_margin_pct": 0.90})
    cfg["sizing"]["max_risk_pct"] = 0.001       # far too tight for a contract
    broker = _run(_Broker(), cfg)
    assert broker.opened == []
