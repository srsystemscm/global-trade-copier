"""Regression tests for SignalBus deduplication.

The real master EA stamps `ts` with MQL4's TimeGMT() -- whole seconds -- while
polling every 500ms, so several distinct actions on one ticket routinely share
a timestamp. Keying dedup on ticket+ts alone silently dropped all but the
first of them.

scripts/simulate_ea.py structurally cannot reproduce this: it stamps
time.time() (microsecond floats) and sleeps between signals, so every signal
gets a unique ts. This only shows up against a real terminal.

These exercise the dedup logic directly rather than publish_raw(), which also
persists to SQLite and reads the kill switch -- neither of which this is about.
"""
import time

from app.models import Signal
from app.signal_bus import SignalBus

SAME_SECOND = 1756300993.0


def _signal(action, ts=SAME_SECOND, ticket=123456, **over):
    return Signal(
        ticket=ticket, symbol="XAUUSD", action=action, direction="BUY",
        lots=0.10, price=2385.5, sl=2375.0, tp=2405.0, atr=10.0,
        account="TEST-MASTER-1", ts=ts, **over,
    )


def _accept(bus, signal):
    """Mirrors publish_raw()'s duplicate check + record, minus the I/O."""
    if bus._is_duplicate(signal):
        return False
    # receipt time, not signal.ts -- _evict_expired() compares against
    # wall-clock now, so stamping it with the signal's own (possibly old)
    # timestamp would evict the entry immediately.
    bus._seen[bus._dedup_key(signal)] = time.time()
    return True


def test_open_then_modify_in_same_second_both_delivered():
    """The actual bug: one whole-second ts, two different actions."""
    bus = SignalBus()
    assert _accept(bus, _signal("OPEN")) is True
    assert _accept(bus, _signal("MODIFY")) is True


def test_full_sequence_in_same_second_all_delivered():
    bus = SignalBus()
    accepted = [a for a in ("OPEN", "MODIFY", "CLOSE") if _accept(bus, _signal(a))]
    assert accepted == ["OPEN", "MODIFY", "CLOSE"]


def test_identical_message_replayed_is_still_deduplicated():
    """Widening the key must not weaken the replay protection it exists for."""
    bus = SignalBus()
    assert _accept(bus, _signal("OPEN")) is True
    assert _accept(bus, _signal("OPEN")) is False


def test_same_action_same_second_different_tickets_both_delivered():
    bus = SignalBus()
    assert _accept(bus, _signal("OPEN", ticket=111)) is True
    assert _accept(bus, _signal("OPEN", ticket=222)) is True


def test_dedup_key_includes_action():
    bus = SignalBus()
    assert bus._dedup_key(_signal("OPEN")) != bus._dedup_key(_signal("MODIFY"))
