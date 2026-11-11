"""MODIFY journeys: what reaches a slave when a master EA walks its stop.

MODIFY is the one action that has never run end to end on real terminals, on
either platform. The unit tests cover it a signal at a time; a live EA does
not send one MODIFY, it sends a ladder -- open with a protective stop, move
to breakeven, then trail in steps for as long as the trade runs, across
several symbols and several legs at once. These drive those sequences
through the real engines and assert what each slave was actually told.

Two journeys matter and they are deliberately different:

  mirror (MT4 -> MT4/MT5)   follows the master's absolute SL/TP exactly
  autonomous (CFD -> futures) IGNORES master MODIFY and runs its own
                              ATR breakeven/trail instead

That second one is a design decision, not an oversight (see
AutonomousCopyEngine's docstring), and it means a master stop-walk does
nothing to a futures leg. Asserted here so the divergence stays visible.
"""
import asyncio

import pytest

from app import trade_registry
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.copy_engine import CopyEngine
from app.models import Signal
from tests.conftest_fakes import FakeBroker

MIRROR = "test-mirror"
AUTO = "test-auto-modify"


# --- driving a journey -----------------------------------------------------

def journey(engine, signals, settle=0.05):
    """Feed a master EA's signal sequence through an engine's real run loop.

    Deliberately not calling _handle() directly: the queue, the pause check
    and the session gate are all part of what a real signal passes through,
    and a MODIFY arriving one tick after its OPEN is exactly the case the
    dedup fix exists for.
    """
    async def go():
        engine.start()
        for s in signals:
            await engine.queue.put(s)
        for _ in range(400):
            await asyncio.sleep(0.005)
            if engine.queue.empty():
                break
        await asyncio.sleep(settle)
        await engine.stop()

    asyncio.run(go())


def modifies(broker):
    """Just the modify calls, as (slave_ticket, sl, tp)."""
    return [(c[1], c[2], c[3]) for c in broker.calls if c[0] == "modify"]


def _mirror(broker):
    return CopyEngine(MIRROR, broker, asyncio.Queue())


def _auto(broker, **cfg):
    config = {
        "mode": "autonomous",
        "atr_period": 14,
        "routes": [
            {"symbol": "XAUUSD", "target": "GC", "month_slot": 0,
             "sizing": {"mode": "fixed_contracts", "contracts": 1}},
        ],
        "risk_management": {
            "breakeven_trigger_atr": 1.0,
            "trailing_atr": 1.5,
            # Long enough that the monitor loop never fires inside a test --
            # these assert what master MODIFY does, not what the trail does.
            "poll_interval_seconds": 3600,
        },
    }
    config.update(cfg)
    return AutonomousCopyEngine(AUTO, broker, asyncio.Queue(), config)


def _sig(**kw):
    """A master signal shaped like the fxDreema EAs actually send them."""
    base = dict(ticket=5001, symbol="XAUUSD", action="OPEN", direction="BUY",
                lots=2.0, price=4029.32, sl=3937.0, tp=0.0, atr=18.0, magic=2389)
    base.update(kw)
    return Signal(**base)


def ladder(stops, **kw):
    """OPEN then one MODIFY per stop -- a breakeven move and a trail."""
    first, *rest = stops
    out = [_sig(action="OPEN", sl=first, **kw)]
    out += [_sig(action="MODIFY", sl=s, **kw) for s in rest]
    return out


# --- mirror mode: the master's levels, exactly ----------------------------

def test_a_breakeven_then_trail_ladder_reaches_the_slave_step_for_step():
    b = FakeBroker()
    journey(_mirror(b), ladder([3937.0, 4029.32, 4045.0, 4060.0, 4075.0]))

    assert len(b.opened) == 1, "the ladder opened more than one leg"
    ticket = b.opened[0]["ticket"]
    assert modifies(b) == [
        (ticket, 4029.32, None),   # to breakeven
        (ticket, 4045.0, None),    # trailing
        (ticket, 4060.0, None),
        (ticket, 4075.0, None),
    ], "a stop-walk must arrive as the master sent it, in order, on one ticket"


def test_a_sell_ladder_walks_the_stop_down():
    """Same journey mirrored. A sign error anywhere would show up as a stop
    moving the wrong way, which on a short is a stop moving further away."""
    b = FakeBroker()
    journey(_mirror(b), ladder([4120.0, 4029.32, 4010.0, 3995.0],
                               direction="SELL", price=4029.32))

    sls = [m[1] for m in modifies(b)]
    assert sls == [4029.32, 4010.0, 3995.0]
    assert sls == sorted(sls, reverse=True), "a short's stop must descend"


def test_two_assets_trailing_at_once_never_cross_tickets():
    """Gold and silver both running, stops moving in turn. The registry maps
    per master_ticket, so a crossed mapping would put gold's stop on the
    silver leg -- survivable in a test, not on an account."""
    b = FakeBroker()
    gold = ladder([3937.0, 4029.32, 4050.0], ticket=5001, symbol="XAUUSD", price=4029.32)
    silver = ladder([46.10, 48.20, 48.90], ticket=5002, symbol="XAGUSD", price=48.20)
    # interleaved, as two EAs on one account would actually send them
    journey(_mirror(b), [gold[0], silver[0], gold[1], silver[1], gold[2], silver[2]])

    by_symbol = {o["symbol"]: o["ticket"] for o in b.opened}
    assert set(by_symbol) == {"XAUUSD", "XAGUSD"}

    per_ticket = {}
    for ticket, sl, _tp in modifies(b):
        per_ticket.setdefault(ticket, []).append(sl)

    assert per_ticket[by_symbol["XAUUSD"]] == [4029.32, 4050.0]
    assert per_ticket[by_symbol["XAGUSD"]] == [48.20, 48.90]


def test_two_legs_on_one_symbol_are_modified_independently():
    """Their EAs run legs that share a magic on the same symbol. Each leg is
    its own master ticket, so each must get its own slave ticket and its own
    stop -- one MODIFY must not move the other leg."""
    b = FakeBroker()
    leg1 = ladder([3937.0, 4029.32], ticket=5001)
    leg2 = ladder([3920.0, 4010.00], ticket=5002)
    journey(_mirror(b), [leg1[0], leg2[0], leg1[1], leg2[1]])

    tickets = [o["ticket"] for o in b.opened]
    assert len(set(tickets)) == 2, "two legs collapsed onto one slave ticket"
    assert sorted(modifies(b)) == sorted([
        (tickets[0], 4029.32, None),
        (tickets[1], 4010.00, None),
    ])


def test_a_zero_take_profit_is_never_sent_as_a_target_of_zero():
    """MQL4 returns 0.0 for an unset level and StringFormat cannot emit JSON
    null, so "no TP" arrives as tp=0.0. Taken literally that is a target at
    a price of zero -- live, it produced a GC target of 7703 against a 4040
    entry. The Signal validator turns it into None; this proves the MODIFY
    path keeps it that way rather than reviving the 0.0."""
    b = FakeBroker()
    journey(_mirror(b), ladder([3937.0, 4029.32], tp=0.0))

    assert b.opened[0]["tp"] is None
    assert [m[2] for m in modifies(b)] == [None]


def test_a_real_take_profit_is_carried_through_every_modify():
    b = FakeBroker()
    journey(_mirror(b), ladder([3937.0, 4029.32, 4050.0], tp=4200.0))

    assert [m[2] for m in modifies(b)] == [4200.0, 4200.0]


def test_modify_for_a_ticket_this_slave_never_opened_is_dropped_not_fatal():
    """A hub started mid-trade, or a slave added while a master position is
    already open. The MODIFY has no mapping. It must be dropped and the
    engine must still be alive for the next OPEN."""
    b = FakeBroker()
    journey(_mirror(b), [
        _sig(action="MODIFY", ticket=9999, sl=4000.0),
        _sig(action="OPEN", ticket=5001, sl=3937.0),
        _sig(action="MODIFY", ticket=5001, sl=4029.32),
    ])

    assert len(b.opened) == 1, "the engine died on the orphan MODIFY"
    assert modifies(b) == [(b.opened[0]["ticket"], 4029.32, None)]


def test_modify_arriving_after_the_master_closed_is_dropped():
    """Ordering a live EA really produces: the trail fires once more in the
    same poll that the position closed. The registry row is already CLOSED,
    so there is no mapping and nothing should reach the broker -- a MODIFY
    against a closed ticket is error 4108 on MT4, and on MT5 it would hit
    the already-closed path instead."""
    b = FakeBroker()
    journey(_mirror(b), [
        _sig(action="OPEN", ticket=5001, sl=3937.0),
        _sig(action="CLOSE", ticket=5001),
        _sig(action="MODIFY", ticket=5001, sl=4029.32),
    ])

    assert b.closed == [b.opened[0]["ticket"]]
    assert modifies(b) == [], "a stop was moved on a closed leg"


def test_a_stop_that_never_moves_still_sends_nothing_extra():
    """An EA that re-asserts the same stop every poll. Mirror mode forwards
    what it is given -- the EAs are what suppress a no-op (SlTpEpsilon on
    the master, retcode 10025 on an MT5 slave), so what this pins down is
    that the hub does not invent extra MODIFYs of its own."""
    b = FakeBroker()
    journey(_mirror(b), ladder([3937.0, 4029.32, 4029.32]))

    assert len(modifies(b)) == 2, "the hub added or dropped a MODIFY"


# --- CFD -> futures: the master's stop does NOT follow --------------------

def test_the_futures_leg_follows_a_master_modify_as_an_atr_multiple():
    """A master stop-walk now reaches the futures leg, translated rather
    than copied: the master's absolute price would be meaningless on a
    different instrument, so what carries across is the ATR multiple."""
    b = FakeBroker(quote=4050.0)
    journey(_auto(b), ladder([3937.0, 4029.32, 4045.0]))

    assert len(b.opened) == 1, "the futures leg did not open"
    assert len(modifies(b)) == 2, "the stop-walk did not reach the futures leg"


def test_the_futures_stop_moves_in_the_masters_direction_not_just_its_distance():
    """The sign matters. A master moving its stop from 92 points BELOW entry
    to 16 points ABOVE it is protecting profit; read as a bare distance that
    becomes a stop 16 points below entry, which moves this leg's stop the
    wrong way at the worst possible moment."""
    b = FakeBroker(quote=4050.0)
    journey(_auto(b), ladder([3937.0, 4045.0]))   # master stop ends ABOVE entry

    (_ticket, sl, _tp), = modifies(b)
    # master: (4029.32 - 4045) / 18 = -0.871 ATR, i.e. above entry
    # slave:  4050 - (-0.871 * 20) = 4067.42, likewise above its own entry
    assert sl == pytest.approx(4067.42, abs=0.01)
    assert sl > 4050.0, "the futures stop went DOWN while the master's went UP"


def test_the_futures_stop_is_the_masters_atr_multiple_not_its_price():
    """What CFD -> futures translation actually does, as a number.

    Master: XAUUSD long at 4029.32, stop 3937.0, its own iATR = 18.0
            -> the stop is (4029.32 - 3937.0) / 18.0 = 5.1289 ATR away
    Slave:  GC quoted 4050.0, its own ATR from the bar series = 20.0
            -> the same 5.1289 multiple becomes 4050 - 102.58 = 3947.42

    So the futures stop is nowhere near the master's 3937, and should not
    be. It is the same *risk distance* expressed in the futures contract's
    own volatility."""
    b = FakeBroker(quote=4050.0)
    journey(_auto(b), [_sig(action="OPEN", sl=3937.0, price=4029.32, atr=18.0)])

    expected_mult = (4029.32 - 3937.0) / 18.0
    expected_sl = 4050.0 - expected_mult * 20.0
    assert b.opened[0]["sl"] == pytest.approx(expected_sl, abs=0.01)
    assert b.opened[0]["sl"] == pytest.approx(3947.42, abs=0.01)


def test_the_futures_leg_still_follows_a_master_close():
    """MODIFY is ignored, CLOSE is not -- otherwise the futures leg would
    outlive the signal that opened it."""
    b = FakeBroker()
    journey(_auto(b), ladder([3937.0, 4029.32]) + [_sig(action="CLOSE")])

    assert b.closed, "the futures leg was left open after the master closed"


def test_one_master_journey_two_slaves_both_follow_in_their_own_terms():
    """The same ladder fanned to both slave types at once, which is the
    shape of the real fleet: an MT5 mirror and an IBKR futures leg hanging
    off one master. Both follow now; what differs is the arithmetic. The
    mirror gets the master's own prices, the futures leg gets the same risk
    distance expressed in its own instrument's ATR."""
    mirror_b, auto_b = FakeBroker(), FakeBroker(quote=4050.0)
    stops = [3937.0, 4029.32, 4045.0, 4060.0]

    journey(_mirror(mirror_b), ladder(stops))
    journey(_auto(auto_b), ladder(stops))

    assert len(modifies(mirror_b)) == 3, "the mirror leg did not follow the stop"
    assert len(modifies(auto_b)) == 3, "the futures leg did not follow the stop"
    assert len(mirror_b.opened) == len(auto_b.opened) == 1

    # Same instruction, deliberately different numbers.
    assert [m[1] for m in modifies(mirror_b)] == [4029.32, 4045.0, 4060.0]
    assert [m[1] for m in modifies(auto_b)] != [m[1] for m in modifies(mirror_b)]


def test_turning_the_follow_off_restores_the_old_behaviour():
    """It was a deliberate design decision before, so it stays available."""
    b = FakeBroker(quote=4050.0)
    journey(_auto(b, follow_master_modify=False), ladder([3937.0, 4029.32, 4045.0]))

    assert len(b.opened) == 1
    assert modifies(b) == []


def test_a_master_that_sends_no_atr_cannot_be_followed():
    """Without the master's ATR there is no multiple to carry across, and
    guessing one would move a real stop on a real position."""
    b = FakeBroker(quote=4050.0)
    journey(_auto(b), [_sig(action="OPEN", sl=3937.0),
                       _sig(action="MODIFY", sl=4045.0, atr=0.0)])

    assert len(b.opened) == 1
    assert modifies(b) == [], "a MODIFY with no ATR moved the stop anyway"


def test_the_trailing_monitor_can_still_read_its_own_position_key():
    """_check_risk() unpacks the position key, which gained the master
    account. It is reached only from the monitor loop, whose handler logs
    and continues -- so a bad unpack there is silent, and every position's
    breakeven and trailing stop simply stops working."""
    import asyncio as _asyncio

    b = FakeBroker(quote=4050.0)
    e = _auto(b)
    key = ("", 5001, "GC")
    pos = {"slave_ticket": 101, "direction": "BUY", "size": 1.0,
           "entry_price": 4000.0, "atr": 20.0, "breakeven_done": False,
           "month_slot": 0, "sl": 3960.0, "tp": None,
           "sl_mult": 2.0, "tp_mult": None}
    e._positions[key] = pos

    _asyncio.run(e._check_risk(key, pos))   # must not raise ValueError


# --- the registry's view of a journey -------------------------------------

def test_the_registry_holds_one_open_row_through_a_whole_ladder():
    """A MODIFY must not create registry rows. If it did, reconciliation
    would later find rows with no broker position behind them."""
    b = FakeBroker()
    journey(_mirror(b), ladder([3937.0, 4029.32, 4045.0]))

    rows = trade_registry.get_open_mappings(MIRROR, "", 5001)
    assert len(rows) == 1
    assert rows[0].slave_ticket == b.opened[0]["ticket"]
