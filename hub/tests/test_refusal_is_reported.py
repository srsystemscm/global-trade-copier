"""A leg that did not happen has to say so.

Observed on 2026-11-15. IBKR's usfuture market-data farm went inactive
mid-session; every later ETH signal produced an mt5-icm-1 leg and no IBKR
leg. The ops hub's Copier tab showed a master order with nothing beside
it, and the question "did the copier decline, or was it never asked?"
could not be answered from the hub at all -- the refusal existed only as
a WARNING in a log file on another machine.

The engine was behaving correctly. It had no way to say so.

Two paths reached `chosen is None` and only one of them reported:

  netting refused it      -> netting_refused, emitted
  nothing could be priced -> nothing at all

The second is the common one, because it covers every ordinary reason a
market cannot be traded right now: no quote, no history, a size that
rounded to nothing.
"""
import asyncio

import pytest

from app.autonomous_copy_engine import AutonomousCopyEngine
from app.events import EventBus
from app.models import Signal
from tests.conftest_fakes import FakeBroker

SLAVE = "test-refusal"


def _signal(**kw):
    base = dict(ticket=99, symbol="ETHUSD", action="OPEN", direction="BUY",
                lots=5.39, price=1807.78, sl=1632.45, atr=18.0, magic=1001,
                account="44964870")
    base.update(kw)
    return Signal(**base)


def _engine(broker, routes=None):
    config = {
        "mode": "autonomous",
        "atr_period": 14,
        "routes": routes if routes is not None else [
            {"symbol": "ETHUSD", "target": "ETHUSDRR",
             "sizing": {"mode": "pct_risk", "risk_pct": 0.01,
                        "capital_base": "balance"}},
        ],
        "risk_management": {"poll_interval_seconds": 3600},
    }
    return AutonomousCopyEngine(SLAVE, broker, asyncio.Queue(), config)


def _collect(engine, signal):
    """Run one OPEN and return the events the engine emitted."""
    seen = []

    async def go():
        bus = EventBus()
        engine.events = bus
        q = bus.subscribe()
        await engine._handle_open(signal)
        while not q.empty():
            seen.append(q.get_nowait())

    asyncio.run(go())
    return seen


def test_a_target_that_cannot_be_priced_is_reported():
    """The exact shape of the ETH case: the broker cannot quote, so no
    leg is possible, and the hub must hear about it."""
    broker = FakeBroker()

    async def _no_quote(symbol, month_slot=0, **kw):
        from app.adapters.base import AdapterError
        raise AdapterError("market data farm inactive")

    broker.get_quote = _no_quote
    events = _collect(_engine(broker), _signal())

    refusals = [e for e in events if e.get("type") == "signal_refused"]
    assert refusals, "a leg that did not happen reported nothing"
    assert "no quote" in refusals[0]["reason"]
    assert refusals[0].get("slave_symbol") == "ETHUSDRR", (
        "a refusal naming no instrument cannot be told from a refusal of "
        "the whole signal"
    )


def test_the_reason_travels_rather_than_staying_in_the_log():
    """The log is on the copier's machine; the person asking is looking
    at the hub."""
    broker = FakeBroker()

    async def _no_history(symbol, period, month_slot=0, **kw):
        from app.adapters.base import AdapterError
        raise AdapterError("no bars for ETHUSDRR")

    broker.get_price_history = _no_history
    events = _collect(_engine(broker), _signal())

    refusals = [e for e in events if e.get("type") == "signal_refused"]
    assert refusals, "a missing price history reported nothing"
    assert "ETHUSDRR" in refusals[0]["reason"] or "history" in refusals[0]["reason"]


def test_a_signal_that_copies_cleanly_emits_no_refusal():
    """The guard against the obvious over-correction: reporting a refusal
    on every signal would make the type worthless."""
    events = _collect(_engine(FakeBroker()), _signal())
    assert not [e for e in events if e.get("type") == "signal_refused"]
