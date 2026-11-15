"""When an entry order should stop being an entry order.

A resting entry has TWO deadlines and they are not the same thing:

  * the BROKER's -- the time in force. GTC never expires; DAY expires at
    that session's close; GTD expires when we say.
  * the ENGINE's -- `entry_timeout`, after which the hub cancels the order
    and forgets the position it was going to open.

Both were spans. `tif: GTC` with `entry_timeout: 93600` is the live ETF
config, and 93600 is twenty-six hours chosen to be "long enough" -- which
it is, for an order placed at Sunday 18:00 ET reaching Monday's 09:30
open. It is not long enough for one placed Friday evening, and it is far
too long for one placed Monday at 09:00. The number cannot be right for
both because the thing it is reaching for is not a span at all: it is the
next time the instrument actually trades.

So a deadline may now be written as the boundary it means:

    "rth_open"        the next 09:30 ET on a weekday
    "rth_close"       the next 16:00 ET
    "premarket_open"  the next 04:00 ET
    "afterhours_close" the next 20:00 ET
    "09:45"           the next time it is 09:45 ET on a weekday
    "rth_open+30m"    any of the above, shifted
    "26h" / "90m" / "3d" / "600s"   a plain span, as before

Resolution happens WHEN THE ORDER IS PLACED, not when the config is
read, because "the next open" depends on when you ask.

WHAT IS NOT MODELLED: market holidays. A GTD landing on Thanksgiving
expires that afternoon without the instrument having traded, and the
entry is simply missed. That is the safe direction to be wrong in -- an
expired entry is no position and no risk, where an order outliving its
intent is a position nobody is watching -- but it is a real limitation,
and it is why the weekend skip below is a skip rather than a calendar.

Eastern time, not local time and not the broker's: US equity sessions are
defined in ET, and the hub may run anywhere. That needs a timezone
database (see tzdata in requirements.txt), and a missing one raises
rather than silently falling back to an offset that is wrong half the
year.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Optional

try:  # pragma: no cover - exercised by whichever branch the host has
    from zoneinfo import ZoneInfo

    EASTERN = ZoneInfo("America/New_York")
except Exception as exc:  # pragma: no cover
    EASTERN = None
    _TZ_ERROR = exc
else:
    _TZ_ERROR = None


class OrderTimingError(ValueError):
    """A deadline spec that cannot be turned into a time.

    A ValueError subclass so a bad config value reads as a bad value
    wherever it is caught, rather than needing this module imported to be
    recognised.
    """


# The ET wall-clock boundaries of a US equity trading day. 09:30-16:00 is
# the regular session; 04:00 and 20:00 are the outer edges of the extended
# session IBKR will fill a limit order in with outsideRth set, which is the
# only way an ETF leg opens from an 18:00 ET signal at all.
BOUNDARIES = {
    "rth_open": (9, 30),
    "rth_close": (16, 0),
    "premarket_open": (4, 0),
    "afterhours_close": (20, 0),
}

_SPAN = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$", re.I)
_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_OFFSET = re.compile(r"^(.*?)\s*([+-])\s*(\d+(?:\.\d+)?)\s*([smhd])$", re.I)

_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

# How many days forward to look for a boundary that is still in the
# future. Four would do for a weekend; ten leaves room for an offset big
# enough to walk a candidate backwards past one.
_MAX_LOOKAHEAD_DAYS = 10


def _eastern(now_utc: dt.datetime) -> dt.datetime:
    if EASTERN is None:  # pragma: no cover - depends on the host's tzdata
        raise OrderTimingError(
            "no IANA timezone database is available, so a session boundary "
            "cannot be resolved to a time (install tzdata). Guessing a fixed "
            f"UTC offset for Eastern time is wrong for half the year: {_TZ_ERROR}"
        )
    return now_utc.astimezone(EASTERN)


def _as_utc(value: dt.datetime) -> dt.datetime:
    """A naive datetime here would be read as local time by astimezone, so
    it is refused rather than guessed at."""
    if value.tzinfo is None:
        raise OrderTimingError(
            "the current time must be timezone-aware; a naive datetime would "
            "be read as the host's local time, which is not what a session "
            "boundary is measured against"
        )
    return value.astimezone(dt.timezone.utc)


def seconds_until(spec, now_utc: Optional[dt.datetime] = None) -> Optional[float]:
    """How long from now until `spec`, in seconds.

    This is what the ENGINE's abandon timer wants, and it is why
    `entry_timeout` can now be written as a boundary: the engine keeps
    counting seconds and the config stops having to guess how many.

    A number passes straight through, so every existing `entry_timeout:
    93600` means exactly what it meant before.
    """
    if spec is None:
        return None
    if isinstance(spec, bool):
        raise OrderTimingError(f"{spec!r} is not a deadline")
    if isinstance(spec, (int, float)):
        return float(spec)

    now_utc = _as_utc(now_utc or dt.datetime.now(dt.timezone.utc))
    when = resolve(spec, now_utc)
    # Never negative: resolve() only returns instants after `now`, and a
    # caller passing this to a sleep must not be handed a negative.
    return max(0.0, (when - now_utc).total_seconds())


def resolve(spec, now_utc: Optional[dt.datetime] = None) -> dt.datetime:
    """The instant `spec` names, as an aware UTC datetime.

    A bare number is a span in SECONDS, matching `entry_timeout`, which is
    the only numeric deadline this system has ever had.
    """
    if spec is None:
        raise OrderTimingError("no deadline given")
    if isinstance(spec, bool):
        raise OrderTimingError(f"{spec!r} is not a deadline")

    now_utc = _as_utc(now_utc or dt.datetime.now(dt.timezone.utc))

    if isinstance(spec, dt.datetime):
        return _as_utc(spec)
    if isinstance(spec, (int, float)):
        if spec <= 0:
            raise OrderTimingError(
                f"{spec} is not a deadline in the future; an entry whose "
                f"deadline has already passed would be cancelled before it "
                f"could fill"
            )
        return now_utc + dt.timedelta(seconds=float(spec))

    text = str(spec).strip()
    if not text:
        raise OrderTimingError("no deadline given")

    offset = dt.timedelta(0)
    found = _OFFSET.match(text)
    if found:
        base, sign, amount, unit = found.groups()
        # "26h" also matches the offset pattern as ("", "+"?, ...) -- it
        # does not, because _OFFSET requires a sign character, and a span
        # has none. But "rth_open-" with nothing after it would leave an
        # empty base, so that is caught here rather than later.
        if not base:
            raise OrderTimingError(
                f"{text!r} has an offset but nothing to offset from"
            )
        seconds = float(amount) * _UNIT_SECONDS[unit.lower()]
        offset = dt.timedelta(seconds=seconds if sign == "+" else -seconds)
        text = base.strip()

    span = _SPAN.match(text)
    if span:
        if offset:
            raise OrderTimingError(
                f"{spec!r} offsets a span, which is just a different span -- "
                f"write the total instead"
            )
        amount, unit = span.groups()
        total = float(amount) * _UNIT_SECONDS[unit.lower()]
        if total <= 0:
            raise OrderTimingError(f"{spec!r} is a zero-length deadline")
        return now_utc + dt.timedelta(seconds=total)

    key = text.lower().replace("-", "_")
    if key in BOUNDARIES:
        hour, minute = BOUNDARIES[key]
    else:
        clock = _CLOCK.match(text)
        if not clock:
            raise OrderTimingError(
                f"{spec!r} is not a deadline this hub understands. Expected a "
                f"span like 26h/90m/3d, a session boundary "
                f"({', '.join(sorted(BOUNDARIES))}), or an Eastern wall clock "
                f"time like 09:45 -- any of them optionally shifted, as in "
                f"rth_open+30m"
            )
        hour, minute = int(clock.group(1)), int(clock.group(2))

    return _next_eastern_wall_time(now_utc, hour, minute, offset)


def _next_eastern_wall_time(
    now_utc: dt.datetime, hour: int, minute: int, offset: dt.timedelta
) -> dt.datetime:
    """The next weekday on which it is `hour:minute` in New York, shifted.

    Built day by day in ET rather than by adding 24h in UTC, because a
    deadline crossing a DST change otherwise lands an hour off -- which for
    "rth_open" means an order expiring an hour before the open it was
    placed to reach.

    The WEEKDAY is taken from the session's own date, before the offset is
    applied, so "rth_open-15m" on a Monday is Monday's pre-open and not the
    preceding Friday evening.
    """
    now_et = _eastern(now_utc)
    for day in range(_MAX_LOOKAHEAD_DAYS):
        date = (now_et + dt.timedelta(days=day)).date()
        if date.weekday() >= 5:  # Saturday, Sunday: no US equity session
            continue
        # Constructed in ET, then converted -- the reverse would pick the
        # wrong offset on the two days a year it differs.
        candidate = dt.datetime(
            date.year, date.month, date.day, hour, minute, tzinfo=EASTERN
        ).astimezone(dt.timezone.utc) + offset
        if candidate > now_utc:
            return candidate
    raise OrderTimingError(  # pragma: no cover - would need a 10-day gap
        f"no {hour:02d}:{minute:02d} Eastern weekday found within "
        f"{_MAX_LOOKAHEAD_DAYS} days"
    )


def format_for_ibkr(when: dt.datetime) -> str:
    """IBKR's `goodTillDate` wire format.

    Byte-identical to what `ib_async.util.formatIBDatetime` produces for a
    datetime -- "%Y%m%d %H:%M:%S UTC" after converting -- but computed here
    so the format is testable on a host with no ib_async installed, and so
    a GTD order does not depend on which of the two client libraries this
    adapter happens to be running against.

    UTC on the wire, deliberately. The spec is written in Eastern because
    that is what a session boundary means; it is sent in UTC because an
    unqualified or locally-qualified timestamp is the kind of thing that
    works all year and then does not.
    """
    return _as_utc(when).strftime("%Y%m%d %H:%M:%S UTC")


def resolve_good_till(spec, now_utc: Optional[dt.datetime] = None) -> Optional[str]:
    """`spec` as a `goodTillDate` string, or None if there is no spec."""
    if spec is None or (isinstance(spec, str) and not spec.strip()):
        return None
    return format_for_ibkr(resolve(spec, now_utc))
