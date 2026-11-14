"""A mirror slave has to be able to name the account it trades.

Reported by the ops hub on 2026-11-14: `status.slaves[].account` was empty
for both MT slaves while the IBKR ones carried theirs. The hub ties a slave
-- and later each of its legs -- to its own ledger by account number, the
same key the Desk Managers report, so a blank one means the live futures
slave's legs cannot be joined to anything.

The bridge had always sent it. SlaveBridge.mq5's ACCOUNT reply carries
`login` from AccountInfoInteger(ACCOUNT_LOGIN); the adapter read `balance`
and `equity` out of that reply and dropped the rest.

`null` until the EA has said, never "" and never 0: the hub reads null as
"not known yet" and anything else as an answer.
"""
import asyncio

import pytest

from app.adapters import create_adapter
from app.slaves import SlaveConfig


def _adapter(**cfg):
    base = {"mode": "mirror", "host": "127.0.0.1", "port": 5599, "timeout_ms": 500}
    base.update(cfg)
    return create_adapter(SlaveConfig(id="mt", name="mt", broker_type="mt4",
                                      enabled=True, paused=False, config=base))


def _answering(adapter, reply):
    """Replace the wire with a canned ACCOUNT reply."""
    async def _ask(command, label):
        return reply
    adapter._ask_tolerating_old_ea = _ask
    return adapter


def test_the_account_is_unknown_before_the_ea_answers():
    a = _adapter()
    assert a.account is None, "an unasked adapter must not claim an account"


def test_the_login_is_kept_from_the_account_reply():
    a = _answering(_adapter(), {"login": 4000068633, "balance": "1000", "equity": "1000"})
    asyncio.run(a.get_account_summary(""))
    assert a.account == "4000068633"


def test_the_login_is_a_string_not_a_number():
    """It is an identifier, not a quantity. MT5 logins are long enough that
    a JSON number is a precision question nobody should have to think
    about, and the hub joins on it as text."""
    a = _answering(_adapter(), {"login": 53116707, "balance": "1", "equity": "1"})
    asyncio.run(a.get_account_summary(""))
    assert a.account == "53116707"
    assert isinstance(a.account, str)


@pytest.mark.parametrize("login", [None, "", 0])
def test_an_empty_login_leaves_it_unknown(login):
    """"" and 0 would read to the hub as a real answer. They are not."""
    a = _answering(_adapter(), {"login": login, "balance": "1", "equity": "1"})
    asyncio.run(a.get_account_summary(""))
    assert a.account is None


def test_an_old_ea_that_sends_no_login_does_not_break_sizing():
    """The reply predates the field. Balance and equity still have to work,
    because that is what a mirror slave sizes from."""
    a = _answering(_adapter(), {"balance": "2500.5", "equity": "2490.25"})
    summary = asyncio.run(a.get_account_summary(""))
    assert (summary.balance, summary.equity) == (2500.5, 2490.25)
    assert a.account is None


def test_a_failure_at_connect_does_not_stop_the_slave():
    """One round trip at connect is a convenience, not a dependency. An EA
    that cannot answer must not stop the slave from starting."""
    a = _adapter()

    async def _boom(command, label):
        raise RuntimeError("no ACK")

    a._ask_tolerating_old_ea = _boom
    asyncio.run(a._learn_account())
    assert a.account is None


def test_the_snapshot_reports_what_the_adapter_learned():
    """_slave_account asks the adapter first, which is the live truth."""
    from app.hub_reporter.snapshot import _slave_account

    class _Engine:
        def __init__(self, account):
            self.adapter = type("A", (), {"account": account})()

    slave = SlaveConfig(id="mt", name="mt", broker_type="mt4", enabled=True,
                        paused=False, config={"mode": "mirror"})
    assert _slave_account(slave, _Engine("53116707")) == "53116707"
    assert _slave_account(slave, _Engine(None)) is None
