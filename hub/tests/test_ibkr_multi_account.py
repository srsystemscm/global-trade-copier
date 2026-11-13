"""One Gateway login, several IBKR accounts.

The plan is 3-4 MT4 masters feeding separate accounts, which works at the
IBKR level -- several clientIds can share one Gateway, and one login can
hold several accounts -- but the adapter used to assume a single account
everywhere. Two of the three resulting failures were silent: orders get
rejected loudly, while account equity and positions came back blended
across every account on the login.

No Gateway and no ib_async needed: these drive _resolve_account() and
_place() against a stub client.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.adapters import create_adapter
from app.adapters.base import AdapterConnectionError
from app.adapters.ibkr import IBKRAdapter
from app.slaves import SlaveConfig


class _IB:
    """Just the surface _resolve_account/_place/the reads actually touch."""

    def __init__(self, accounts=("DU111111",), positions=()):
        self._accounts = list(accounts)
        self._positions = list(positions)
        self.placed = []
        self.summary_arg = None
        self.positions_arg = None

    def managedAccounts(self):
        return list(self._accounts)

    def placeOrder(self, contract, order):
        self.placed.append((contract, order))
        return SimpleNamespace(order=order)

    async def accountSummaryAsync(self, account=""):
        self.summary_arg = account
        return [SimpleNamespace(tag="TotalCashValue", value="1000.0"),
                SimpleNamespace(tag="NetLiquidation", value="1200.0")]

    def positions(self, account=""):
        self.positions_arg = account
        return list(self._positions)


def _adapter(account=None, ib=None, **kw):
    a = IBKRAdapter("ibkr-1", "127.0.0.1", 4002, client_id=1, account=account, **kw)
    a.ib = ib or _IB()
    return a


def _resolve(adapter):
    asyncio.run(adapter._resolve_account())


# --- config plumbing ------------------------------------------------------

def _slave(**config):
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002, "client_id": 7}
    base.update(config)
    return SlaveConfig(id="ibkr-1", name="IBKR", broker_type="ibkr",
                       enabled=True, paused=False, config=base)


def test_account_passes_through_from_slave_config():
    assert create_adapter(_slave(account="DU999999")).account == "DU999999"


def test_account_defaults_to_unset():
    """Unset is fine for a single-account login -- connect() pins it."""
    assert create_adapter(_slave()).account == ""


# --- picking the account at connect time ----------------------------------

def test_a_single_account_login_pins_itself():
    """Pinned even though IBKR would default to it anyway, so the summary
    and position reads name an account instead of relying on that default."""
    a = _adapter(ib=_IB(accounts=["DU111111"]))
    _resolve(a)
    assert a.account == "DU111111"


def test_a_configured_account_on_the_login_is_kept():
    a = _adapter(account="DU222222", ib=_IB(accounts=["DU111111", "DU222222"]))
    _resolve(a)
    assert a.account == "DU222222"


def test_a_configured_account_the_login_does_not_have_is_refused():
    """Catches the typo at connect time rather than at the first order."""
    a = _adapter(account="DU333333", ib=_IB(accounts=["DU111111", "DU222222"]))
    with pytest.raises(AdapterConnectionError) as exc:
        _resolve(a)
    assert "DU333333" in str(exc.value)
    assert "DU222222" in str(exc.value), "the error should name what IS available"


def test_several_accounts_and_no_config_is_refused():
    """The dangerous case. Unpinned, orders are rejected and both equity and
    positions come back blended -- so refuse to start instead."""
    a = _adapter(ib=_IB(accounts=["DU111111", "DU222222"]))
    with pytest.raises(AdapterConnectionError) as exc:
        _resolve(a)
    msg = str(exc.value)
    assert "2 accounts" in msg
    assert "account" in msg, "the error should say which setting fixes it"


def test_no_managed_accounts_reported_continues_unpinned():
    """The handshake normally delivers these before connectAsync returns. If
    it hasn't, a single-account login still behaves correctly, so refusing
    to start would be worse than carrying on."""
    a = _adapter(ib=_IB(accounts=[]))
    _resolve(a)
    assert a.account == ""


# --- every order names the account ---------------------------------------

def test_placed_orders_carry_the_account():
    ib = _IB()
    a = _adapter(account="DU222222", ib=ib)
    a._place("contract", SimpleNamespace(account=""))
    assert ib.placed[0][1].account == "DU222222"


def test_placed_orders_are_left_alone_when_unpinned():
    """An order with account='' is what a single-account login expects; only
    set it when we actually know which account."""
    ib = _IB()
    a = _adapter(ib=ib)
    a._place("contract", SimpleNamespace(account=""))
    assert ib.placed[0][1].account == ""


def test_every_place_order_call_site_goes_through_place():
    """_place() exists so a new call site cannot omit the account and still
    work on a single-account login while being rejected on a real one. If
    this fails, something started calling ib.placeOrder directly again."""
    import inspect

    src = inspect.getsource(IBKRAdapter)
    direct = [ln.strip() for ln in src.splitlines()
              if "self.ib.placeOrder(" in ln and "def _place" not in ln]
    # exactly one: the single call inside _place itself
    assert len(direct) == 1, "placeOrder called outside _place: %s" % direct


# --- the reads are scoped too --------------------------------------------

def test_account_summary_asks_for_this_account_only():
    """Blended rows are keyed by tag, so with two accounts the last one
    silently wins -- and that equity drives %-risk sizing."""
    ib = _IB()
    a = _adapter(account="DU222222", ib=ib)
    summary = asyncio.run(a.get_account_summary(None))
    assert ib.summary_arg == "DU222222"
    assert (summary.balance, summary.equity) == (1000.0, 1200.0)


def test_open_positions_ask_for_this_account_only():
    """Otherwise reconciliation reads a sibling account's positions as
    untracked positions on this slave."""
    pos = SimpleNamespace(position=1.0, avgCost=4050.0,
                          contract=SimpleNamespace(symbol="GC"))
    ib = _IB(positions=[pos])
    a = _adapter(account="DU222222", ib=ib)
    out = asyncio.run(a.get_open_positions(None))
    assert ib.positions_arg == "DU222222"
    assert [p.symbol for p in out] == ["GC"]


# --- what still needs separate Gateways ----------------------------------

def test_two_slaves_can_share_one_gateway_on_different_client_ids():
    """The architecture check: accounts under ONE login are one Gateway with
    one clientId each. Separate logins still need separate Gateway instances
    on separate ports, which is a deployment fact rather than code -- pinned
    here so the distinction is written down next to the code it affects."""
    a = create_adapter(_slave(account="DU111111", client_id=1))
    b = create_adapter(_slave(account="DU222222", client_id=2))
    assert (a.host, a.port) == (b.host, b.port), "same Gateway"
    assert a.client_id != b.client_id, "one clientId per concurrent session"
    assert a.account != b.account
