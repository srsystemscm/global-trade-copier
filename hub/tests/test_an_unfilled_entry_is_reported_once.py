"""Two paths find an unfilled entry. One alert.

MEASURED. hub.log, 2026-11-16, ibkr-etf-1, UGL 1193 -- an entry placed
at 20:41, past the 20:00 ET extended-hours close, so no US equity
session existed for it to fill in:

    20:42:21 WARNING  cancelled unfilled entry 1193 (1 slice(s)) and its
                      protective legs
    20:42:21 WARNING  entry 1193 for UGL never filled; cancelled it and its
                      protective legs instead of placing a closing order ...
    20:42:51 ERROR    Slave ibkr-etf-1 never filled UGL for
                      master_ticket=382970843; the order and its protective
                      legs have been cancelled. The master IS in this trade
                      and this leg is NOT.

One event, 30 seconds apart, reported twice -- and the second one sends
its own notification. _handle_close gets there when the master closes
before the entry fills; the background fill-capture task gets there when
its own wait runs out. Both are right to ask. Only the first has
anything to tell anyone.

This alert in particular has to stay worth reading. It is the one that
says the master is in a trade and this account is not, which is the most
expensive thing the copier can have to say.

The REGISTRY decides, because it is the only thing that knows: the
UPDATE carries `status = 'OPEN'`, so a second call matches no rows. That
is a fact about what happened rather than a guess about who got here
first.
"""
import pytest

from app import trade_registry


def _open_row():
    trade_registry.record_open(
        "ibkr-etf-1", "44964870", 382970843, "UGL", 1193,
        slave_lots=1492.0,
    )


# ------------------------------------------------- the registry ---------

def test_the_first_cancel_reports_that_it_moved_the_row():
    _open_row()
    assert trade_registry.record_entry_cancelled(
        "ibkr-etf-1", "44964870", 382970843, "UGL") is True


def test_the_second_cancel_reports_that_it_did_not():
    """THE BUG, at the level that can answer it. Nothing else knows
    whether this ticket has already been through here -- the adapter has
    dropped its slices, the engine's dict entry is gone, and both paths
    arrive holding exactly the same information."""
    _open_row()
    trade_registry.record_entry_cancelled("ibkr-etf-1", "44964870", 382970843, "UGL")
    assert trade_registry.record_entry_cancelled(
        "ibkr-etf-1", "44964870", 382970843, "UGL") is False


def test_cancelling_a_row_that_was_never_opened_reports_nothing_moved():
    assert trade_registry.record_entry_cancelled(
        "ibkr-etf-1", "44964870", 999, "UGL") is False


def test_the_row_still_ends_up_cancelled():
    """The answer is about reporting, not about the write. The row has to
    be CANCELLED either way -- it is what stops reconciliation claiming a
    position the broker never had."""
    _open_row()
    trade_registry.record_entry_cancelled("ibkr-etf-1", "44964870", 382970843, "UGL")
    trade_registry.record_entry_cancelled("ibkr-etf-1", "44964870", 382970843, "UGL")
    assert not trade_registry.get_open_mappings(
        "ibkr-etf-1", "44964870", 382970843), (
        "the row is still OPEN after being cancelled"
    )


def test_a_closed_row_is_not_reopened_as_cancelled():
    """CANCELLED and CLOSED mean different things -- "never existed"
    versus "closed without a captured price" -- and reconciliation
    cannot tell a missed fill from a missing measurement if this path
    overwrites one with the other."""
    _open_row()
    trade_registry.record_close(
        "ibkr-etf-1", "44964870", 382970843, "UGL", slave_close_price=38.71)
    assert trade_registry.record_entry_cancelled(
        "ibkr-etf-1", "44964870", 382970843, "UGL") is False


# ------------------------------------------------- the engine -----------

def test_the_engine_only_raises_the_alert_when_it_moved_the_row():
    """The alert, the event and the notification all hang off the same
    decision, so they travel together or not at all."""
    import inspect
    from app.autonomous_copy_engine import AutonomousCopyEngine

    src = inspect.getsource(AutonomousCopyEngine._abandon_unfilled_entry)
    assert "if not trade_registry.record_entry_cancelled(" in src, (
        "the engine still reports unconditionally, so the second path "
        "through raises the alert and notifies again for one event"
    )
    # The cleanup must NOT be behind that decision: it is idempotent, and
    # the path that got here first may not have reached all of it.
    cleanup = src.index("position_store.delete")
    guard = src.index("if not trade_registry.record_entry_cancelled(")
    assert cleanup < guard, (
        "the position and position_store cleanup moved behind the "
        "already-reported check, so a second path through would skip it"
    )
