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
    """A ticker with nothing but marketPrice, as an unsubscribed one is.

    last and close are None here on purpose: get_quote falls back to them
    when marketPrice is NaN, and the point of these cases is that there is
    no price available at all.
    """

    def __init__(self, price, last=None, close=None):
        self._price = price
        self.last = last
        self.close = close

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

    async def contract(symbol, month_slot=0):
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


def test_nan_falls_back_to_the_last_trade_when_there_is_one():
    """NaN means "nothing has arrived yet", not "there is no market". With
    delayed data it is routine even on an open session, and refusing on it
    dropped MES while CME was open -- US500 produced no futures leg at all.
    A -1 is different: that is IBKR stating there is no market, and no
    fallback should rescue it.
    """
    a = IBKRAdapter("t", "127.0.0.1", 4002, client_id=1, quote_timeout=0)

    class _IB:
        def __init__(self, ticker):
            self._t = ticker

        def reqMktData(self, *a, **k):
            return self._t

        def cancelMktData(self, *a, **k):
            pass

    async def contract(symbol, month_slot=0, contract_id=None):
        return object()

    a._contract_for_request = contract

    a.ib = _IB(_Ticker(float("nan"), last=5432.25))
    assert asyncio.run(a.get_quote("MES")) == 5432.25

    a.ib = _IB(_Ticker(float("nan"), last=None, close=5400.0))
    assert asyncio.run(a.get_quote("MES")) == 5400.0

    # -1 is not rescued by a stale close
    a.ib = _IB(_Ticker(-1.0, last=5432.25, close=5400.0))
    with pytest.raises(AdapterRejectedError):
        asyncio.run(a.get_quote("SPXL"))


# ---------------- contention, not latency --------------------------------
#
# US500 produced no futures leg in two consecutive live runs: MES came back
# NaN both times, in the same instant as the ETF slave's own quote request.
# Probed alone straight afterwards it quoted 7603.25 within a second, so it
# was neither a timeout nor a missing subscription -- it was two engines on
# one account asking at once, with IBKR delivering nothing to the loser.

class _Sequence:
    """Returns a scripted series of prices, one per sampling attempt."""

    def __init__(self, *prices):
        self.prices = list(prices)
        self.calls = 0

    def __call__(self, contract, symbol):
        self.calls += 1
        value = self.prices.pop(0) if self.prices else float("nan")

        async def result():
            return value

        return result()


def _adapter_with(sequence, retries=2):
    a = IBKRAdapter("t", "127.0.0.1", 4002, client_id=1, quote_timeout=0,
                    quote_retries=retries)
    a.ib = object()

    async def contract(symbol, month_slot=0, contract_id=None):
        return object()

    a._contract_for_request = contract
    a._sample_quote = sequence
    return a


def test_an_empty_quote_is_retried_and_succeeds():
    seq = _Sequence(float("nan"), 7603.25)
    a = _adapter_with(seq)
    assert asyncio.run(a.get_quote("MES")) == 7603.25
    assert seq.calls == 2


def test_retries_are_bounded():
    seq = _Sequence(float("nan"), float("nan"), float("nan"), 1.0)
    a = _adapter_with(seq, retries=2)
    with pytest.raises(AdapterRejectedError):
        asyncio.run(a.get_quote("MES"))
    assert seq.calls == 3, "should try once plus two retries"


def test_a_first_attempt_that_works_costs_nothing_extra():
    seq = _Sequence(7603.25)
    a = _adapter_with(seq)
    assert asyncio.run(a.get_quote("MES")) == 7603.25
    assert seq.calls == 1


def test_a_no_market_sentinel_is_not_retried():
    """-1 is IBKR stating there is no market. It will still be true in a
    second, and retrying would multiply the wait by every closed route on
    the slave -- all fourteen ETF ones, overnight."""
    seq = _Sequence(-1.0, 7603.25)
    a = _adapter_with(seq)
    with pytest.raises(AdapterRejectedError):
        asyncio.run(a.get_quote("SPXL"))
    assert seq.calls == 1, "retried a market IBKR says does not exist"


def test_retries_can_be_switched_off():
    seq = _Sequence(float("nan"), 7603.25)
    a = _adapter_with(seq, retries=0)
    with pytest.raises(AdapterRejectedError):
        asyncio.run(a.get_quote("MES"))
    assert seq.calls == 1
