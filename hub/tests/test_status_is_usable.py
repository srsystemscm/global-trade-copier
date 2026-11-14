"""/status has to render even when a broker does not answer.

Seen on 2026-11-13, a Friday evening with the Darwinex terminal shut: the
dashboard showed "Loading master status..." and never resolved. /status
returned nothing at all after 120 seconds -- no status code, no body --
because it fetched every slave's positions one after another and the MT4
adapter's own timeout is 45 seconds. That timeout is right for a copy that
must not be abandoned halfway and far too long for a page poll.

Two faults, one symptom: no bound on a single slave, and no concurrency
across them, so the slowest slave set the page's speed and several slow
ones added up.

Also pinned here: a mirror slave's `sizing` reaches the API. It used to be
reported for autonomous slaves only, from when a mirror slave had no sizing
of its own. One with a `sizing` block now sizes from its own capital, so
omitting it hid a setting that decides what actually gets traded.
"""
import asyncio
import time
import types

import pytest

from app import api_routes
from app.adapters.base import AdapterError
from app.slaves import SlaveConfig


class _Pos:
    def __init__(self, symbol, quantity):
        self.symbol = symbol
        self.quantity = quantity
        self.average_price = 1.0
        self.market_value = None
        self.unrealized_pnl = None


class _Adapter:
    """A broker that takes `delay` seconds to answer, or raises."""

    def __init__(self, delay=0.0, positions=(), error=None):
        self.delay, self._positions, self.error = delay, list(positions), error

    async def get_open_positions(self, account_type=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self._positions


class _Engine:
    def __init__(self, slave_id, adapter):
        self.slave_id = slave_id
        self.adapter = adapter
        self.paused = False
        self.watchdog = types.SimpleNamespace(connected=True)


def _request(*engines):
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(engines=list(engines))))


def _slave(slave_id, config=None, broker_type="mt4"):
    return SlaveConfig(id=slave_id, name=slave_id, broker_type=broker_type,
                       enabled=True, paused=False,
                       config=config or {"mode": "mirror"})


# ---------------------------------------------------- one slow slave

def test_a_slave_that_never_answers_does_not_hang_the_summary():
    adapter = _Adapter(delay=30.0)          # the MT4 timeout is 45s
    req = _request(_Engine("dwx", adapter))

    started = time.monotonic()
    summary = asyncio.run(api_routes._slave_summary(req, _slave("dwx")))
    elapsed = time.monotonic() - started

    assert elapsed < api_routes.POSITIONS_TIMEOUT_S + 2.0, (
        f"waited {elapsed:.1f}s on one unresponsive broker")
    assert summary["positions"] is None
    assert "terminal" in (summary["positions_error"] or ""), (
        "the page must say which slave went quiet, not just fail")


def test_a_slow_slave_does_not_hide_the_others():
    """The whole point. One shut terminal must not cost the other five."""
    slow = _Engine("dwx", _Adapter(delay=30.0))
    quick = _Engine("icm", _Adapter(positions=[_Pos("XAUUSD", 1.0)]))
    req = _request(slow, quick)

    async def go():
        return await asyncio.gather(
            api_routes._slave_summary(req, _slave("dwx")),
            api_routes._slave_summary(req, _slave("icm")),
        )

    started = time.monotonic()
    dwx, icm = asyncio.run(go())
    elapsed = time.monotonic() - started

    assert elapsed < api_routes.POSITIONS_TIMEOUT_S + 2.0
    assert dwx["positions"] is None and dwx["positions_error"]
    assert icm["positions"] == [{
        "symbol": "XAUUSD", "quantity": 1.0, "average_price": 1.0,
        "market_value": None, "unrealized_pnl": None,
    }], "a healthy slave reported nothing because a different one was slow"


def test_a_broker_error_is_still_reported_as_itself():
    """The timeout must not swallow the errors that already worked."""
    adapter = _Adapter(error=AdapterError("bridge said no"))
    req = _request(_Engine("icm", adapter))

    summary = asyncio.run(api_routes._slave_summary(req, _slave("icm")))

    assert summary["positions_error"] == "bridge said no"


# ---------------------------------------------------- what the UI can see

def test_a_mirror_slave_reports_the_sizing_it_actually_uses():
    sizing = {"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance"}
    slave = _slave("icm", {"mode": "mirror", "sizing": sizing})

    summary = asyncio.run(api_routes._slave_summary(_request(), slave))

    assert summary["sizing"] == sizing, (
        "a mirror slave sizing from its own capital showed no sizing at all")


def test_the_settings_that_refuse_a_trade_are_visible():
    """netting.allow_opposing decides whether a hedged leg copies at all,
    and margin.max_pct_of_free decides whether a size is cut. Both were
    absent from every screen."""
    slave = _slave("dwx", {
        "mode": "mirror",
        "netting": {"allow_opposing": True},
        "margin": {"max_pct_of_free": 0.25},
    })

    summary = asyncio.run(api_routes._slave_summary(_request(), slave))

    assert summary["netting"] == {"allow_opposing": True}
    assert summary["margin"] == {"max_pct_of_free": 0.25}


def test_absent_blocks_report_as_null_rather_than_empty():
    """An empty dict reads on screen as though something were configured."""
    summary = asyncio.run(api_routes._slave_summary(
        _request(), _slave("icm", {"mode": "mirror", "netting": {}})))

    assert summary["netting"] is None
    assert summary["margin"] is None
