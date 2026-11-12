"""Risk against the account the operator means, not only the one reported.

A 250k account pays 50k in tax and the broker now reports 200k. The
strategies are meant to keep risking as if it were 250k: the withdrawal is
not a drawdown, and nothing about the strategies changed. The alternative --
resizing every EA on the master -- destroys the record of what each strategy
actually intended, on every account it copies to, and has to be undone by
hand when the money comes back.

So the correction lives once, on the slave, and applies to whatever
capital_base is in force.

What it must NOT touch is margin. Margin is held by the broker against real
equity; a cap measured against an inflated figure believes it has room it
does not have, and the consequence of that is a margin call rather than a
slightly large trade.
"""
import asyncio

from app.adapters.base import AdapterRejectedError, MarginPreview
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from app.sizing import resolve_balance_adjustment
from tests.conftest_fakes import FakeBroker

ACCT = "ICM-44964870"
REPORTED = 200_000.0       # what the broker says, after the tax payment
INTENDED = 250_000.0       # what the strategies were built against


class _Broker(FakeBroker):
    """Quotes a round number so the arithmetic in the assertions is plain."""

    def __init__(self, balance=REPORTED, margin_after=40_000.0):
        super().__init__(balance=balance, quote=100.0)
        self.margin_after = margin_after
        self.margin_equities = []

    async def get_price_history(self, symbol, period, month_slot=0,
                               contract_id=None, bar_size=None):
        from app.adapters.base import Bar
        # A 2.00 true range, so ATR is 2.00 and the master's 10.00 stop
        # is 5 ATR -- which lands the slave's stop back on 90.00 and
        # keeps the risk per share a round 10.00.
        return [Bar(high=101.0, low=99.0, close=100.0) for _ in range(40)]

    async def get_quote(self, symbol, month_slot=0, contract_id=None):
        return 100.0

    async def preview_margin(self, order):
        return MarginPreview(initial_change=self.margin_after,
                             initial_after=self.margin_after)


def _cfg(adjustment=None, capital_base="balance", risk_pct=0.02,
         netting=None, route_sizing=None):
    route = {"symbol": "SPY", "target": "SPY", "month_slot": 0}
    if route_sizing is not None:
        route["sizing"] = route_sizing
    cfg = {
        "mode": "autonomous",
        "atr_period": 14,
        "sizing": {"mode": "pct_risk", "risk_pct": risk_pct,
                   "capital_base": capital_base, "rounding": "floor"},
        "routes": [route],
    }
    if adjustment is not None:
        cfg["balance_adjustment"] = adjustment
    if netting is not None:
        cfg["netting"] = netting
    return cfg


def _sig(ticket=1):
    # a 10.00 stop on a 100.00 entry: 10 dollars of risk per share, so
    # 2% of 200k is 400 shares and 2% of 250k is 500
    return Signal(ticket=ticket, symbol="SPY", action="OPEN", direction="BUY",
                  lots=1.0, price=100.0, sl=90.0, account=ACCT, atr=2.0,
                  magic=1001)


def _run(broker, cfg, signals=(1,)):
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine("s", broker, queue, cfg)

    async def go():
        engine.start()
        try:
            for t in signals:
                await queue.put(_sig(t))
                await asyncio.sleep(0.12)
        finally:
            await engine.stop()

    asyncio.run(go())
    return broker, engine


# ---------------- reading the field ------------------------------------

def test_absent_means_zero_so_nothing_resizes_silently():
    assert resolve_balance_adjustment({}) == 0.0
    assert resolve_balance_adjustment({"balance_adjustment": None}) == 0.0


def test_it_is_read_from_the_slaves_top_level():
    assert resolve_balance_adjustment({"balance_adjustment": 50_000}) == 50_000.0


# ---------------- what it does to size ---------------------------------

def test_without_it_the_reported_balance_is_what_is_risked():
    broker, _ = _run(_Broker(), _cfg())
    assert broker.opened[0]["size"] == 400.0


def test_it_sizes_against_the_intended_balance():
    """The whole point: 2% of 250k, not 2% of the 200k that is there."""
    broker, _ = _run(_Broker(), _cfg(adjustment=50_000))
    assert broker.opened[0]["size"] == 500.0


def test_a_negative_adjustment_reduces_size():
    """It is signed. Holding back capital that is in the account but not
    meant to be risked is the same mechanism."""
    broker, _ = _run(_Broker(), _cfg(adjustment=-100_000))
    assert broker.opened[0]["size"] == 200.0


def test_it_applies_to_an_equity_capital_base_too():
    """It is a correction to the account, not to one capital_base. Risking
    off equity is the common setting on these slaves."""
    broker, _ = _run(_Broker(), _cfg(adjustment=50_000, capital_base="equity"))
    assert broker.opened[0]["size"] == 500.0


def test_a_route_does_not_have_to_carry_it():
    """A route overriding its sizing block must not drop the adjustment --
    that is the silent failure that would put one strategy back on the
    uncorrected balance."""
    broker, _ = _run(_Broker(), _cfg(adjustment=50_000,
                                     route_sizing={"risk_pct": 0.01}))
    assert broker.opened[0]["size"] == 250.0


# ---------------- when it cannot be honoured ---------------------------

def test_an_adjustment_that_empties_the_account_refuses():
    """Falling back to the real balance would be worse than refusing: the
    operator asked for a specific capital base and silently getting a
    different one is how an account gets sized wrong without anyone
    noticing."""
    broker, _ = _run(_Broker(), _cfg(adjustment=-REPORTED))
    assert broker.opened == []


def test_an_adjustment_past_zero_refuses():
    broker, _ = _run(_Broker(), _cfg(adjustment=-500_000))
    assert broker.opened == []


# ---------------- margin is measured on the real account ---------------

def test_the_margin_cap_uses_the_brokers_own_equity():
    """40k of margin is 20% of the 200k that exists and 16% of the 250k
    being risked against. With an 18% cap it must be refused: the broker
    holds margin against real equity, and believing otherwise is a margin
    call rather than a large trade."""
    broker, _ = _run(_Broker(margin_after=40_000.0),
                     _cfg(adjustment=50_000, netting={"max_margin_pct": 0.18}))
    assert broker.opened == [], "margin was measured against adjusted equity"


def test_the_margin_cap_still_passes_what_genuinely_fits():
    broker, _ = _run(_Broker(margin_after=30_000.0),
                     _cfg(adjustment=50_000, netting={"max_margin_pct": 0.18}))
    assert broker.opened, "refused a position inside the real margin ceiling"


# ---------------- risk limits follow the adjusted figure ---------------

def test_the_aggregate_risk_cap_follows_the_adjusted_capital():
    """Risk limits and risk sizing have to read the same capital, or a leg
    sized to 2% would measure as 2.5% against its own ceiling and the two
    numbers would never agree."""
    broker, _ = _run(
        _Broker(), _cfg(adjustment=50_000,
                        netting={"max_aggregate_risk_pct": 0.045}),
        signals=(1, 2, 3))
    # 2% of 250k a leg: two fit under 4.5%, the third would make 6%
    assert len(broker.opened) == 2
