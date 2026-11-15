"""get_working_orders called the ib_async MODULE, and took the hub down.

2026-11-15 18:04, one minute into the Sunday futures open:

    File "app/adapters/ibkr.py", line 1427, in get_working_orders
        for trade in ib.openTrades():
    AttributeError: module 'ib_async' has no attribute 'openTrades'

    ERROR app.engine_manager: slave=ibkr-paper-1 failed to start;
          the hub is running WITHOUT it
    ERROR app.engine_manager: slave=ibkr-etf-1 failed to start;
          the hub is running WITHOUT it

Both IBKR slaves, with M2K, MBT, MGC, TQQQ and UGL open at the broker
and the week starting. `self._require_ib()` returns the ib_async MODULE;
the connected client is `self.ib`. Everywhere else in that file calls
the module `ib_mod`, so binding it to `ib` here made the wrong line read
exactly right.

WHY NOTHING CAUGHT IT. test_pending_entry_survives_restart.py has nine
tests over this feature and all nine pass against the broken code,
because they drive reconciliation with a fake adapter that has its own
get_working_orders. The real method body was never executed by anything
-- the tests described the feature rather than running it. Same shape as
the switch-guard bug a day earlier: a fake built to match my idea of the
code instead of the code.

So this file executes the real IBKRAdapter method, against a stand-in
for the IB CLIENT rather than for the adapter. Everything it asserts was
already "covered".
"""
import asyncio

import pytest

from app.adapters.base import AdapterError
from app.slaves import SlaveConfig


def _adapter(**cfg):
    from app.adapters import create_adapter
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002, "client_id": 1}
    base.update(cfg)
    return create_adapter(SlaveConfig(id="i", name="i", broker_type="ibkr",
                                      enabled=True, paused=False, config=base))


class _Order:
    def __init__(self, ref=None, perm=None, parent=0):
        self.orderRef = ref
        self.permId = perm
        self.parentId = parent


class _Status:
    def __init__(self, status):
        self.status = status


class _Contract:
    def __init__(self, local="", symbol=""):
        self.localSymbol = local
        self.symbol = symbol


class _Trade:
    def __init__(self, order, status, contract):
        self.order = order
        self.orderStatus = _Status(status)
        self.contract = contract


class _FakeClient:
    """Stands in for ib_async.IB -- the CLIENT, which is the thing the
    method has to be reaching for."""

    def __init__(self, trades):
        self._trades = trades
        self.asked = 0

    def openTrades(self):
        self.asked += 1
        return list(self._trades)


def _run(adapter):
    return asyncio.run(adapter.get_working_orders())


# -------------------------------------------------- the bug itself ------

def test_it_reads_the_connected_client_rather_than_the_module():
    """THE REGRESSION. Against the module this raises AttributeError, and
    that exception reached engine_manager and stopped the slave."""
    a = _adapter()
    a.ib = _FakeClient([_Trade(_Order(ref="m:1:MGC", perm=77),
                               "PreSubmitted", _Contract("MGCZ6", "MGC"))])
    orders = _run(a)
    assert a.ib.asked == 1, "the client was never asked"
    assert [o.order_ref for o in orders] == ["m:1:MGC"]


def test_the_module_really_does_not_have_it():
    """Pinned so the claim in the docstring stays checkable: this is not
    a version quirk, the two objects are simply different things."""
    ib_async = pytest.importorskip("ib_async")
    assert not hasattr(ib_async, "openTrades")
    assert hasattr(ib_async.IB, "openTrades")


def test_disconnected_raises_an_adapter_error_not_an_attribute_error():
    """An adapter that cannot answer must say so in a type reconciliation
    expects. AttributeError was the whole outage."""
    a = _adapter()
    a.ib = None
    with pytest.raises(AdapterError):
        _run(a)


# -------------------------------------------------- what it filters -----

def test_terminal_orders_are_not_working():
    a = _adapter()
    a.ib = _FakeClient([
        _Trade(_Order(ref="live"), "Submitted", _Contract("UGL", "UGL")),
        _Trade(_Order(ref="done"), "Filled", _Contract("UGL", "UGL")),
        _Trade(_Order(ref="gone"), "Cancelled", _Contract("UGL", "UGL")),
        _Trade(_Order(ref="api"), "ApiCancelled", _Contract("UGL", "UGL")),
        _Trade(_Order(ref="dead"), "Inactive", _Contract("UGL", "UGL")),
    ])
    assert [o.order_ref for o in _run(a)] == ["live"]


def test_protective_children_are_not_counted_as_resting_entries():
    """A stop and a target are working orders too. Counting them would
    make every protected position look like it had an unfilled entry --
    and reconciliation would then leave closed rows open forever."""
    a = _adapter()
    a.ib = _FakeClient([
        _Trade(_Order(ref="m:1:MGC", parent=0), "PreSubmitted", _Contract("MGCZ6")),
        _Trade(_Order(ref="m:1:MGC", parent=991), "PreSubmitted", _Contract("MGCZ6")),
        _Trade(_Order(ref="m:1:MGC", parent=991), "PreSubmitted", _Contract("MGCZ6")),
    ])
    assert len(_run(a)) == 1


def test_a_symbol_falls_back_when_there_is_no_local_symbol():
    a = _adapter()
    a.ib = _FakeClient([_Trade(_Order(ref="r"), "Submitted", _Contract("", "UGL"))])
    assert _run(a)[0].symbol == "UGL"


def test_an_order_without_our_reference_still_reports_its_symbol():
    """Reconciliation matches on order_ref first and symbol second, so an
    order placed by hand in TWS must not come back as a blank that
    matches nothing."""
    a = _adapter()
    a.ib = _FakeClient([_Trade(_Order(ref="", perm=5), "Submitted",
                               _Contract("MESZ6", "MES"))])
    found = _run(a)[0]
    assert found.order_ref is None
    assert found.symbol == "MESZ6"


def test_nothing_resting_is_an_empty_list_not_an_error():
    a = _adapter()
    a.ib = _FakeClient([])
    assert _run(a) == []
