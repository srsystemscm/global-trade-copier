"""Schwab had the same orphan bug IBKR did.

Both keep per-position state in memory -- the account an order went to, the
broker-side symbol, the bracket's child order ids. A hub restart left a live
position that modify() and close() both reject. IBKR was fixed when the
durable store went in; Schwab was not, and would have failed the same way the
moment a Schwab slave was used.
"""
import asyncio
import inspect

from app.adapters.schwab import SchwabAdapter
from app.schwab_auth import SchwabAuth


def _adapter():
    auth = SchwabAuth("id", "secret", "https://x/cb", "https://x/oauth")
    return SchwabAdapter("s1", auth, "FUT-1", "BRK-1", "https://x/api", "https://x/mkt")


def test_a_futures_position_restores_onto_the_futures_account():
    a = _adapter()
    asyncio.run(a.restore_position(77, {
        "slave_symbol": "MGC", "direction": "BUY", "size": 1.0,
        "sl": 2300.0, "tp": None, "sl_order_id": 91, "tp_order_id": None,
    }))
    pos = a._positions[77]
    assert pos["account_id"] == "FUT-1"
    assert pos["is_future"] is True
    assert pos["symbol"] == "/MGC", "Schwab futures symbols carry a leading slash"
    assert pos["stop_order_id"] == 91


def test_an_equity_position_restores_onto_the_brokerage_account():
    a = _adapter()
    asyncio.run(a.restore_position(78, {
        "slave_symbol": "GLD", "direction": "SELL", "size": 20.0,
        "sl": None, "tp": 300.0, "sl_order_id": None, "tp_order_id": 92,
    }))
    pos = a._positions[78]
    assert pos["account_id"] == "BRK-1"
    assert pos["is_future"] is False
    assert pos["symbol"] == "GLD"
    assert pos["limit_order_id"] == 92


def test_direction_and_size_survive():
    a = _adapter()
    asyncio.run(a.restore_position(79, {
        "slave_symbol": "GLD", "direction": "SELL", "size": 20.0,
    }))
    assert (a._positions[79]["direction"], a._positions[79]["size"]) == ("SELL", 20.0)


def test_a_row_with_no_symbol_is_skipped_rather_than_crashing():
    a = _adapter()
    asyncio.run(a.restore_position(80, {"direction": "BUY", "size": 1.0}))
    assert 80 not in a._positions


def test_open_reports_its_bracket_ids_so_they_can_be_persisted():
    """Without these on OrderResult there is nothing to restore from."""
    src = inspect.getsource(SchwabAdapter.open)
    assert "sl_order_id=" in src and "tp_order_id=" in src
