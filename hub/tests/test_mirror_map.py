"""Mirror mode's two decisions: which instrument, how big, and whether the
account's existing exposure forbids it.

The numbers here are the real ones, read off the two brokers' SYMBOLS
replies on 2026-11-12, because the point of the point-value ratio is that
nobody has to work it out by hand:

    ICM          XAUUSD 100   XAGUSD 1000  XTIUSD 100  BTCUSD 1   US500 1
    Darwinex     GC_Z   100   SI_Z   5000  CL_F  1000  MBT_X 0.1  ES_Z  50
"""
import asyncio

import pytest

from app import trade_registry
from app.copy_engine import CopyEngine
from app.mirror import MirrorTarget, floor_to_step, netting_refusal, resolve_target
from app.models import Signal
from tests.conftest_fakes import FakeBroker

ACCT = "ICM-44916920"
_TICKET = iter(range(7001, 7999))


class FakePosition:
    def __init__(self, symbol, quantity):
        self.symbol = symbol
        self.quantity = quantity


# --------------------------------------------------------------- the grid

def test_floor_never_rounds_up():
    # The whole point. 0.15 on a 0.1 grid is 0.1, not 0.2.
    assert floor_to_step(0.15, 0.1) == 0.1
    assert floor_to_step(0.6, 1.0) == 0.0
    assert floor_to_step(200.4, 1.0) == 200.0


def test_floor_keeps_sizes_that_are_already_exact():
    # 0.03 / 0.01 is 2.9999999999999996 in doubles. A bare floor would make
    # this 0.02 and quietly carry a third less risk than asked.
    assert floor_to_step(0.03, 0.01) == 0.03
    assert floor_to_step(0.07, 0.01) == 0.07
    assert floor_to_step(2.0, 1.0) == 2.0
    assert floor_to_step(50.0, 0.1) == 50.0


# ------------------------------------------------------- symbol and size

def test_no_map_behaves_as_mirror_mode_always_did():
    target, reason = resolve_target({}, "XAUUSD", 0.5)
    assert reason is None
    assert target == MirrorTarget(symbol="XAUUSD", lots=0.5, how=target.how)


def test_risk_scale_still_applies_and_still_floors():
    target, reason = resolve_target({"risk_scale": 0.5}, "XAUUSD", 0.25)
    assert reason is None
    # 0.125 floored to the 0.01 grid, not rounded to 0.13.
    assert target.lots == 0.12


def test_a_bare_string_maps_the_symbol_and_keeps_the_size():
    target, reason = resolve_target(
        {"mirror_map": {"XAUUSD": "GC_Z"}}, "XAUUSD", 1.0
    )
    assert reason is None
    assert (target.symbol, target.lots) == ("GC_Z", 1.0)


def test_one_btc_lot_becomes_ten_micro_contracts():
    config = {"mirror_map": {"BTCUSD": {
        "target": "MBT_X", "master_point_value": 1.0, "point_value": 0.1,
        "volume_step": 1.0, "volume_min": 1.0,
    }}}
    target, reason = resolve_target(config, "BTCUSD", 1.0)
    assert reason is None
    assert (target.symbol, target.lots) == ("MBT_X", 10.0)
    # And the log line has to explain itself.
    assert "10" in target.how


def test_five_silver_lots_become_one_comex_contract():
    config = {"mirror_map": {"XAGUSD": {
        "target": "SI_Z", "master_point_value": 1000.0, "point_value": 5000.0,
        "volume_step": 1.0, "volume_min": 1.0,
    }}}
    target, reason = resolve_target(config, "XAGUSD", 5.0)
    assert reason is None
    assert (target.symbol, target.lots) == ("SI_Z", 1.0)


def test_a_size_that_floors_below_the_minimum_is_refused_not_rounded_up():
    # Four silver lots is 0.8 of a COMEX contract. The old round-to-nearest
    # on the EA side would have made that 1.0 -- 25% more silver than the
    # master held.
    config = {"mirror_map": {"XAGUSD": {
        "target": "SI_Z", "master_point_value": 1000.0, "point_value": 5000.0,
        "volume_step": 1.0, "volume_min": 1.0,
    }}}
    target, reason = resolve_target(config, "XAGUSD", 4.0)
    assert target is None
    assert "below its minimum" in reason
    assert "rounding up" in reason


def test_an_unmapped_symbol_is_skipped_once_a_map_exists():
    config = {"mirror_map": {"BTCUSD": "MBT_X"}}
    target, reason = resolve_target(config, "XAUUSD", 1.0)
    assert target is None
    assert "no mirror_map entry for XAUUSD" in reason


def test_passthrough_restores_the_old_behaviour_for_unmapped_symbols():
    config = {"mirror_map": {"BTCUSD": "MBT_X"}, "mirror_map_passthrough": True}
    target, reason = resolve_target(config, "XAUUSD", 1.0)
    assert reason is None
    assert (target.symbol, target.lots) == ("XAUUSD", 1.0)


def test_half_a_ratio_is_refused_rather_than_assumed_to_be_one():
    config = {"mirror_map": {"BTCUSD": {
        "target": "MBT_X", "point_value": 0.1,
    }}}
    target, reason = resolve_target(config, "BTCUSD", 1.0)
    assert target is None
    assert "only one side" in reason


def test_lot_ratio_is_accepted_where_no_point_value_describes_it():
    config = {"mirror_map": {"US500": {"target": "ES_Z", "lot_ratio": 0.02,
                                       "volume_step": 1.0, "volume_min": 1.0}}}
    target, reason = resolve_target(config, "US500", 100.0)
    assert reason is None
    assert (target.symbol, target.lots) == ("ES_Z", 2.0)


@pytest.mark.parametrize("scale", [0, -1, -0.5])
def test_a_nonsense_risk_scale_refuses_rather_than_inverting_the_trade(scale):
    target, reason = resolve_target({"risk_scale": scale}, "XAUUSD", 1.0)
    assert target is None
    assert "not a tradeable fraction" in reason


# -------------------------------------------------------------- netting

def test_no_existing_exposure_permits_anything():
    assert netting_refusal([], "MBT_X", "BUY", 200.0, {}) is None


def test_an_opposing_leg_is_refused_by_default():
    # The real case: the futures account held 73 lots short MBT_X while a
    # 200-lot long copy was being considered.
    held = [FakePosition("MBT_X", -73.0)]
    reason = netting_refusal(held, "MBT_X", "BUY", 200.0, {})
    assert reason is not None
    assert "-73" in reason and "+127" in reason


def test_allow_opposing_lets_it_through():
    held = [FakePosition("MBT_X", -73.0)]
    assert netting_refusal(held, "MBT_X", "BUY", 200.0,
                           {"allow_opposing": True}) is None


def test_exposure_in_another_symbol_is_not_this_symbol_s_problem():
    held = [FakePosition("GC_Z", -1.0)]
    assert netting_refusal(held, "MBT_X", "BUY", 20.0, {}) is None


def test_same_side_exposure_is_allowed_unless_max_legs_says_otherwise():
    held = [FakePosition("MBT_X", 20.0), FakePosition("MBT_X", 20.0)]
    assert netting_refusal(held, "MBT_X", "BUY", 20.0, {}) is None
    reason = netting_refusal(held, "MBT_X", "BUY", 20.0, {"max_legs": 2})
    assert reason is not None and "max_legs" in reason


def test_max_net_lots_caps_the_resulting_exposure():
    held = [FakePosition("MBT_X", 100.0)]
    policy = {"max_net_lots": 150}
    assert netting_refusal(held, "MBT_X", "BUY", 50.0, policy) is None
    reason = netting_refusal(held, "MBT_X", "BUY", 60.0, policy)
    assert reason is not None and "max_net_lots" in reason


def test_max_net_lots_measures_the_net_not_the_gross():
    # Short 100 plus a long 60 is a net 40, well inside a 150 ceiling, even
    # though the gross is 160. Opposing legs have to be permitted for this
    # to be reachable at all.
    held = [FakePosition("MBT_X", -100.0)]
    policy = {"max_net_lots": 150, "allow_opposing": True}
    assert netting_refusal(held, "MBT_X", "BUY", 60.0, policy) is None


# ----------------------------------------------- the engine, wired up

class HoldingBroker(FakeBroker):
    """A slave account with positions of its own, like the futures one."""

    def __init__(self, held=(), **kw):
        super().__init__(**kw)
        self.held = list(held)
        self.position_reads = 0

    async def get_open_positions(self, account_type=None):
        self.position_reads += 1
        return self.held


def _run(broker, config, symbol="BTCUSD", lots=1.0, direction="BUY"):
    queue: asyncio.Queue = asyncio.Queue()
    engine = CopyEngine("mirror-test", broker, queue, config)
    ticket = next(_TICKET)
    signal = Signal(ticket=ticket, symbol=symbol, action="OPEN",
                    direction=direction, lots=lots, price=71600.0, sl=70900.0,
                    account=ACCT)

    async def go():
        engine.start()
        await queue.put(signal)
        await asyncio.sleep(0.1)
        await engine.stop()

    asyncio.run(go())
    return ticket


def test_the_engine_opens_the_mapped_symbol_at_the_converted_size():
    broker = HoldingBroker()
    config = {"mirror_map": {"BTCUSD": {
        "target": "MBT_X", "master_point_value": 1.0, "point_value": 0.1,
        "volume_step": 1.0, "volume_min": 1.0,
    }}}
    _run(broker, config, lots=2.0)
    assert len(broker.opened) == 1
    assert broker.opened[0]["symbol"] == "MBT_X"
    assert broker.opened[0]["size"] == 20.0


def test_the_registry_records_the_slave_s_symbol_not_the_master_s():
    """MODIFY and CLOSE read slave_symbol back out of this row, and
    reconciliation matches open positions on it. Recording XAUUSD against a
    GC_Z position would break both."""
    broker = HoldingBroker()
    config = {"mirror_map": {"XAUUSD": "GC_Z"}}
    ticket = _run(broker, config, symbol="XAUUSD", lots=1.0)
    mappings = trade_registry.get_open_mappings("mirror-test", ACCT, ticket)
    assert [m.slave_symbol for m in mappings] == ["GC_Z"]


def test_the_netting_guard_blocks_an_opposing_copy_when_configured():
    broker = HoldingBroker(held=[FakePosition("MBT_X", -73.0)])
    config = {
        "mirror_map": {"BTCUSD": {"target": "MBT_X", "master_point_value": 1.0,
                                  "point_value": 0.1, "volume_step": 1.0,
                                  "volume_min": 1.0}},
        "netting": {},
    }
    _run(broker, config, lots=2.0)
    assert broker.opened == []
    assert broker.position_reads == 1


def test_the_guard_is_opt_in_and_costs_nothing_when_unconfigured():
    """An existing mirror slave must behave exactly as before: no POSITIONS
    round trip on every OPEN, and a master holding both sides of a symbol
    not suddenly refused."""
    broker = HoldingBroker(held=[FakePosition("MBT_X", -73.0)])
    config = {"mirror_map": {"BTCUSD": {"target": "MBT_X", "master_point_value": 1.0,
                                        "point_value": 0.1, "volume_step": 1.0,
                                        "volume_min": 1.0}}}
    _run(broker, config, lots=2.0)
    assert len(broker.opened) == 1
    assert broker.position_reads == 0


def test_a_slave_that_cannot_report_positions_does_not_silently_pass_the_guard():
    """An unknown book is not an empty one. The trade proceeds -- refusing
    every copy because an EA is old would be worse -- but the adapter says
    so in its own log, and the guard must not claim to have checked."""
    class Mute(FakeBroker):
        async def get_open_positions(self, account_type=None):
            raise NotImplementedError("EA predates POSITIONS")

    broker = Mute()
    config = {"mirror_map": {"XAUUSD": "GC_Z"}, "netting": {}}
    _run(broker, config, symbol="XAUUSD", lots=1.0)
    assert len(broker.opened) == 1
