"""An MT slave can be priced, which is what autonomous mode needs.

`MT4Adapter.get_quote` raised NotImplementedError with the message
"mirror mode does not support quotes". That was true of the ADAPTER and
not of the bridge: SYMBOLS has carried `bid` and `ask` since SlaveBridge
1.03, for sizing. Nothing was missing but the wiring.

It matters because autonomous mode needs a price to measure the stop
distance from. Without it an autonomous MT slave starts cleanly and then
refuses every signal -- which is how giving mt5-icm-1 routes would have
looked: configured, running, and silently trading nothing.

The MID, not the side being traded. A quote here is the level the stop
is measured FROM, so ask-for-BUY and bid-for-SELL would make the same
stop a different distance by direction -- the spread counted as risk one
way and not the other. Fills still cross the spread; this is the
reference, not the execution.
"""
import asyncio

import pytest

from app.adapters.base import AdapterError
from app.adapters.mt4 import MT4Adapter


def _adapter():
    return MT4Adapter("test-mt", host="127.0.0.1", port=5599, timeout_ms=10)


def _spec(bid, ask):
    return {
        "symbol": "ETHUSD", "exists": True, "digits": 2, "tick_size": 0.01,
        "tick_value": 0.01, "volume_step": 0.01, "volume_min": 0.01,
        "volume_max": 100.0, "bid": bid, "ask": ask,
    }


def _wired(adapter, reply):
    async def _ask(command, name):
        return reply
    adapter._ask_tolerating_old_ea = _ask
    return adapter


def test_the_quote_is_the_mid_of_the_symbols_reply():
    a = _wired(_adapter(), {"symbols": [_spec(1807.50, 1808.50)]})
    assert asyncio.run(a.get_quote("ETHUSD")) == 1808.0


def test_the_quote_does_not_depend_on_direction():
    """The whole reason for the mid: the same stop must be the same
    distance whether the signal is a BUY or a SELL."""
    a = _wired(_adapter(), {"symbols": [_spec(1807.50, 1808.50)]})
    # get_quote takes no direction at all -- that is the guarantee.
    import inspect
    params = inspect.signature(MT4Adapter.get_quote).parameters
    assert "direction" not in params
    assert asyncio.run(a.get_quote("ETHUSD")) == 1808.0


def test_one_side_quoting_is_still_a_price():
    a = _wired(_adapter(), {"symbols": [_spec(1807.50, 0.0)]})
    assert asyncio.run(a.get_quote("ETHUSD")) == 1807.50
    b = _wired(_adapter(), {"symbols": [_spec(0.0, 1808.50)]})
    assert asyncio.run(b.get_quote("ETHUSD")) == 1808.50


def test_a_symbol_quoting_nothing_refuses_rather_than_returning_zero():
    """0.0 would size a position against a stop distance of the entire
    price -- the one answer worse than refusing."""
    a = _wired(_adapter(), {"symbols": [_spec(0.0, 0.0)]})
    with pytest.raises(AdapterError) as got:
        asyncio.run(a.get_quote("ETHUSD"))
    assert "no quote" in str(got.value)
    assert "ETHUSD" in str(got.value)


def test_a_symbol_the_server_does_not_carry_still_says_so():
    """Unchanged from get_symbol_spec: a name the server has never heard
    of is a configuration error, not a missing price."""
    from app.adapters.base import AdapterRejectedError
    a = _wired(_adapter(), {"symbols": [{"symbol": "NOPE", "exists": False}]})
    with pytest.raises(AdapterRejectedError):
        asyncio.run(a.get_quote("NOPE"))


def test_an_old_ea_still_degrades_rather_than_erroring():
    """_ask_tolerating_old_ea turns "unknown cmd" into NotImplementedError
    so callers can fall back. get_quote must not swallow that into
    something that looks like a missing price."""
    a = _adapter()

    async def _old(command, name):
        raise NotImplementedError("slave EA predates the SYMBOLS command")

    a._ask_tolerating_old_ea = _old
    with pytest.raises(NotImplementedError):
        asyncio.run(a.get_quote("ETHUSD"))


def test_price_history_is_still_the_remaining_gap():
    """Honest about what this does NOT finish. Autonomous mode also needs
    the slave's own ATR, and no EA command answers bars yet -- so a
    routed MT slave is still not a working autonomous slave."""
    a = _adapter()
    with pytest.raises(NotImplementedError):
        asyncio.run(a.get_price_history("ETHUSD", 14))
