"""MT4Adapter.get_open_positions(), backed by the bridge EA's POSITIONS command.

Before POSITIONS existed this raised NotImplementedError, so reconcile_slave()
skipped MT4 slaves entirely and a slave_trades row that drifted from the
account could never be corrected. The degradation test below matters as much
as the parsing ones: a hub upgraded ahead of the EAs on someone's desktop is
an ordinary state, not an error.
"""
import asyncio

import pytest

from app.adapters.base import AdapterRejectedError
from app.adapters.mt4 import MT4Adapter


def _adapter(reply=None, raises=None):
    a = MT4Adapter(slave_id="t", host="127.0.0.1", port=1)

    async def fake_request(command, retries=None):
        assert command == {"cmd": "POSITIONS"}
        # a status read must not retry a dead slave -- GET /status is polled
        # every 5s by the dashboard
        assert retries == 0, f"POSITIONS should not retry, got retries={retries!r}"
        if raises is not None:
            raise raises
        return reply

    a._request = fake_request  # type: ignore[assignment]
    return a


def _positions(**kw):
    return asyncio.run(_adapter(**kw).get_open_positions(None))


def test_parses_buy_and_sell_with_signed_quantity():
    got = _positions(reply={
        "status": "ok",
        "positions": [
            {"ticket": 1, "symbol": "XAUUSD", "quantity": 0.10,
             "average_price": 4325.47, "unrealized_pnl": -12.34},
            {"ticket": 2, "symbol": "XAGUSD", "quantity": -0.20,
             "average_price": 63.287, "unrealized_pnl": 5.00},
        ],
    })
    assert [p.symbol for p in got] == ["XAUUSD", "XAGUSD"]
    assert got[0].quantity == 0.10            # BUY  -> positive
    assert got[1].quantity == -0.20           # SELL -> negative
    assert got[0].average_price == 4325.47
    assert got[0].unrealized_pnl == -12.34


def test_flat_account_returns_empty_list():
    assert _positions(reply={"status": "ok", "positions": []}) == []


def test_missing_pnl_is_none_not_zero():
    """None and 0.0 mean different things on a P&L field."""
    got = _positions(reply={
        "status": "ok",
        "positions": [{"ticket": 1, "symbol": "EURUSD", "quantity": 0.01,
                       "average_price": 1.1}],
    })
    assert got[0].unrealized_pnl is None


def test_old_ea_degrades_to_not_implemented():
    """An EA built before POSITIONS answers "unknown cmd". That must come back
    as NotImplementedError, which reconcile_slave() and the /status route both
    already handle as "skip gracefully" -- exactly the pre-POSITIONS behaviour.
    """
    with pytest.raises(NotImplementedError):
        _positions(raises=AdapterRejectedError("unknown cmd 'POSITIONS'"))


def test_other_rejections_are_not_swallowed():
    """Only the unknown-command case degrades; a real rejection must surface."""
    with pytest.raises(AdapterRejectedError):
        _positions(raises=AdapterRejectedError("slave rejected command"))


# --- protective levels (EA mq4 1.04 / mq5 1.01) ---------------------------

def test_sl_and_tp_are_reported_per_leg():
    """The only way to confirm a MODIFY actually landed. Nothing else reads a
    leg's protective levels back off the account."""
    got = _positions(reply={
        "status": "ok",
        "positions": [{"ticket": 1, "symbol": "XAUUSD", "quantity": 0.10,
                       "average_price": 4029.32, "sl": 3947.42, "tp": 4200.0,
                       "unrealized_pnl": 1.0}],
    })
    assert (got[0].sl, got[0].tp) == (3947.42, 4200.0)


def test_a_zero_level_means_unprotected_not_a_stop_at_zero():
    """MQL4's OrderStopLoss() and MQL5's POSITION_SL both return 0.0 when no
    level is set, and the EAs format numbers rather than emitting JSON null.
    Read literally, 0.0 is a stop at a price of zero -- the same trap the
    Signal model guards on the way in."""
    got = _positions(reply={
        "status": "ok",
        "positions": [{"ticket": 1, "symbol": "XAUUSD", "quantity": 0.10,
                       "average_price": 4029.32, "sl": 0.0, "tp": 0.0,
                       "unrealized_pnl": 1.0}],
    })
    assert got[0].sl is None
    assert got[0].tp is None


def test_an_ea_that_omits_the_levels_reports_none():
    """mq4 1.03 and earlier answered POSITIONS without sl/tp. Absent must read
    the same as unset, not raise."""
    got = _positions(reply={
        "status": "ok",
        "positions": [{"ticket": 1, "symbol": "XAUUSD", "quantity": 0.10,
                       "average_price": 4029.32, "unrealized_pnl": 1.0}],
    })
    assert (got[0].sl, got[0].tp) == (None, None)


def test_the_simulator_answers_positions_the_way_the_eas_do():
    """scripts/simulate_slave.py is only useful while it stays in step with
    the real EAs -- it once accepted JSON whitespace the MQL4 parser did not,
    which hid a bug until it reached a real terminal. This drives the
    simulator's own handler through the adapter's parsing."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_sim_slave", Path(__file__).resolve().parent.parent / "scripts" / "simulate_slave.py")
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)

    opened = sim.handle({"cmd": "OPEN", "master_ticket": 1, "symbol": "XAUUSD",
                         "direction": "BUY", "lots": 0.1, "price": 4029.32,
                         "sl": 3937.0, "tp": None})
    ticket = opened["slave_ticket"]
    sim.handle({"cmd": "MODIFY", "ticket": ticket, "sl": 4029.32, "tp": None})

    a = MT4Adapter(slave_id="t", host="127.0.0.1", port=1)

    async def fake_request(command, retries=None):
        return sim.handle({"cmd": "POSITIONS"})

    a._request = fake_request  # type: ignore[assignment]
    got = asyncio.run(a.get_open_positions(None))

    assert len(got) == 1
    assert got[0].sl == 4029.32, "the stop-walk is not visible through POSITIONS"
    assert got[0].tp is None, "an unset TP came back as a level"

    # and the already-closed contract both EAs honour since mq4 1.02
    sim.handle({"cmd": "CLOSE", "ticket": ticket})
    assert sim.handle({"cmd": "MODIFY", "ticket": ticket, "sl": 1.0}) == {
        "status": "ok", "already_closed": True, "slave_ticket": ticket}
