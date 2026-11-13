"""Bus event -> reported event. One table, matching the spec's catalogue.

The copier's engines emit around thirty event types for their own reasons:
some of them predate this reporter by a long way and are named for the
dashboard's activity feed. This is the single place that knows how each one
becomes the shape `docs/hub-reporter.md` defines, so a field name that
differs between the two is a line in a table here rather than a rename
rippling through the engines.

A bus type that is not in this table is not reported. That is a decision
per type, not an oversight:

- `reconciliation` (the per-row drift events) is covered by
  `reconciliation_summary`, which carries the same rows in `untracked[]`
  and `stuck[]`. Reporting both would show one pass three times.

Anything genuinely unknown is reported as itself with its own fields as
`detail`, rather than dropped -- a new event type added to an engine should
show up in the tab as something unlabelled, not vanish.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Protocol

logger = logging.getLogger(__name__)

# `alert` is the hub's recommendation, not a decision taken here: the copier
# reports the class it believes an event has and the hub's tab decides what
# to do about it. `mismatch` means "alert only when something did not
# match" -- see docs/hub-reporter.md.
INFO, NOTICE, ALERT, MISMATCH = "info", "notice", "alert", "mismatch"

# Which snapshot counter an event feeds. A signal arriving is `received`;
# each leg that actually executed is `copied`; a leg turned away by policy
# is `refused`; a leg that was meant to happen and did not is `failed`.
# Events that are neither -- a disconnect, a deferral still in flight, a
# margin reduction on a copy that did go on -- feed none, because counting
# them would double-count the copy they belong to.
RECEIVED, COPIED, REFUSED, FAILED = "received", "copied", "refused", "failed"


class Facts(Protocol):
    """What the catalogue needs to know about a slave to label its events."""

    def size_unit(self, slave_id: Optional[str], slave_symbol: Optional[str]) -> Optional[str]:
        ...


Source = Any  # a bus field name, or a callable(event, facts)


def iso(ts: Optional[float]) -> Optional[str]:
    """UTC, ISO-8601, Z, to the second. The only time format in a report."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _first(*names: str) -> Callable[[Dict[str, Any], Facts], Any]:
    """The first of these bus fields that is present.

    Several engines say the same thing under two names -- `reason` in one
    place and `message` in another -- because each was written for a
    different reader. The hub gets one name.
    """
    def pick(event: Dict[str, Any], _facts: Facts) -> Any:
        for name in names:
            if event.get(name) is not None:
                return event[name]
        return None
    return pick


def _size_unit(event: Dict[str, Any], facts: Facts) -> Any:
    return facts.size_unit(event.get("slave_id"), event.get("slave_symbol"))


def _at(event: Dict[str, Any], _facts: Facts) -> Any:
    return iso(event.get("at"))


_reason = _first("reason", "message")
_waited = _first("waited_s", "waited_seconds")


@dataclass(frozen=True)
class Reported:
    """One row of the spec's event catalogue."""

    spec_type: Optional[str] = None       # None: the bus name is already right
    cls: str = INFO
    counter: Optional[str] = None
    detail: Dict[str, Source] = field(default_factory=dict)


_NETTING = {
    "slave_symbol": "slave_symbol",
    "net_held": "net_held",
    "direction": "direction",
    "reason": _reason,
}
_DEFERRED_FILLED = {
    "slave_ticket": "slave_ticket",
    "fill_price": "fill_price",
    "waited_s": _waited,
}
_ABANDONED = {"reason": _reason, "waited_s": _waited}

TYPES: Dict[str, Reported] = {
    # ------------------------------------------------------------------ info
    "signal": Reported("signal_received", INFO, RECEIVED, {
        "action": "action", "direction": "direction", "lots": "lots",
        "price": "price", "sl": "sl", "tp": "tp", "atr": "atr",
        "targets": "targets",
    }),
    "slave_open": Reported(None, INFO, COPIED, {
        "slave_symbol": "slave_symbol", "slave_ticket": "slave_ticket",
        "size": "size", "size_unit": _size_unit, "sizing": "sizing",
        "requested_price": "requested_price", "fill_price": "fill_price",
        "sl": "sl", "tp": "tp", "legs": "legs",
        "latency_ms": "latency_ms", "signal_to_fill_ms": "signal_to_fill_ms",
    }),
    "slave_modify": Reported(None, INFO, COPIED, {
        "slave_ticket": "slave_ticket", "sl": "sl", "tp": "tp",
        "legs_moved": "legs_moved",
    }),
    "slave_close": Reported(None, INFO, COPIED, {
        "slave_ticket": "slave_ticket", "fill_price": "fill_price",
        "size": "size", "latency_ms": "latency_ms",
    }),
    "deferred_open_filled": Reported(None, INFO, COPIED, _DEFERRED_FILLED),
    "deferred_close_filled": Reported(None, INFO, COPIED, _DEFERRED_FILLED),

    # ---------------------------------------------------------------- notice
    "margin_reduced": Reported(None, NOTICE, None, {
        "slave_symbol": "slave_symbol", "asked": "asked", "sent": "sent",
        "margin_required": "margin_required", "margin_free": "margin_free",
        "ceiling_pct": "ceiling_pct",
        "risk_fraction_carried": "risk_fraction_carried",
    }),
    "netting_blocked": Reported(None, NOTICE, REFUSED, _NETTING),
    # The autonomous engine's own name for the same refusal.
    "netting_refused": Reported("netting_blocked", NOTICE, REFUSED, _NETTING),
    "margin_blocked": Reported(None, NOTICE, REFUSED, {
        "slave_symbol": "slave_symbol", "reason": _reason,
    }),
    "route_blocked": Reported(None, NOTICE, REFUSED, {
        "target": _first("target", "slave_symbol"), "reason": _reason,
    }),
    "route_unresolved": Reported(None, NOTICE, REFUSED, {
        "target": _first("target", "slave_symbol"), "reason": _reason,
    }),
    "signal_unrouted": Reported(None, NOTICE, REFUSED, {"reason": _reason}),
    "trading_hours_blocked": Reported(None, NOTICE, REFUSED, {}),
    "duplicate_open_ignored": Reported(None, NOTICE, REFUSED, {
        "existing_slave_ticket": "slave_ticket",
    }),
    "signal_refused": Reported(None, NOTICE, REFUSED, {
        # `action` is beyond the spec's row for this type and deliberately
        # kept: a refused CLOSE leaves the slave holding a position the
        # master has exited, which is nothing like a refused OPEN.
        "reason": _reason, "action": "action",
    }),
    "open_deferred": Reported(None, NOTICE, None, {
        "slave_symbol": "slave_symbol", "reason": _reason,
        "retry_every_s": "retry_every_s",
    }),
    "close_deferred": Reported(None, NOTICE, None, {
        "slave_symbol": "slave_symbol", "reason": _reason,
        "retry_every_s": "retry_every_s",
    }),
    "deferred_open_cancelled": Reported(None, NOTICE, REFUSED, _ABANDONED),
    "master_disconnected": Reported(None, NOTICE, None, {
        "account": "account", "silent_for_s": "silent_for_s",
    }),
    "master_reconnected": Reported(None, NOTICE, None, {
        "account": "account", "silent_for_s": "silent_for_s",
    }),

    # ----------------------------------------------------------------- alert
    "kill_switch_blocked": Reported(None, ALERT, REFUSED, {}),
    "deferred_open_abandoned": Reported(None, ALERT, FAILED, _ABANDONED),
    "deferred_close_abandoned": Reported(None, ALERT, FAILED, _ABANDONED),
    "entry_unfilled": Reported(None, ALERT, FAILED, {
        "slave_symbol": "slave_symbol", "limit_price": "limit_price",
        "waited_s": _waited,
    }),
    "slave_error": Reported(None, ALERT, FAILED, {
        "action": "action", "message": "message",
    }),
    "slave_start_failed": Reported(None, ALERT, FAILED, {"message": "message"}),
    "slave_recovery_failed": Reported(None, ALERT, FAILED, {
        "message": "message", "attempts": "attempts",
    }),

    # A session going down and coming back is what IB Gateway's forced daily
    # restart looks like, so these are info and never alert however alarming
    # the words are. `slave_recovery_failed` above is the one that matters.
    "slave_disconnected": Reported(None, INFO, None, {"message": "message"}),
    "slave_reconnected": Reported(None, INFO, None, {"message": "message"}),
    "slave_session_recovered": Reported(None, INFO, None, {"message": "message"}),

    "reconciliation_summary": Reported("reconciliation", MISMATCH, None, {
        "checked": "checked", "matched": "matched",
        "untracked": "untracked", "stuck": "stuck", "at": _at,
    }),

    # The same idea against a different authority: `reconciliation` compares
    # the registry to what the broker says it HOLDS, this compares it to what
    # the broker says it EXECUTED. Independent of the Gateway API that every
    # other number here came through, which is the whole value of it.
    "flex_reconciliation": Reported(None, MISMATCH, None, {
        "account": "account", "period": "period",
        "checked": "checked", "orders": "orders", "matched": "matched",
        "protective": "protective", "unjoinable": "unjoinable",
        "stored": "stored", "already_had": "already_had",
        "mismatched": "mismatched", "untracked": "untracked",
        # False, always: a clean pass does NOT mean the broker executed
        # everything the registry expected. See app/flex_reconcile.py for
        # why absence cannot be computed honestly from a Flex statement.
        "absence_checked": "absence_checked",
    }),

    # ---------------------------------- beyond the spec's catalogue (see docs)
    # Both are in the copier's vocabulary and neither is in the spec's table.
    # Reported rather than dropped: a roll replaces a live position with a
    # different contract, and a drawdown alert is the account telling you
    # something -- a tab that showed neither would be missing the two events
    # most likely to explain a surprise.
    "contract_roll": Reported(None, INFO, None, {
        "slave_symbol": "slave_symbol", "slave_ticket": "slave_ticket",
        "from_contract": "from_contract", "to_contract": "to_contract",
        "sl": "sl", "tp": "tp",
    }),
    "drawdown_alert": Reported(None, ALERT, None, {
        "account_type": "account_type", "drawdown_pct": "drawdown_pct",
        "peak_equity": "peak_equity", "equity": "equity",
    }),
}

# Bus types this reporter deliberately does not send.
IGNORED = frozenset({"reconciliation"})

# Fields that are never copied into `detail`: they are either the event's
# identity, which lives at the top level, or bookkeeping of this hub's own.
_IDENTITY = frozenset({
    "type", "slave_id", "master_account", "master_ticket", "magic",
    "master_symbol", "emitted_at", "ticket", "account", "symbol", "ts",
})


def _resolve(source: Source, event: Dict[str, Any], facts: Facts) -> Any:
    if callable(source):
        return source(event, facts)
    return event.get(source)


def translate(event: Dict[str, Any], facts: Facts) -> Optional[Dict[str, Any]]:
    """One bus event as the hub will receive it, or None if it is not reported.

    No `event_id`: that is assigned by the outbox when the event is queued,
    once, and stored with it.
    """
    bus_type = event.get("type")
    if not bus_type or bus_type in IGNORED:
        return None

    spec = TYPES.get(bus_type)
    if spec is None:
        # An event type this table has not been told about. Passed through
        # with whatever it carries, so adding one to an engine shows up in
        # the tab as unlabelled rather than as nothing at all.
        logger.debug("hub reporter: passing through unmapped event %r", bus_type)
        detail = {k: v for k, v in event.items() if k not in _IDENTITY}
        spec_type = str(bus_type)
    else:
        detail = {f: _resolve(src, event, facts) for f, src in spec.detail.items()}
        spec_type = spec.spec_type or str(bus_type)

    return {
        "type": spec_type,
        # When it happened, not when it is sent. The bus stamps every event
        # with the moment it was emitted, which for everything here is the
        # moment the thing itself occurred.
        "at": iso(event.get("emitted_at")),
        "master_account": _first("master_account", "account")(event, facts),
        "master_ticket": _first("master_ticket", "ticket")(event, facts),
        "magic": event.get("magic"),
        "master_symbol": _first("master_symbol", "symbol")(event, facts),
        # None for an event that is about the copier rather than one slave --
        # a master going quiet, a signal that reached nobody.
        "slave_id": event.get("slave_id"),
        "detail": detail,
    }


def counter_for(bus_type: Optional[str]) -> Optional[str]:
    """Which snapshot counter this event feeds, if any."""
    spec = TYPES.get(bus_type or "")
    return spec.counter if spec else None


def class_for(bus_type: Optional[str]) -> str:
    """info / notice / alert / mismatch, as the spec's catalogue classes them.

    Kept out of the reported event on purpose: the class is this copier's
    recommendation about what deserves attention, and the hub's tab is where
    that decision belongs. It is used here only to decide which event's text
    becomes a slave's `last_error` in the snapshot.
    """
    spec = TYPES.get(bus_type or "")
    return spec.cls if spec else INFO
