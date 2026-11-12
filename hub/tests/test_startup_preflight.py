"""Check route targets against the broker before a signal needs them.

Two live failures, neither caught by anything the hub did:

  MNG is a real CME contract that IBKR does not carry. It sat in a tier as
  the cheaper alternative to NG, so it would have failed at exactly the
  moment it was wanted, with a position already decided on.

  SIL resolved to SIZ6 -- the 5,000 ounce silver contract -- because
  ContFuture ignores tradingClass. It resolved, qualified and quoted 49.75
  perfectly happily, and would have filled at five times the intended size.
  Only the multiplier said anything was wrong.

They are different in kind and are handled differently. An unresolvable
target is loud at signal time, so it is reported and the route is left
alone; one dead ticker must not stop the other nineteen from working. A
multiplier that disagrees with its spec is silent, and would mis-size every
position on that instrument, so that target is blocked.
"""
import asyncio

from app.adapters.base import ContractInfo
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from app.routing import configured_targets
from tests.conftest_fakes import FakeBroker

ACCT = "ICM-44964870"


# ---------------- enumerating what a config can reach -------------------

def test_a_plain_target_is_enumerated():
    assert configured_targets({"routes": [{"symbol": "XAUUSD", "target": "MGC"}]}) \
        == [("MGC", 0)]


def test_a_preference_list_enumerates_every_candidate():
    """The MNG case: the fallback is the one that has to exist, because it
    is only reached when the first choice cannot be traded."""
    cfg = {"routes": [{"symbol": "XNGUSD", "target": ["NG", "QG"]}]}
    assert configured_targets(cfg) == [("NG", 0), ("QG", 0)]


def test_a_fan_out_enumerates_every_destination():
    cfg = {"routes": [{"symbol": "XAUUSD", "targets": ["MGC", "GLD"]}]}
    assert configured_targets(cfg) == [("MGC", 0), ("GLD", 0)]


def test_the_month_slot_travels_with_the_target():
    cfg = {"routes": [{"symbol": "XAUUSD", "target": "MGC", "month_slot": 2}]}
    assert configured_targets(cfg) == [("MGC", 2)]


def test_the_same_target_is_only_checked_once():
    """SP500 and US500 both route to MES; two round trips for one answer."""
    cfg = {"routes": [{"symbol": "SP500", "target": "MES"},
                      {"symbol": "US500", "target": "MES"}]}
    assert configured_targets(cfg) == [("MES", 0)]


def test_the_same_target_in_two_slots_is_two_checks():
    cfg = {"routes": [{"symbol": "A", "target": "MGC", "month_slot": 0},
                      {"symbol": "B", "target": "MGC", "month_slot": 1}]}
    assert configured_targets(cfg) == [("MGC", 0), ("MGC", 1)]


def test_a_direction_specific_route_is_still_enumerated():
    """Startup has no signal, so a target only a SELL can reach still has
    to resolve before that SELL arrives."""
    cfg = {"routes": [{"symbol": "XAUUSD", "target": "GLL",
                       "direction": "SELL", "target_direction": "BUY"}]}
    assert configured_targets(cfg) == [("GLL", 0)]


def test_no_routes_means_nothing_to_check():
    assert configured_targets({}) == []
    assert configured_targets({"routes": []}) == []


# ---------------- the preflight itself ----------------------------------

class _Broker(FakeBroker):
    """Resolves targets, with per-symbol multipliers and failures."""

    def __init__(self, multipliers=None, failures=()):
        super().__init__(balance=1_000_000.0, quote=4050.0)
        self.multipliers = multipliers or {}
        self.failures = set(failures)
        self.resolved = []
        self.quotes = 0

    async def resolve_contract(self, symbol, month_slot=0):
        self.resolved.append((symbol, month_slot))
        if symbol in self.failures:
            from app.adapters.base import AdapterRejectedError
            raise AdapterRejectedError(f"no security definition for {symbol}")
        return ContractInfo(symbol=symbol, contract_id=1, local_symbol=symbol + "Z6",
                            expiry="20261229",
                            multiplier=self.multipliers.get(symbol))

    async def get_quote(self, symbol, month_slot=0, contract_id=None):
        self.quotes += 1
        return self.quote_price

    async def get_price_history(self, symbol, period, month_slot=0,
                               contract_id=None, bar_size=None):
        from app.adapters.base import Bar
        return [Bar(high=4070.0, low=4030.0, close=4050.0) for _ in range(40)]


class _NoContracts(_Broker):
    async def resolve_contract(self, symbol, month_slot=0):
        raise NotImplementedError("this adapter has no contract months")


def _engine(broker, target="MGC"):
    cfg = {
        "mode": "autonomous",
        "atr_period": 14,
        "sizing": {"mode": "pct_risk", "risk_pct": 0.02,
                   "capital_base": "balance", "rounding": "nearest_min1"},
        "routes": [{"symbol": "XAUUSD", "target": target, "month_slot": 0}],
    }
    return AutonomousCopyEngine("s", broker, asyncio.Queue(), cfg)


def _preflight(broker, target="MGC"):
    engine = _engine(broker, target)
    asyncio.run(engine._preflight_routes())
    return engine


# MGC's spec says 10.0 per point.

def test_a_matching_multiplier_blocks_nothing():
    engine = _preflight(_Broker(multipliers={"MGC": 10.0}))
    assert engine.blocked_targets == {}


def test_a_mismatched_multiplier_blocks_the_target():
    """The silver case. A 100.0 multiplier where the spec says 10.0 sizes
    every position by a tenth of the right contract value."""
    engine = _preflight(_Broker(multipliers={"MGC": 100.0}))
    assert "MGC" in engine.blocked_targets
    assert "100" in engine.blocked_targets["MGC"]
    assert "10" in engine.blocked_targets["MGC"]


def test_a_target_that_will_not_resolve_is_not_blocked():
    """The MNG case is loud at signal time: an error, a refusal, a log line.
    Blocking the route as well would mean one dead ticker could stop routes
    that work, and this check has to be safe to leave switched on."""
    engine = _preflight(_Broker(failures=["MGC"]))
    assert engine.blocked_targets == {}


def test_a_broker_without_contract_months_blocks_nothing():
    engine = _preflight(_NoContracts())
    assert engine.blocked_targets == {}


def test_a_target_without_a_spec_is_left_alone():
    """ETFs have no contract spec, so there is nothing for a multiplier to
    disagree with."""
    engine = _preflight(_Broker(multipliers={"SPXL": 1.0}), target="SPXL")
    assert engine.blocked_targets == {}


def test_nothing_is_quoted():
    """Most ETF routes are shut overnight, so quote failures here would mean
    nothing -- and holding 35 market-data lines at startup is how MES came
    back empty twice."""
    broker = _Broker(multipliers={"MGC": 10.0})
    _preflight(broker)
    assert broker.quotes == 0


def test_every_target_is_checked_even_after_one_fails():
    broker = _Broker(failures=["NG"], multipliers={"QG": 2500.0})
    engine = _engine(broker)
    engine.slave_config["routes"] = [
        {"symbol": "XNGUSD", "target": ["NG", "QG"], "month_slot": 0}]
    asyncio.run(engine._preflight_routes())
    assert broker.resolved == [("NG", 0), ("QG", 0)]


def test_it_always_finishes_even_when_it_breaks():
    """A preflight that died must not be the thing that stops the copier
    from trading."""
    broker = _Broker()

    async def explode(symbol, month_slot=0):
        raise RuntimeError("broker connection dropped mid-preflight")

    engine = _engine(broker)
    engine.adapter.resolve_contract = explode
    asyncio.run(engine._preflight_routes())
    assert engine._preflight_done.is_set()


# ---------------- what a block does to a signal -------------------------

def _sig(ticket=1):
    return Signal(ticket=ticket, symbol="XAUUSD", action="OPEN", direction="BUY",
                  lots=1.0, price=4050.0, sl=3930.0, account=ACCT, atr=40.0,
                  magic=1001)


def _run(broker, routes, signals=(1,)):
    cfg = {
        "mode": "autonomous",
        "atr_period": 14,
        "sizing": {"mode": "pct_risk", "risk_pct": 0.02,
                   "capital_base": "balance", "rounding": "nearest_min1",
                   "max_risk_pct": 0.05},
        "routes": routes,
    }
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine("s", broker, queue, cfg)

    async def go():
        engine.start()
        try:
            for t in signals:
                await queue.put(_sig(t))
                await asyncio.sleep(0.2)
        finally:
            await engine.stop()

    asyncio.run(go())
    return broker, engine


def test_a_blocked_target_refuses_the_signal():
    broker, engine = _run(
        _Broker(multipliers={"MGC": 100.0}),
        [{"symbol": "XAUUSD", "target": "MGC", "month_slot": 0}])
    assert engine.blocked_targets, "preflight did not run before the signal"
    assert broker.opened == []


def test_a_tier_steps_past_a_blocked_candidate():
    """What the preference list is for. MGC disagrees with its spec, so the
    signal goes to the next candidate rather than nowhere."""
    broker, _ = _run(
        _Broker(multipliers={"MGC": 100.0, "GC": 100.0}),
        [{"symbol": "XAUUSD", "target": ["MGC", "GC"], "month_slot": 0}])
    assert [o["symbol"] for o in broker.opened] == ["GC"]


def test_a_correct_target_still_trades():
    broker, _ = _run(
        _Broker(multipliers={"MGC": 10.0}),
        [{"symbol": "XAUUSD", "target": "MGC", "month_slot": 0}])
    assert [o["symbol"] for o in broker.opened] == ["MGC"]


def test_a_signal_does_not_overtake_the_preflight():
    """A signal arriving in the first seconds after startup is exactly the
    one the multiplier check has to cover."""
    broker = _Broker(multipliers={"MGC": 100.0})
    slow = asyncio.Event()
    inner = broker.resolve_contract

    async def resolve_slowly(symbol, month_slot=0):
        await asyncio.sleep(0.25)
        slow.set()
        return await inner(symbol, month_slot)

    broker.resolve_contract = resolve_slowly

    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine("s", broker, queue, {
        "mode": "autonomous", "atr_period": 14,
        "sizing": {"mode": "pct_risk", "risk_pct": 0.02,
                   "capital_base": "balance", "rounding": "nearest_min1"},
        "routes": [{"symbol": "XAUUSD", "target": "MGC", "month_slot": 0}]})

    async def go():
        engine.start()
        try:
            # straight away, while the preflight is still resolving
            await queue.put(_sig(1))
            await asyncio.sleep(0.8)
        finally:
            await engine.stop()

    asyncio.run(go())
    assert slow.is_set(), "the preflight never got to finish"
    assert engine.blocked_targets, "the block was not recorded"
    assert broker.opened == [], "sized before preflight had finished"


def test_the_wait_is_bounded():
    """A preflight that hangs must not hold signals for ever."""
    broker = _Broker(multipliers={"MGC": 10.0})

    async def never(symbol, month_slot=0):
        await asyncio.Event().wait()

    broker.resolve_contract = never
    cfg_routes = [{"symbol": "XAUUSD", "target": "MGC", "month_slot": 0}]
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine("s", broker, queue, {
        "mode": "autonomous", "atr_period": 14,
        "sizing": {"mode": "pct_risk", "risk_pct": 0.02,
                   "capital_base": "balance", "rounding": "nearest_min1"},
        "routes": cfg_routes, "preflight_wait_seconds": 0.3})

    async def go():
        engine.start()
        try:
            await queue.put(_sig(1))
            await asyncio.sleep(1.0)
        finally:
            await engine.stop()

    asyncio.run(go())
    assert broker.opened, "a hanging preflight stopped the copier trading"
