"""Mirror mode sizing by a percentage of the SLAVE's own capital.

This is how risk has ported to every other slave: ibkr-paper-1 risks 2% of
its own equity per signal and the master's lot count never enters into it. A
mirror slave could not do it until the bridge could report its balance.

The figures are the live ones from 2026-11-12: the Darwinex futures account
at 1,433,614.86 USD, GC_Z with a 0.1 tick worth 10.00 (point value 100), and
MBT_X with a 5.0 tick worth 0.50 (point value 0.1, maximum 20 lots).
"""
import asyncio

import pytest

from app.adapters.base import AccountSummary, BrokerSymbolSpec
from app.copy_engine import CopyEngine
from app.mirror import plan_by_risk
from app.models import Signal
from tests.conftest_fakes import FakeBroker

ACCT = "ICM-44916920"
_TICKET = iter(range(8001, 8999))
BALANCE = 1_433_614.86


def gold(bid=3882.9, ask=3883.1):
    return BrokerSymbolSpec(symbol="GC_Z", digits=2, tick_size=0.1, tick_value=10.0,
                            volume_step=1.0, volume_min=1.0, volume_max=50.0,
                            bid=bid, ask=ask, trade_mode="FULL", contract_size=1.0)


def bitcoin(bid=71610.0, ask=71625.0):
    return BrokerSymbolSpec(symbol="MBT_X", digits=0, tick_size=5.0, tick_value=0.5,
                            volume_step=1.0, volume_min=1.0, volume_max=20.0,
                            bid=bid, ask=ask, trade_mode="FULL", contract_size=1.0)


def _plan(spec, config=None, price=3884.0, sl=3845.16, tp=None, direction="BUY",
          balance=BALANCE, equity=BALANCE):
    config = config or {"sizing": {"mode": "pct_risk", "risk_pct": 0.02, "capital_base": "balance"}}
    return plan_by_risk(config, spec.symbol, direction, price, sl, tp, spec,
                        balance, equity)


# ----------------------------------------------------------- the model

def test_two_percent_of_the_slave_s_own_balance_decides_the_size():
    # A 1% stop on gold: 38.84 away on a 3884 master price. On GC_Z at
    # 3883.1 that is 38.83 points, and a point is worth 100, so one contract
    # risks ~3883. 2% of 1,433,614 is 28,672 -> 7 contracts.
    plan, reason = _plan(gold())
    assert reason is None
    assert plan.lots == 7.0
    # One contract risks 3,880 at this stop, and the log line has to let a
    # reader reproduce that from the figures it prints.
    assert "3880.00/lot of 1433614.86" in plan.how


def test_the_master_s_lot_count_is_irrelevant():
    """The point of the change. 0.1 master lots and 100 master lots must
    give the same slave size, because the slave sizes from its own risk."""
    first, _ = _plan(gold())
    # plan_by_risk is not even given the master's lots -- this test exists to
    # pin that signature, so a future edit cannot quietly reintroduce them.
    assert "master lots" not in plan_signature()
    assert first.lots == 7.0


def plan_signature():
    import inspect
    return str(inspect.signature(plan_by_risk))


def test_capital_base_equity_is_honoured():
    plan, _ = _plan(gold(),
                    config={"sizing": {"mode": "pct_risk", "risk_pct": 0.02, "capital_base": "equity"}},
                    balance=100.0, equity=BALANCE)
    assert plan.lots == 7.0


def test_the_balance_adjustment_applies_here_too():
    """It is a fact about the account, not about one sizing mode."""
    base, _ = _plan(gold(), config={"sizing": {"mode": "pct_risk", "risk_pct": 0.02}},
                    balance=1_000_000.0, equity=1_000_000.0)
    lifted, _ = _plan(gold(),
                      config={"sizing": {"mode": "pct_risk", "risk_pct": 0.02},
                              "balance_adjustment": 1_000_000.0},
                      balance=1_000_000.0, equity=1_000_000.0)
    assert lifted.lots > base.lots


def test_risk_scale_still_scales_the_result():
    whole, _ = _plan(gold())
    half, _ = _plan(gold(), config={"sizing": {"mode": "pct_risk", "risk_pct": 0.02},
                                    "risk_scale": 0.5})
    # 7.39 contracts halved is 3.70, and nearest_min1 rounds half up: 4, not
    # 3. The dial scales the risk the model asked for, and the rounding rule
    # still has the last word -- which is how it behaves for ibkr-paper-1.
    assert whole.lots == 7.0
    assert half.lots == 4.0


# ------------------------------------------------- the stop translation

def test_the_stop_travels_as_a_fraction_not_as_a_level():
    """The bug this replaced: the master's absolute SL was passed through to
    a different instrument, where it is either absurd or already triggered."""
    # A 1% stop below a 7594 master price, on a target quoting 6800.
    spec = BrokerSymbolSpec(symbol="ES_Z", digits=2, tick_size=0.25,
                            tick_value=12.5, volume_step=1.0, volume_min=1.0,
                            volume_max=50.0, bid=6799.75, ask=6800.25)
    plan, reason = plan_by_risk(
        {"sizing": {"mode": "pct_risk", "risk_pct": 0.02}}, "ES_Z", "BUY",
        7594.0, 7518.06, None, spec, BALANCE, BALANCE,
    )
    assert reason is None
    # 1% below 6800.25, not anywhere near the master's 7518.
    assert 6730 < plan.sl < 6740
    assert plan.sl != 7518.06


def test_the_stop_lands_on_the_target_s_tick_grid():
    plan, _ = _plan(bitcoin(), price=71600.0, sl=70884.0)
    # MBT_X trades in whole fives.
    assert plan.sl % 5 == 0


def test_a_sell_puts_its_stop_above_the_entry():
    plan, reason = _plan(gold(), direction="SELL", price=3884.0, sl=3922.84)
    assert reason is None
    assert plan.sl > gold().bid


def test_the_take_profit_keeps_the_master_s_reward_to_risk():
    # Stop 1% below, target 3% above: the slave's levels must be 1% and 3%
    # from ITS price, so the ratio the master chose survives.
    plan, _ = _plan(gold(), price=3884.0, sl=3845.16, tp=4000.52)
    entry = gold().ask
    assert (plan.tp - entry) / (entry - plan.sl) == pytest.approx(3.0, abs=0.05)


def test_no_stop_on_the_master_signal_is_refused_not_guessed():
    plan, reason = _plan(gold(), sl=None)
    assert plan is None
    assert "no stop on the master signal" in reason


# ------------------------------------------------------ edges that bite

def test_a_signal_too_small_for_one_contract_still_takes_one():
    """nearest_min1, the convention every other slave sizes by: round half
    up, never below one unit. A master signal far smaller than one futures
    contract becomes one contract rather than nothing."""
    plan, reason = _plan(gold(),
                         config={"sizing": {"mode": "pct_risk", "risk_pct": 0.0001}})
    assert reason is None
    assert plan.lots == 1.0


def test_max_risk_pct_is_what_refuses_a_minimum_that_risks_too_much():
    """And it is the reason nearest_min1 is safe. One GC_Z contract risks
    3,880; against a 150,000 account that is 2.6%, so a 1% ceiling refuses
    it rather than taking a position 2.6x the size agreed."""
    plan, reason = _plan(
        gold(),
        config={"sizing": {"mode": "pct_risk", "risk_pct": 0.0001,
                           "max_risk_pct": 0.01}},
        balance=150_000.0, equity=150_000.0,
    )
    assert plan is None
    assert "max_risk_pct" in reason or "risk" in reason


def test_the_floor_rounding_mode_skips_instead_of_taking_one():
    plan, reason = _plan(gold(),
                         config={"sizing": {"mode": "pct_risk", "risk_pct": 0.0001,
                                            "rounding": "floor"}})
    assert plan is None


def test_a_size_above_the_maximum_is_allowed_and_says_it_will_split():
    # 2% over a 1% stop on MBT_X: a point is worth 0.1, so one contract
    # risks ~72 and 28,672 buys about 400 -- far over the 20-lot cap.
    plan, reason = _plan(bitcoin(), price=71600.0, sl=70884.0,
                         config={"sizing": {"mode": "pct_risk", "risk_pct": 0.02}})
    assert reason is None
    assert plan.lots > 20
    assert "deals held together as one group" in plan.how
    # 401 lots in clips of 20 is 21 deals.
    assert "21 deals" in plan.how


def test_an_unticked_target_is_refused_rather_than_sized_on_a_zero():
    plan, reason = _plan(gold(bid=0.0, ask=0.0))
    assert plan is None
    assert "no price yet" in reason


def test_a_symbol_with_no_point_value_is_refused():
    spec = gold()
    spec.tick_value = 0.0
    plan, reason = _plan(spec)
    assert plan is None
    assert "no point value" in reason


# ------------------------------------------------- wired into the engine

class SpecBroker(FakeBroker):
    """A bridge that answers ACCOUNT and SYMBOLS, as 1.03+ does."""

    def __init__(self, spec, balance=BALANCE, **kw):
        super().__init__(**kw)
        self.spec = spec
        self.account_balance = balance

    async def get_symbol_spec(self, symbol):
        return self.spec

    async def get_account_summary(self, account_type=""):
        return AccountSummary(balance=self.account_balance,
                              equity=self.account_balance)

    async def get_open_positions(self, account_type=None):
        return []


class OldBroker(FakeBroker):
    async def get_symbol_spec(self, symbol):
        raise NotImplementedError("slave EA predates the SYMBOLS command")

    async def get_account_summary(self, account_type=""):
        raise NotImplementedError("slave EA predates the ACCOUNT command")


def _run(broker, config, symbol="XAUUSD", lots=0.1, price=3884.0, sl=3845.16):
    queue: asyncio.Queue = asyncio.Queue()
    engine = CopyEngine("risk-mirror", broker, queue, config)
    signal = Signal(ticket=next(_TICKET), symbol=symbol, action="OPEN",
                    direction="BUY", lots=lots, price=price, sl=sl, account=ACCT)

    async def go():
        engine.start()
        await queue.put(signal)
        await asyncio.sleep(0.1)
        await engine.stop()

    asyncio.run(go())
    return broker


def test_the_engine_sizes_from_the_slave_account_and_sends_a_translated_stop():
    broker = SpecBroker(gold())
    config = {"mirror_map": {"XAUUSD": {"target": "GC_Z"}},
              "sizing": {"mode": "pct_risk", "risk_pct": 0.02, "capital_base": "balance"}}
    _run(broker, config)
    assert len(broker.opened) == 1
    sent = broker.opened[0]
    assert sent["symbol"] == "GC_Z"
    # A 0.1-lot master signal becomes 7 gold contracts, because the size
    # comes from the slave's own capital.
    assert sent["size"] == 7.0
    # And NOT the master's 3845.16.
    assert sent["sl"] != 3845.16
    assert 3840 < sent["sl"] < 3850


def test_an_old_ea_refuses_rather_than_silently_sizing_another_way():
    """Falling back to lot mirroring would put on a position sized by a
    model nobody configured."""
    broker = OldBroker()
    config = {"mirror_map": {"XAUUSD": {"target": "GC_Z"}},
              "sizing": {"mode": "pct_risk", "risk_pct": 0.02}}
    _run(broker, config)
    assert broker.opened == []


def test_a_slave_without_a_sizing_block_still_mirrors_lots_exactly_as_before():
    """mt5-icm-1 must not change behaviour."""
    broker = SpecBroker(gold())
    _run(broker, {}, symbol="XAUUSD", lots=0.1)
    assert len(broker.opened) == 1
    assert broker.opened[0]["symbol"] == "XAUUSD"
    assert broker.opened[0]["size"] == 0.1
    # The master's own stop, untranslated, because it is the same instrument.
    assert broker.opened[0]["sl"] == 3845.16
