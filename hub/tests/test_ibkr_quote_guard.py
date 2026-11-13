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


# ---------------- a root with two contract sizes ------------------------

def test_a_trading_class_spec_skips_contfuture():
    """ContFuture ignores tradingClass. Probed live:
    ContFuture("SI", tradingClass="SIL") resolves to SIZ6, multiplier 5000 --
    the 5,000 ounce contract, which quotes and fills perfectly happily while
    being five times the intended size. reqContractDetails does honour it,
    so a two-size root resolves from the dated list instead."""
    import inspect
    import re
    from app.adapters.ibkr import IBKRAdapter

    src = re.sub(r"\s+", " ", inspect.getsource(IBKRAdapter._qualified_contract))
    assert "spec.trading_class" in src
    assert "_dated_contract" in src
    # and the branch has to come before the ContFuture path, or it never runs
    assert src.index("_dated_contract") < src.index("_contract_for(symbol)")


def test_the_month_list_filters_by_trading_class():
    """Belt and braces behind the request filter: one root carrying two
    sizes must not mix them into one cycle, or a month_slot walk steps from
    a micro contract into a full-size one."""
    import inspect
    import re
    from app.adapters.ibkr import IBKRAdapter

    src = re.sub(r"\s+", " ", inspect.getsource(
        IBKRAdapter._liquid_dated_contracts))
    assert "spec.trading_class and k.tradingClass != spec.trading_class" in src


def test_the_contract_request_names_the_ibkr_symbol():
    """A route says SIL; IBKR has never heard of it."""
    import inspect
    import re
    from app.adapters.ibkr import IBKRAdapter

    for method in (IBKRAdapter._contract_for,
                   IBKRAdapter._liquid_dated_contracts):
        src = re.sub(r"\s+", " ", inspect.getsource(method))
        assert "spec.ib_symbol or symbol" in src, method.__name__


# ---------------- an unqualifiable contract says so ---------------------

def test_an_unqualified_contract_is_dropped_not_dereferenced():
    """ib_async returns a list the same length as the request, with None
    where IBKR refused -- so `if not qualified` passes on [None] and the
    next line raises AttributeError. Preflight caught this: an unroutable
    MNG reported "'NoneType' object has no attribute 'conId'" instead of
    saying IBKR has never heard of the contract."""
    from app.adapters.ibkr import IBKRAdapter

    assert IBKRAdapter._qualified_only([None]) == []
    assert IBKRAdapter._qualified_only(None) == []
    assert IBKRAdapter._qualified_only([]) == []
    keeper = object()
    assert IBKRAdapter._qualified_only([None, keeper]) == [keeper]


def test_every_qualify_call_filters_its_result():
    """Three call sites, and any one of them left raw turns a clear refusal
    into an AttributeError."""
    import inspect
    import re
    from app.adapters.ibkr import IBKRAdapter

    src = re.sub(r"\s+", " ", inspect.getsource(IBKRAdapter))
    raw = len(re.findall(r"(?<!_qualified_only\()(?<!\()await self\.ib\.qualifyContractsAsync", src))
    wrapped = src.count("_qualified_only(")
    assert "qualifyContractsAsync" in src
    # the helper's own definition plus one use per call site
    assert wrapped >= 4, "a qualifyContracts call site is unfiltered"
