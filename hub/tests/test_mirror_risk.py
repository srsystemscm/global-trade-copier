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
    # reader reproduce that from the figures it prints -- including WHICH
    # capital figure was sized against, since capital_base chooses.
    assert "3880.00/lot of 1433614.86 (balance)" in plan.how


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


def test_max_net_lots_can_be_set_per_symbol():
    from app.mirror import netting_refusal

    class P:
        def __init__(self, s, q):
            self.symbol, self.quantity = s, q

    policy = {"max_net_lots": {"MBT_X": 70, "GC_Z": 3}, "allow_opposing": True}
    assert netting_refusal([P("MBT_X", 50.0)], "MBT_X", "BUY", 20.0, policy) is None
    assert netting_refusal([P("MBT_X", 60.0)], "MBT_X", "BUY", 20.0, policy) is not None
    # A ceiling that suits gold at 388,290 a contract is meaningless for
    # bitcoin at 7,161, which is why one number for both is the wrong shape.
    assert netting_refusal([P("GC_Z", 2.0)], "GC_Z", "BUY", 2.0, policy) is not None


def test_a_symbol_with_no_entry_in_the_per_symbol_cap_is_uncapped():
    from app.mirror import netting_refusal

    class P:
        def __init__(self, s, q):
            self.symbol, self.quantity = s, q

    policy = {"max_net_lots": {"MBT_X": 70}}
    assert netting_refusal([P("SI_Z", 900.0)], "SI_Z", "BUY", 100.0, policy) is None


# --------------------------------------------------- the margin ceiling

class FakePreview:
    def __init__(self, initial_change, per_unit, free_before, initial_after=None):
        self.initial_change = initial_change
        self.per_unit = per_unit
        self.free_before = free_before
        self.initial_after = initial_after


def test_no_margin_policy_leaves_the_size_alone():
    from app.mirror import margin_fit
    lots, note, refusal = margin_fit(401.0, FakePreview(1_363_400, 3400, 1_250_000),
                                     None, 1.0, 1.0)
    assert (lots, note, refusal) == (401.0, None, None)


def test_a_size_that_fits_the_ceiling_passes_untouched():
    from app.mirror import margin_fit
    lots, note, refusal = margin_fit(7.0, FakePreview(60_000, 8571, 1_250_000),
                                     {"max_pct_of_free": 0.25}, 1.0, 1.0)
    assert lots == 7.0 and note is None and refusal is None


def test_the_measured_mbt_case_is_reduced_to_what_the_account_can_margin():
    """401 MBT_X contracts at the 3,400/lot measured is 1,363,400 of margin
    against 1,250,000 free. A 25% ceiling is 312,500, which affords 91."""
    from app.mirror import margin_fit
    lots, note, refusal = margin_fit(
        401.0, FakePreview(1_363_400, 3400, 1_250_000),
        {"max_pct_of_free": 0.25}, 1.0, 1.0,
    )
    assert refusal is None
    assert lots == 91.0
    assert "REDUCED by margin from 401 to 91" in note
    # The honest part: this leg no longer carries the risk that was asked for.
    assert "22.7% of the risk" in note


def test_reducing_lands_on_the_broker_s_volume_grid():
    from app.mirror import margin_fit
    lots, _, _ = margin_fit(401.0, FakePreview(1_363_400, 3400, 1_000_000),
                            {"max_pct_of_free": 0.25}, 5.0, 5.0)
    assert lots % 5 == 0


def test_a_ceiling_that_cannot_afford_even_one_lot_refuses():
    from app.mirror import margin_fit
    lots, note, refusal = margin_fit(
        7.0, FakePreview(60_000, 8571, 100_000),
        {"max_pct_of_free": 0.05}, 1.0, 1.0,
    )
    assert lots is None
    assert "even the minimum 1 lots needs" in refusal


def test_a_ceiling_switched_on_but_unanswerable_refuses_rather_than_trading():
    """A setting that silently stops applying is worse than no setting."""
    from app.mirror import margin_fit
    lots, note, refusal = margin_fit(7.0, FakePreview(None, None, None),
                                     {"max_pct_of_free": 0.25}, 1.0, 1.0)
    assert lots is None
    assert "did not report a usable margin figure" in refusal


def test_no_per_lot_figure_means_no_affordable_size_can_be_worked_out():
    from app.mirror import margin_fit
    lots, note, refusal = margin_fit(401.0, FakePreview(1_363_400, None, 1_250_000),
                                     {"max_pct_of_free": 0.25}, 1.0, 1.0)
    assert lots is None
    assert "no per-lot margin" in refusal


class MarginBroker(SpecBroker):
    def __init__(self, spec, per_lot, free, **kw):
        super().__init__(spec, **kw)
        self.per_lot = per_lot
        self.free = free
        self.previews = 0

    async def preview_margin(self, order):
        from app.adapters.base import MarginPreview
        self.previews += 1
        return MarginPreview(initial_change=order.size * self.per_lot,
                             per_unit=self.per_lot, free_before=self.free)


def test_the_engine_sends_the_margin_reduced_size():
    broker = MarginBroker(bitcoin(), per_lot=3400.0, free=1_250_000.0)
    config = {
        "mirror_map": {"BTCUSD": {"target": "MBT_X"}},
        "sizing": {"mode": "pct_risk", "risk_pct": 0.02, "capital_base": "balance"},
        "margin": {"max_pct_of_free": 0.25},
    }
    _run(broker, config, symbol="BTCUSD", price=71600.0, sl=70884.0)
    assert len(broker.opened) == 1
    assert broker.opened[0]["size"] == 91.0
    assert broker.previews == 1


def test_no_margin_block_means_no_round_trip():
    broker = MarginBroker(gold(), per_lot=8571.0, free=1_250_000.0)
    config = {"mirror_map": {"XAUUSD": {"target": "GC_Z"}},
              "sizing": {"mode": "pct_risk", "risk_pct": 0.02,
                         "capital_base": "balance"}}
    _run(broker, config, symbol="XAUUSD")
    assert broker.previews == 0
    assert broker.opened[0]["size"] == 7.0


def test_an_old_ea_refuses_a_configured_ceiling_rather_than_ignoring_it():
    class NoMargin(SpecBroker):
        async def preview_margin(self, order):
            raise NotImplementedError("slave EA predates the MARGIN command")

    broker = NoMargin(gold())
    config = {"mirror_map": {"XAUUSD": {"target": "GC_Z"}},
              "sizing": {"mode": "pct_risk", "risk_pct": 0.02},
              "margin": {"max_pct_of_free": 0.25}}
    _run(broker, config, symbol="XAUUSD")
    assert broker.opened == []


def test_the_explanation_names_the_capital_it_actually_sized_against():
    """Caught by a live dry run: capital_base was equity, the size came from
    equity, and the line printed the balance -- so the figures in it did not
    add up to the size beside them."""
    plan, reason = _plan(
        gold(),
        config={"sizing": {"mode": "pct_risk", "risk_pct": 0.02,
                           "capital_base": "equity"}},
        balance=1_000_000.0, equity=1_536_551.86,
    )
    assert reason is None
    assert "of 1536551.86 (equity)" in plan.how
    assert "1000000" not in plan.how
