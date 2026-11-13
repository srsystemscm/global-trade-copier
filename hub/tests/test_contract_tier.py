"""A route can name several contracts for one position, largest first.

Commission is charged per contract, so one NG is far cheaper than ten MNG
for the same exposure. The right contract is the LARGEST whose single unit
the account can carry -- which depends on the account, so it cannot be
settled when the route is written. A 250k account at 1% takes NG; the same
route on a 50k account steps down to MNG by itself.

The sharp edge is that `targets` already meant fan-out: one position on EACH
instrument. Singular `target` as a list means alternatives for ONE position.
Confusing them would either double the exposure or produce none.
"""
import asyncio

import pytest

from app.autonomous_copy_engine import AutonomousCopyEngine
from app.models import Signal
from app.routing import resolve_routes
from tests.conftest_fakes import FakeBroker

ACCT = "ICM-44964870"


def _signal(symbol="XNGUSD", **kw):
    base = dict(ticket=5001, symbol=symbol, action="OPEN", direction="BUY",
                lots=1.0, price=4.10, sl=3.77, account=ACCT, atr=0.22,
                magic=1001)
    base.update(kw)
    return Signal(**base)


# ---------------- parsing: one position or several ----------------------

def test_a_target_list_is_one_position_with_alternatives():
    cfg = {"routes": [{"symbol": "XNGUSD", "target": ["NG", "MNG"]}]}
    routes = resolve_routes(cfg, _signal())
    assert len(routes) == 1, "a preference list must not fan out into two positions"
    assert routes[0].candidates == ["NG", "MNG"]
    assert routes[0].target == "NG", "target stays the first candidate"


def test_a_targets_list_is_still_several_positions():
    """Pre-existing meaning, which must not change: fan-out."""
    cfg = {"routes": [{"symbol": "XNGUSD", "targets": ["NG", "MNG"]}]}
    routes = resolve_routes(cfg, _signal())
    assert len(routes) == 2
    assert [r.target for r in routes] == ["NG", "MNG"]
    assert [r.candidates for r in routes] == [["NG"], ["MNG"]]


def test_a_plain_string_target_is_unchanged():
    cfg = {"routes": [{"symbol": "XNGUSD", "target": "MNG"}]}
    routes = resolve_routes(cfg, _signal())
    assert len(routes) == 1
    assert routes[0].target == "MNG"
    assert routes[0].candidates == ["MNG"]


def test_giving_both_is_refused():
    """Three names would be ambiguous between three positions and three
    fallbacks, and guessing either way is a real-money mistake."""
    cfg = {"routes": [{"symbol": "XNGUSD", "target": ["NG", "MNG"],
                       "targets": ["NG", "MNG"]}]}
    with pytest.raises(ValueError, match="one or the other"):
        resolve_routes(cfg, _signal())


# ---------------- selection: largest the account can carry --------------

class _Broker(FakeBroker):
    """Quotes and bars per symbol, so each candidate sizes differently."""

    def __init__(self, balance):
        super().__init__(balance=balance)
        self._px = {"NG": 4.10, "MNG": 4.10}

    async def get_quote(self, symbol, month_slot=0, contract_id=None):
        return self._px[symbol]

    async def get_price_history(self, symbol, period, month_slot=0,
                               contract_id=None, bar_size=None):
        from app.adapters.base import Bar
        # ~0.22 ATR on nat gas: a 1.5x stop is 0.33, which is $3,300 on NG
        # and $330 on MNG.
        return [Bar(high=4.21, low=3.99, close=4.10) for _ in range(40)]


def _run(balance, gate=0.03, candidates=("NG", "MNG")):
    cfg = {
        "mode": "autonomous",
        "atr_period": 14,
        "sizing": {"mode": "fixed_contracts", "contracts": 1,
                   "rounding": "nearest_min1", "max_risk_pct": gate},
        "routes": [{"symbol": "XNGUSD", "target": list(candidates),
                    "month_slot": 0}],
    }
    broker = _Broker(balance)
    queue: asyncio.Queue = asyncio.Queue()
    engine = AutonomousCopyEngine("s", broker, queue, cfg)

    async def go():
        engine.start()
        try:
            await queue.put(_signal())
            for _ in range(200):
                if broker.opened:
                    break
                await asyncio.sleep(0.01)
        finally:
            await engine.stop()

    asyncio.run(go())
    return broker


def test_a_large_account_takes_the_full_size_contract():
    """One NG risks ~$3,300. On 250k at a 3% gate that is 1.3%, so the
    cheaper contract is affordable and should be chosen."""
    broker = _run(250_000)
    assert broker.opened, "nothing opened"
    assert broker.opened[0]["symbol"] == "NG"


def test_a_small_account_steps_down_by_itself():
    """Same route, 50k account: one NG is 6.6%, over the gate, so it falls
    through to MNG at 0.66%. No config change, no second route."""
    broker = _run(50_000)
    assert broker.opened, "nothing opened"
    assert broker.opened[0]["symbol"] == "MNG"


def test_order_is_the_declared_preference_not_inferred_from_size():
    """There are reasons besides size to prefer a contract -- liquidity,
    hours, settlement -- so the list is obeyed as written."""
    broker = _run(250_000, candidates=("MNG", "NG"))
    assert broker.opened[0]["symbol"] == "MNG"


def test_nothing_trades_when_no_candidate_fits():
    """A tier list must not become a way to trade something unaffordable."""
    broker = _run(5_000)
    assert broker.opened == []


def test_a_tighter_gate_moves_the_boundary():
    """The gate is the whole mechanism: at 1% even 250k cannot carry one NG
    at $3,300, so the same account steps down."""
    assert _run(250_000, gate=0.01).opened[0]["symbol"] == "MNG"
    assert _run(250_000, gate=0.03).opened[0]["symbol"] == "NG"


def test_the_chosen_contract_is_what_gets_recorded():
    """Variance capture has to say which tier actually traded, or the cost
    comparison it exists to support cannot be made."""
    from app import trade_registry

    broker = _run(50_000)
    assert broker.opened[0]["symbol"] == "MNG"
    mappings = trade_registry.get_open_mappings("s", ACCT, 5001)
    assert [m.slave_symbol for m in mappings] == ["MNG"]
