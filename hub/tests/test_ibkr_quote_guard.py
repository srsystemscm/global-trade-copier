"""get_quote() must reject IBKR's no-market sentinels.

Found live: GLD quoted -1.0 outside the US cash session while GC (nearly 24h)
quoted fine. The original guard only caught NaN, so -1.0 passed through as a
valid price -- and get_quote feeds both sizing (which divides by it) and the
ATR-derived SL/TP, so a negative price is worse than an outright failure.
"""
import asyncio

import pytest

from app.adapters.base import AdapterRejectedError
from app.adapters.ibkr import IBKRAdapter


class _Ticker:
    def __init__(self, price):
        self._price = price

    def marketPrice(self):
        return self._price


class _FakeIB:
    def __init__(self, price):
        self._price = price

    def reqMktData(self, *a, **k):
        return _Ticker(self._price)

    def cancelMktData(self, *a, **k):
        pass


def _quote(price):
    a = IBKRAdapter("t", "127.0.0.1", 4002, client_id=1, quote_timeout=0)
    a.ib = _FakeIB(price)

    async def contract(symbol):
        return object()

    a._qualified_contract = contract  # type: ignore[assignment]
    return asyncio.run(a.get_quote("GC"))


def test_positive_price_is_returned():
    assert _quote(4049.4) == 4049.4


@pytest.mark.parametrize("bad", [-1.0, 0.0, -0.01, float("nan")])
def test_no_market_sentinels_are_rejected(bad):
    with pytest.raises(AdapterRejectedError):
        _quote(bad)


def test_error_names_the_value_it_got():
    with pytest.raises(AdapterRejectedError, match=r"-1\.0"):
        _quote(-1.0)
