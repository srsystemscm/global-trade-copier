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

    async def fake_request(command):
        assert command == {"cmd": "POSITIONS"}
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
