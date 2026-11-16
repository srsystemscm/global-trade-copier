"""GTD meant nothing, and a deadline is a session rather than a span.

TWO defects, and the first is the dangerous one.

1. `GTD` was in IBKRAdapter.ENTRY_TIFS from the day the per-route TIF
   landed, and `goodTillDate` was never set on the order. A route could
   therefore ask for "expire at the open" and get an order with no
   deadline: rejected outright by some TWS versions, and treated as GTC
   by others. The second is worse than the rejection -- the entry rests
   indefinitely and can fill days later, off a signal nobody is acting on
   any more, into a position whose stop was sized for a price that has
   moved.

2. Both deadlines were spans. The live ETF config is `tif: GTC,
   entry_timeout: 93600` -- twenty-six hours, chosen to be long enough
   for an order placed Sunday 18:00 ET to reach Monday's 09:30 open. It
   is not long enough for one placed Friday evening, and it is far too
   long for one placed Monday morning. No single number is right, because
   the thing it is reaching for is not a span: it is the next time the
   instrument actually trades.

Chris's words: "a 'friday' or a 'weekly' signal don't land for the EA to
act on up till the next bar - Sunday open. so the ETF GTC would go out
then, but might cancel unless its timed to the ETF's GTO EXO open."

So a deadline may now be written as the boundary it means. The resolver
is pure and lives in app/order_timing.py; the hour it answers with is
Eastern, because that is what a US equity session is defined in.
"""
import datetime as dt

import pytest

from app.adapters.base import AdapterError, OrderRequest
from app.models import Signal
from app.order_timing import (
    OrderTimingError,
    format_for_ibkr,
    resolve,
    resolve_good_till,
    seconds_until,
)
from app.routing import resolve_routes
from app.slaves import SlaveConfig

ET = "America/New_York"

# Sunday 2026-11-15, 18:00 ET -- the hour the ETF legs exist for. The
# master is a CFD account whose week opens now; the ETF it routes to does
# not trade for another fifteen and a half hours.
SUNDAY_EVENING = dt.datetime(2026, 11, 15, 23, 0, tzinfo=dt.timezone.utc)
# Friday 2026-11-13, 18:00 ET. A deadline naming "the next open" from here
# has to clear two non-trading days.
FRIDAY_EVENING = dt.datetime(2026, 11, 13, 23, 0, tzinfo=dt.timezone.utc)


def _sig(symbol="XAUUSD", magic=1001):
    return Signal(ticket=1, symbol=symbol, action="OPEN", direction="BUY",
                  lots=1.0, price=100.0, sl=98.0, magic=magic,
                  account="44964870")


def _et(when):
    from zoneinfo import ZoneInfo
    return when.astimezone(ZoneInfo(ET))


# ------------------------------------------------ the boundaries --------

def test_the_next_open_from_sunday_evening_is_mondays():
    when = _et(resolve("rth_open", SUNDAY_EVENING))
    assert (when.year, when.month, when.day) == (2026, 11, 16)
    assert (when.hour, when.minute) == (9, 30)


def test_the_next_open_from_friday_evening_clears_the_weekend():
    """The case 93600 cannot express. Saturday 09:30 is not an open, and
    an order expiring then was never going to fill."""
    when = _et(resolve("rth_open", FRIDAY_EVENING))
    assert when.weekday() == 0, f"{when} is not a Monday"
    assert (when.hour, when.minute) == (9, 30)


def test_the_close_is_the_one_an_etf_entry_actually_wants():
    """Expiring AT the open cancels the order exactly when liquidity
    arrives, so a resting ETF entry wants the session it is waiting for,
    not the instant it begins."""
    when = _et(resolve("rth_close", SUNDAY_EVENING))
    assert (when.hour, when.minute) == (16, 0)
    assert when.day == 16
    assert resolve("rth_close", SUNDAY_EVENING) > resolve("rth_open", SUNDAY_EVENING)


def test_the_extended_session_edges_are_nameable():
    """outside_rth is already per-route, and 04:00-20:00 ET is the window
    it opens. A deadline inside that window is the point of setting it."""
    assert (_et(resolve("premarket_open", SUNDAY_EVENING)).hour,
            _et(resolve("premarket_open", SUNDAY_EVENING)).minute) == (4, 0)
    assert _et(resolve("afterhours_close", SUNDAY_EVENING)).hour == 20


def test_a_boundary_can_be_shifted():
    """"Thirty minutes into the session" is a real instruction and not
    expressible as a boundary on its own."""
    assert _et(resolve("rth_open+30m", SUNDAY_EVENING)).hour == 10
    early = _et(resolve("rth_open-15m", SUNDAY_EVENING))
    assert (early.hour, early.minute) == (9, 15)


def test_a_shift_stays_on_its_own_session():
    """"Monday's open minus fifteen minutes" is Monday, not the preceding
    Friday evening. The weekday is taken from the session's date, before
    the shift is applied -- otherwise a Monday pre-open deadline lands on
    a day the instrument does not trade."""
    when = _et(resolve("rth_open-15m", FRIDAY_EVENING))
    assert when.weekday() == 0, f"{when} slipped off Monday"


def test_an_eastern_wall_clock_time_is_accepted():
    when = _et(resolve("09:45", SUNDAY_EVENING))
    assert (when.hour, when.minute) == (9, 45)


def test_a_span_still_means_exactly_what_it_meant():
    """Every live route writes a number. None of them may change."""
    assert resolve(93600, SUNDAY_EVENING) == SUNDAY_EVENING + dt.timedelta(seconds=93600)
    assert resolve("26h", SUNDAY_EVENING) == SUNDAY_EVENING + dt.timedelta(hours=26)
    assert resolve("90m", SUNDAY_EVENING) == SUNDAY_EVENING + dt.timedelta(minutes=90)
    assert resolve("3d", SUNDAY_EVENING) == SUNDAY_EVENING + dt.timedelta(days=3)


def test_the_boundary_beats_the_guess_it_replaces():
    """The arithmetic, stated: 93600 from Sunday 18:00 ET runs to Monday
    20:00 ET -- past the close, through after-hours. "rth_open" is 15.5
    hours. Neither is wrong; the point is that the first number is only
    right from one particular Sunday evening, and the config cannot know
    it is Sunday evening."""
    assert seconds_until("rth_open", SUNDAY_EVENING) == pytest.approx(55800)
    assert seconds_until("rth_open", FRIDAY_EVENING) == pytest.approx(55800 + 2 * 86400)
    # The same spec, two placements, two different right answers. That is
    # the whole argument for the spec.
    assert seconds_until("rth_open", FRIDAY_EVENING) != seconds_until(
        "rth_open", SUNDAY_EVENING
    )


def test_a_number_passes_through_seconds_until_untouched():
    assert seconds_until(93600, SUNDAY_EVENING) == 93600.0
    assert seconds_until(None) is None


# ------------------------------------------------ daylight saving -------

def test_a_deadline_across_the_spring_change_is_still_the_open():
    """Built day by day in Eastern rather than by adding 24h in UTC. The
    naive version lands an hour off across a DST change, which for
    "rth_open" means an order expiring an hour BEFORE the open it was
    placed to reach."""
    friday = dt.datetime(2027, 3, 12, 23, 0, tzinfo=dt.timezone.utc)  # before spring-forward
    when = _et(resolve("rth_open", friday))
    assert (when.hour, when.minute) == (9, 30), f"{when} drifted across DST"
    assert when.utcoffset() == dt.timedelta(hours=-4), "should be EDT by Monday"
    # 09:30 EDT is 13:30 UTC, not 14:30. An hour wrong here is the bug.
    assert resolve("rth_open", friday).hour == 13


def test_a_deadline_across_the_autumn_change_is_still_the_open():
    friday = dt.datetime(2026, 10, 30, 23, 0, tzinfo=dt.timezone.utc)  # before fall-back
    when = _et(resolve("rth_open", friday))
    assert (when.hour, when.minute) == (9, 30)
    assert when.utcoffset() == dt.timedelta(hours=-5), "should be EST by Monday"


# ------------------------------------------------ the wire format -------

def test_the_wire_format_is_the_one_the_ib_client_itself_writes():
    """Matched to ib_async.util.formatIBDatetime, which converts to UTC and
    writes "%Y%m%d %H:%M:%S UTC". Reimplemented here rather than imported
    so the format is testable on a host with no ib_async, and so a GTD
    order does not depend on which of the two client libraries this
    adapter happens to be running against."""
    assert format_for_ibkr(SUNDAY_EVENING) == "20261115 23:00:00 UTC"
    assert resolve_good_till("rth_open", SUNDAY_EVENING) == "20261116 14:30:00 UTC"


def test_the_wire_format_agrees_with_the_installed_client():
    """Pinned against the real thing where it is installed, because the
    one claim this module makes that unit tests cannot otherwise check is
    that TWS will accept the string."""
    util = pytest.importorskip("ib_async.util")
    for when in (SUNDAY_EVENING, resolve("rth_open", SUNDAY_EVENING)):
        assert format_for_ibkr(when) == util.formatIBDatetime(when)


def test_no_spec_is_no_deadline_rather_than_an_invented_one():
    assert resolve_good_till(None) is None
    assert resolve_good_till("") is None
    assert resolve_good_till("   ") is None


# ------------------------------------------------ bad config ------------

@pytest.mark.parametrize("spec", [
    "rth_opne",          # a typo in a boundary name
    "GTC",               # a TIF where a deadline belongs
    "25:00",             # not a time
    "+30m",              # a shift with nothing to shift
    "0h",                # a zero-length deadline
    -5,                  # already past
    True,                # bool is an int, and must not be read as 1 second
])
def test_an_unusable_deadline_raises_rather_than_guessing(spec):
    with pytest.raises(OrderTimingError):
        resolve(spec, SUNDAY_EVENING)


def test_a_bad_spec_is_refused_when_the_config_is_saved():
    """A typo sitting in the config looks correct until the one evening a
    signal arrives, which is the worst moment to find it: nobody is
    watching, and the alternative to this route is no position.

    So it is caught where somebody is there to read the message -- saving
    the slave -- alongside the magic-filter and master-filter checks that
    already guard this endpoint."""
    from fastapi import HTTPException

    from app.api_routes import _validated_route_deadlines

    with pytest.raises(HTTPException) as exc:
        _validated_route_deadlines(
            {"routes": [{"symbol": "XAUUSD", "target": "UGL",
                         "tif": "GTD", "good_till": "rth_opne"}]}
        )
    assert exc.value.status_code == 400
    assert "UGL" in exc.value.detail, "the message should name the route"


def test_saving_gtd_with_no_deadline_at_all_is_refused():
    """The specific misconfiguration that used to reach IBKR as an order
    with no expiry."""
    from fastapi import HTTPException

    from app.api_routes import _validated_route_deadlines

    with pytest.raises(HTTPException) as exc:
        _validated_route_deadlines(
            {"routes": [{"symbol": "XAUUSD", "target": "UGL", "tif": "GTD"}]}
        )
    assert "good_till" in exc.value.detail
    # A slave-wide default satisfies it -- twenty ETF routes should not
    # each have to repeat the same boundary.
    _validated_route_deadlines(
        {"good_till": "rth_close",
         "routes": [{"symbol": "XAUUSD", "target": "UGL", "tif": "GTD"}]}
    )


def test_the_existing_live_config_still_saves():
    """Twenty GTC routes at 93600 seconds. If this check rejected them,
    the first save of any slave setting would fail."""
    from app.api_routes import _validated_route_deadlines

    _validated_route_deadlines({
        "routes": [{"symbol": "XAUUSD", "target": "UGL", "order_type": "limit",
                    "outside_rth": True, "tif": "GTC", "entry_timeout": 93600}],
    })


def test_routing_does_not_raise_over_one_mistyped_route():
    """MY OWN FIRST CUT PUT THE CHECK HERE, in routing, with a docstring
    claiming it ran at config-load time. It does not: resolve_routes() is
    called PER SIGNAL. So one mistyped word in one route would have raised
    out of routing and stopped every signal to that slave from routing at
    all -- a far wider failure than the one it was meant to prevent.

    A bad spec now costs exactly the one order it belongs to: the adapter
    refuses it, by name, and the other routes are untouched."""
    routes = resolve_routes(
        {"routes": [{"symbol": "XAUUSD", "target": "UGL",
                     "tif": "GTD", "good_till": "rth_opne"}]},
        _sig(),
    )
    assert routes, "routing refused the whole table over one bad deadline"
    assert routes[0].good_till == "rth_opne", "the bad value is carried, not swallowed"


def test_a_naive_now_is_refused_rather_than_read_as_local_time():
    """The hub may run anywhere. A naive datetime would be read as the
    host's local time, and a session boundary measured against the wrong
    zone is a deadline hours out."""
    with pytest.raises(OrderTimingError):
        resolve("rth_open", dt.datetime(2026, 11, 15, 23, 0))


# ------------------------------------------------ the route -------------

def test_a_route_carries_its_own_deadline():
    routes = resolve_routes(
        {"routes": [{"symbol": "XAUUSD", "target": "UGL", "tif": "GTD",
                     "good_till": "rth_close", "entry_timeout": "rth_close"}]},
        _sig(),
    )
    assert routes[0].tif == "GTD"
    assert routes[0].good_till == "rth_close"
    assert routes[0].entry_timeout == "rth_close"


def test_an_existing_numeric_timeout_still_parses_to_a_float():
    """entry_timeout was float()-ed on load, which is why it could not
    carry a boundary -- float("rth_open") is a ValueError that takes the
    whole routing table down. Numbers must keep arriving as numbers."""
    routes = resolve_routes(
        {"routes": [{"symbol": "XAUUSD", "target": "UGL", "tif": "GTC",
                     "entry_timeout": 93600}]},
        _sig(),
    )
    assert routes[0].entry_timeout == 93600.0
    assert isinstance(routes[0].entry_timeout, float)
    assert routes[0].good_till is None


def test_a_route_that_says_nothing_still_says_nothing():
    routes = resolve_routes({"routes": [{"symbol": "XAUUSD", "target": "MGC"}]},
                            _sig())
    assert routes[0].good_till is None
    assert routes[0].entry_timeout is None


def test_the_engine_hands_the_deadline_to_the_adapter():
    """Without this the route can be set, the test above passes, and the
    order still goes out with no deadline."""
    import inspect

    from app.autonomous_copy_engine import AutonomousCopyEngine
    src = inspect.getsource(AutonomousCopyEngine)
    assert "good_till=route.good_till" in src, (
        "the route's deadline never reaches OrderRequest, so GTD is still "
        "an order with no expiry"
    )


# ------------------------------------------------ the adapter -----------

class _Entry:
    """Stands in for ib_async's Order, which is a plain dataclass here."""

    def __init__(self):
        self.tif = None
        self.goodTillDate = ""


def _adapter(**cfg):
    from app.adapters import create_adapter
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002, "client_id": 1}
    base.update(cfg)
    return create_adapter(SlaveConfig(id="i", name="i", broker_type="ibkr",
                                      enabled=True, paused=False, config=base))


def _order(**kw):
    kw.setdefault("symbol", "UGL")
    kw.setdefault("direction", "BUY")
    kw.setdefault("size", 10)
    return OrderRequest(**kw)


def test_a_gtd_entry_is_stamped_with_a_deadline():
    entry = _adapter()._stamp_entry_timing(
        _Entry(), _order(tif="GTD", good_till="rth_close")
    )
    assert entry.tif == "GTD"
    assert entry.goodTillDate, "GTD with no goodTillDate is GTC at the broker"
    assert entry.goodTillDate.endswith(" UTC")


def test_a_gtd_entry_with_no_deadline_anywhere_is_refused():
    """THE BUG. It used to be placed, and IBKR treats a dateless GTD as
    GTC -- so the route asking to expire at the open got an order that
    never expires. Refused at the adapter, where the message can say what
    to set, rather than silently becoming the one thing it must not be."""
    with pytest.raises(AdapterError) as exc:
        _adapter()._stamp_entry_timing(_Entry(), _order(tif="GTD"))
    assert "good_till" in str(exc.value)
    assert "GTC" in str(exc.value), "the message should name the alternative"


def test_a_gtd_entry_with_an_unusable_deadline_is_refused():
    with pytest.raises(AdapterError):
        _adapter()._stamp_entry_timing(
            _Entry(), _order(tif="GTD", good_till="rth_opne")
        )


def test_the_slave_can_supply_the_deadline_for_every_gtd_route():
    """Twenty ETF routes should not each have to repeat it."""
    entry = _adapter(good_till="rth_close")._stamp_entry_timing(
        _Entry(), _order(tif="GTD")
    )
    assert entry.goodTillDate


def test_the_route_beats_the_slave():
    """WAS TIME-DEPENDENT, and failed at 11:50 ET on 2026-11-16.

    It asserted rth_open lands BEFORE afterhours_close. That holds in the
    evening, when both are the next day -- and inverts during the session,
    when afterhours_close is tonight and rth_open is tomorrow morning. The
    test was written at 23:00 and passed for twelve hours.

    What it means to check is that the ROUTE's value is used rather than
    the slave's, so it now pins that directly: two boundaries whose order
    cannot swap, because they are the same boundary a day apart."""
    a = _adapter(good_till="afterhours_close")
    route_said = a._stamp_entry_timing(_Entry(), _order(tif="GTD", good_till="rth_open"))
    slave_said = a._stamp_entry_timing(_Entry(), _order(tif="GTD"))
    assert route_said.goodTillDate != slave_said.goodTillDate, (
        "the slave's deadline was used for a route that named its own"
    )
    # And the route's value really is rth_open, whatever the hour: 09:30
    # Eastern is 13:30 or 14:30 UTC depending on daylight saving.
    assert route_said.goodTillDate[9:14] in ("13:30", "14:30"), route_said.goodTillDate
    assert slave_said.goodTillDate[9:14] in ("00:00", "01:00"), slave_said.goodTillDate


def test_a_non_gtd_entry_is_left_without_a_deadline():
    """goodTillDate on a GTC or DAY order is meaningless, and IBKR warns
    on it. Only GTD gets one."""
    for tif in ("GTC", "DAY", "IOC"):
        entry = _adapter(good_till="rth_close")._stamp_entry_timing(
            _Entry(), _order(tif=tif)
        )
        assert entry.tif == tif
        assert not entry.goodTillDate, f"{tif} was given a deadline"


def test_the_slave_default_tif_still_applies_when_the_route_is_silent():
    assert _adapter()._stamp_entry_timing(_Entry(), _order()).tif == "DAY"
    assert _adapter(entry_tif="GTC")._stamp_entry_timing(_Entry(), _order()).tif == "GTC"


def test_a_tif_the_adapter_cannot_place_is_still_refused():
    with pytest.raises(AdapterError):
        _adapter()._stamp_entry_timing(_Entry(), _order(tif="OPG"))


def test_the_engines_own_clock_resolves_a_boundary_too():
    """The pair has to travel together. A GTD order the broker keeps until
    the open is still cancelled by this engine's abandon timer if that
    timer is a shorter span -- so entry_timeout takes the same grammar,
    and float() on load would have made that impossible."""
    import inspect

    from app.adapters.ibkr import IBKRAdapter
    src = inspect.getsource(IBKRAdapter.open)
    assert "seconds_until(order.entry_timeout)" in src, (
        "entry_timeout is still float()-ed or passed raw, so a boundary "
        "there either crashes or is ignored"
    )
    assert "float(order.entry_timeout)" not in src
