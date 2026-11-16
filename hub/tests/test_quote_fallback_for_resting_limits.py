"""IBKR quoting -1 does not always mean the instrument is not trading.

Sunday 2026-11-15, 22:38 ET. Chris: "UGL is trading EXO hours on schwab
right now" -- inside the 24/5 overnight session. At that same moment the
hub's own log carried, from IBKR's account feed:

    22:36:15 updatePortfolio: ... symbol='UGL' ... marketPrice=39.21554945

while reqMktData returned -1 for the very same contract, and the ETF
legs were refused with "no quote ... A negative price means IBKR has no
market for it right now -- usually the instrument's session is closed."

That message was written from a GLD observation and is right about a
futures leg outside the cash session. It is wrong here: IBKR holds a
price for UGL, the instrument is trading, and what is missing is a quote
on this particular market-data line.

The -1 used to fall straight through to the refusal -- the last/close
fallback only ran on NaN. So an ETF entry could not be priced in exactly
the session it was configured to trade in.

WHY THIS IS PER SLAVE AND OFF BY DEFAULT. The two cases are genuinely
different:

  resting limit   outside_rth, GTD. A stale anchor costs a MISSED fill,
                  never a bad one -- a BUY limit priced below a market
                  that has risen simply does not fill.
  market order    A stale close is a genuinely bad fill. Futures slaves
                  keep the strict refusal.

Sizing is unaffected either way: the stop distance is ATR-derived, so a
stale quote moves the limit's anchor and not the risk.
"""
import pytest

from app.slaves import SlaveConfig


def _adapter(**cfg):
    from app.adapters import create_adapter
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002, "client_id": 1}
    base.update(cfg)
    return create_adapter(SlaveConfig(id="i", name="i", broker_type="ibkr",
                                      enabled=True, paused=False, config=base))


class _Ticker:
    """Stands in for ib_async's Ticker."""

    def __init__(self, market, last=None, close=None):
        self._market = market
        self.last = last
        self.close = close

    def marketPrice(self):
        return self._market


class _FakeIB:
    def __init__(self, ticker):
        self._ticker = ticker
        self.cancelled = 0

    def reqMktData(self, contract, *a, **k):
        return self._ticker

    def cancelMktData(self, contract):
        self.cancelled += 1


async def _sample(adapter, ticker):
    adapter.ib = _FakeIB(ticker)
    adapter.quote_timeout = 0.0
    return await adapter._sample_quote(object(), "UGL")


def _run(adapter, ticker):
    import asyncio
    return asyncio.run(_sample(adapter, ticker))


NAN = float("nan")


# ------------------------------------------------- the default ---------

def test_strict_is_the_default():
    """A futures slave must not start pricing market orders off a stale
    close because a setting was added for the ETF one."""
    assert _adapter().quote_fallback == "strict"


def test_strict_still_refuses_a_negative_quote():
    """IBKR's -1 on a slave that sends market orders is still fatal: the
    alternative is sizing dividing by a stale number."""
    price = _run(_adapter(), _Ticker(-1.0, last=39.2, close=39.18))
    assert price == -1.0, "the -1 must survive to _require_usable_quote"


# ------------------------------------------------- the fallback --------

def test_last_close_prices_a_negative_quote_from_the_last_trade():
    """THE CASE. UGL at 22:38 on a Sunday: trading, priced by IBKR's
    account feed, and quoted -1 on the market-data line."""
    price = _run(_adapter(quote_fallback="last_close"),
                 _Ticker(-1.0, last=39.2155, close=39.18))
    assert price == pytest.approx(39.2155)


def test_it_falls_back_to_the_close_when_there_is_no_last():
    price = _run(_adapter(quote_fallback="last_close"),
                 _Ticker(-1.0, last=None, close=39.18))
    assert price == pytest.approx(39.18)


def test_a_live_quote_is_always_preferred_to_either():
    """The fallback must not shadow a real quote once the session opens."""
    for mode in ("strict", "last_close"):
        price = _run(_adapter(quote_fallback=mode),
                     _Ticker(39.5, last=39.2155, close=39.18))
        assert price == pytest.approx(39.5)


def test_nothing_usable_anywhere_still_fails():
    """A fallback that invents a price would be worse than the refusal it
    replaces."""
    price = _run(_adapter(quote_fallback="last_close"),
                 _Ticker(-1.0, last=None, close=None))
    assert price == -1.0
    price = _run(_adapter(quote_fallback="last_close"),
                 _Ticker(-1.0, last=0.0, close=-1.0))
    assert price == -1.0, "zero and negative are not prices"


# ------------------------------- the pre-existing NaN path is untouched -

def test_a_nan_still_falls_back_on_a_strict_slave():
    """This predates the change and must not regress: with delayed data a
    NaN is common on an OPEN session, and refusing on it dropped MES at
    00:42 while US500 produced no futures leg at all."""
    price = _run(_adapter(), _Ticker(NAN, last=5812.25, close=5810.0))
    assert price == pytest.approx(5812.25)


def test_the_subscription_is_always_released():
    """One leaked market-data line per refused quote would exhaust the
    account's allowance, and the failure that follows looks like a
    different bug entirely."""
    a = _adapter(quote_fallback="last_close")
    a.quote_timeout = 0.0
    import asyncio
    for ticker in (_Ticker(39.5), _Ticker(-1.0, close=39.1), _Ticker(NAN)):
        a.ib = _FakeIB(ticker)
        asyncio.run(a._sample_quote(object(), "UGL"))
        assert a.ib.cancelled == 1
