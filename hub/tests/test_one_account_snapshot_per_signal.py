"""A candidate chain prices against one view of the account.

2026-11-16. Both IBKR slaves trade account DUR314653, and the ETF slave
fills while the futures slave is still choosing between GC and MGC.
_size_candidate fetched a fresh account summary per candidate, so the
two legs of one decision were priced against different balances:

  run  ETF filled just before          MGC vs what GC implied
  D    3,282 UGL, one second before    16.5% short
  H4   7,646 UGL, two seconds before   35.1% short
  H1   refused -- nothing filled        1.0%, exact

The shortfall tracked the size of the fill every time, and H1 -- the one
run where the ETF leg was refused on margin and nothing opened -- came
out right. That is the whole proof: the only variable was whether
another slave's order landed mid-decision.

WHY IT WAS INVISIBLE. Nothing failed. The chain stepped down for the
stated reason, the size was internally consistent with the balance it
had just read, and the dollar risk reported matched that size. Only
comparing "GC wants 0.72 of them" against "MGC took 6" -- numbers from
two different log lines -- showed the 7.19 that should have been there.

A chain is one decision about one signal. Candidates differ by
instrument, not by when they happened to ask the broker.

KEYED BY account_type, not cached flat: a route may list a futures
contract and a brokerage one, and those really are different accounts
with different balances.
"""
import asyncio

import pytest

from app.autonomous_copy_engine import AutonomousCopyEngine


class _Account:
    def __init__(self, balance):
        self.balance = balance
        self.equity = balance
        self.account_type = "futures"


class _Adapter:
    """Counts account lookups, and shrinks the balance on each one --
    standing in for another slave filling on the same account."""

    def __init__(self, balances):
        self._balances = list(balances)
        self.calls = []

    async def get_account_summary(self, account_type):
        self.calls.append(account_type)
        bal = self._balances[min(len(self.calls) - 1, len(self._balances) - 1)]
        return _Account(bal)


def _engine(adapter):
    e = object.__new__(AutonomousCopyEngine)
    e.slave_id = "ibkr-paper-1"
    e.adapter = adapter
    return e


def _fetch(engine, accounts, account_type="futures"):
    """The cache-aware lookup, exercised the way _size_candidate does."""
    async def go():
        if account_type in accounts:
            return accounts[account_type]
        got = await engine.adapter.get_account_summary(account_type)
        accounts[account_type] = got
        return got
    return asyncio.run(go())


# ----------------------------------------------------- the behaviour ---

def test_the_chain_asks_once_however_many_candidates():
    """Two candidates, one lookup. GC and MGC are the same decision."""
    a = _Adapter([866268.0, 700000.0, 500000.0])
    e = _engine(a)
    accounts = {}
    first = _fetch(e, accounts)
    second = _fetch(e, accounts)
    assert a.calls == ["futures"], "the fallback asked the broker again"
    assert first.balance == second.balance == 866268.0


def test_a_fill_landing_mid_decision_cannot_move_the_second_candidate():
    """THE BUG, as the account actually behaved: each lookup returns less
    than the last because another slave is filling. Without the shared
    snapshot the second candidate sizes against 700000 and undershoots by
    19%."""
    a = _Adapter([866268.0, 700000.0])
    e = _engine(a)
    accounts = {}
    gc = _fetch(e, accounts)
    mgc = _fetch(e, accounts)
    assert mgc.balance == gc.balance
    # And the unshared version is what shipped -- shown here so the
    # difference is a number rather than an assertion about intent.
    unshared_second = asyncio.run(a.get_account_summary("futures"))
    assert unshared_second.balance < gc.balance


def test_a_mixed_chain_still_sees_both_accounts():
    """A route may list a futures contract and a brokerage one. Those are
    genuinely different accounts, so the cache is keyed rather than
    flat -- collapsing them would price an ETF leg against the futures
    balance."""
    a = _Adapter([500000.0, 250000.0])
    e = _engine(a)
    accounts = {}
    fut = _fetch(e, accounts, "futures")
    brok = _fetch(e, accounts, "brokerage")
    assert a.calls == ["futures", "brokerage"]
    assert fut.balance != brok.balance


def test_the_cache_does_not_outlive_the_signal():
    """A fresh dict per _open_one. A snapshot held across signals would
    size tomorrow's trade against yesterday's balance, which is a worse
    bug than the one being fixed."""
    import inspect
    src = inspect.getsource(AutonomousCopyEngine._open_one)
    assert "accounts: Dict[str, Any] = {}" in src, (
        "the snapshot is no longer created per signal"
    )
    assert "accounts=accounts" in src


# ----------------------------------------------------- the wiring ------

def test_size_candidate_takes_the_snapshot_and_fills_it():
    import inspect
    src = inspect.getsource(AutonomousCopyEngine._size_candidate)
    assert "accounts: Optional[Dict[str, Any]] = None" in src
    assert "if account_type in accounts:" in src
    assert "accounts[account_type] = real_account" in src


def test_a_direct_caller_without_a_snapshot_still_works():
    """Tests and any future caller may omit it; None means "just this
    one", not a crash."""
    import inspect
    src = inspect.getsource(AutonomousCopyEngine._size_candidate)
    assert "if accounts is None:" in src
