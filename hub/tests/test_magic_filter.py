"""Each slave takes a selection of the master's strategies, by magic number.

One master terminal runs thirty or forty origin EAs down one PUB socket.
The selection of which strategies reach which slave lives on the slave and
is applied once in the Signal Bus's fan-out -- see app/magic_filter.py for
why it is there rather than in the EA or in each engine.
"""
import asyncio
import json
import time

import pytest

from app import magic_filter
from app.events import EventBus
from app.magic_filter import MagicFilter
from app.models import Signal
from app.routing import describe_refusal, resolve_routes
from app.signal_bus import SignalBus


def sig(magic=1001, ticket=1, symbol="US2000", action="OPEN"):
    return Signal(ticket=ticket, symbol=symbol, action=action, direction="BUY",
                  lots=1.0, price=2819.0, sl=2684.0, atr=45.0, magic=magic,
                  account="11558127", ts=time.time())


def raw(magic=1001, ticket=1, symbol="US2000", action="OPEN"):
    body = {"ticket": ticket, "symbol": symbol, "action": action,
            "direction": "BUY", "lots": 1.0, "price": 2819.0, "sl": 2684.0,
            "atr": 45.0, "account": "11558127", "ts": time.time()}
    if magic is not None:
        body["magic"] = magic
    return json.dumps(body).encode("utf-8")


# ------------------------------------------------------------------- parsing


def test_no_filter_takes_everything():
    """Which is what every slave configured today does."""
    takes = magic_filter.parse({})
    assert takes.configured is False
    assert takes.accepts(sig(magic=1)) == (True, None)
    assert takes.accepts(sig(magic=None)) == (True, None)
    assert takes.as_dict() is None, "null, not an empty selection"


def test_a_bare_list_is_an_allow_list():
    takes = magic_filter.parse({"magic_filter": [1001, 1002]})
    assert takes.accepts(sig(magic=1001))[0] is True
    assert takes.accepts(sig(magic=1003))[0] is False
    assert "not among the 2 this slave takes" in takes.accepts(sig(magic=1003))[1]


def test_deny_wins_over_allow():
    """So one magic can be taken out of service without editing the list."""
    takes = magic_filter.parse(
        {"magic_filter": {"allow": [1001, 1002, 1003], "deny": [1002]}})
    assert takes.accepts(sig(magic=1001))[0] is True
    assert takes.accepts(sig(magic=1002))[0] is False
    assert "deny-list" in takes.accepts(sig(magic=1002))[1]


def test_a_deny_list_alone_takes_everything_else():
    takes = magic_filter.parse({"magic_filter": {"deny": [9999]}})
    assert takes.accepts(sig(magic=9999))[0] is False
    assert takes.accepts(sig(magic=1))[0] is True
    assert takes.accepts(sig(magic=None))[0] is True, (
        "nothing was excluded, so an unmagicked signal is not excluded either"
    )


def test_magic_zero_is_a_real_value():
    """OrderMagicNumber() is 0 for a trade placed by hand, and 0 is falsy.

    Read as a boolean rather than compared to None, a slave configured to
    take manual trades would take nothing and one configured to deny them
    would deny everything.
    """
    assert magic_filter.parse({"magic_filter": [0]}).accepts(sig(magic=0))[0] is True
    assert magic_filter.parse({"magic_filter": [0]}).accepts(sig(magic=1))[0] is False
    deny_manual = magic_filter.parse({"magic_filter": {"deny": [0]}})
    assert deny_manual.accepts(sig(magic=0))[0] is False
    assert deny_manual.accepts(sig(magic=1))[0] is True


def test_an_allow_list_refuses_a_signal_with_no_magic():
    """A bridge EA older than 1.02 sends none.

    Same reasoning as strict `routes`: a slave with an allow-list has
    stated which strategies it takes, and "cannot tell" is not one of them.
    """
    takes = magic_filter.parse({"magic_filter": [1001]})
    accepts, why = takes.accepts(sig(magic=None))
    assert accepts is False
    assert "no magic number" in why


@pytest.mark.parametrize("bad, fragment", [
    ({"magic_filter": ["not-a-number"]}, "not a magic number"),
    ({"magic_filter": {"allow": [1001], "alow": [1002]}}, "unknown key"),
    ({"magic_filter": {"allow": []}}, "allows nothing"),
    ({"magic_filter": {"allow": [{"strategy": "no magic here"}]}}, "no `magic` key"),
    ({"magic_filter": {"allow": [1], "source": "a string"}}, "source"),
])
def test_an_unreadable_selection_raises_rather_than_failing_open(bad, fragment):
    """A typo in an allow-list fails OPEN, which is the one direction this
    must not fail: the slave would copy a strategy nobody put on its list.

    Raised here, start_engines() stops this one slave loudly and keeps
    every other one running.
    """
    with pytest.raises(ValueError) as exc:
        magic_filter.parse(bad)
    assert fragment in str(exc.value)


def test_an_empty_allow_list_is_not_the_same_as_no_allow_list():
    """`{"allow": []}` read as a boolean would pass every signal it exists
    to stop, so it is refused outright rather than silently ignored."""
    assert magic_filter.parse({}).allow is None
    with pytest.raises(ValueError):
        magic_filter.parse({"magic_filter": {"allow": []}})


# ------------------------------------------- importing from the registry


def test_a_registry_export_can_be_pasted_in_with_its_names():
    """The Operations hub's registry is what names a magic number.

    An entry may be an object carrying whatever that registry knows; the
    copier reads `magic`, keeps any name, and ignores the rest -- it has no
    business failing on a field the portfolio manager added for its own
    reasons.
    """
    takes = magic_filter.parse({"magic_filter": {
        "allow": [
            {"magic": 1001, "strategy": "US2000 trend", "portfolio": "Trend",
             "asset_class": "equity_index", "something_new": True},
            {"magic": 1002, "strategy": "GC breakout", "portfolio": "Trend"},
        ],
        "source": {"kind": "ops_hub_registry", "portfolio": "Trend",
                   "imported_at": "2026-11-12T20:05:02Z", "revision": 7},
    }})

    assert takes.accepts(sig(magic=1001))[0] is True
    assert takes.accepts(sig(magic=9999))[0] is False
    assert takes.labels == {1001: "US2000 trend", 1002: "GC breakout"}
    assert takes.source["revision"] == 7
    assert "US2000 trend" in takes.describe(), (
        "the names should reach the log line that says what a slave takes"
    )

    reported = takes.as_dict()
    assert reported["allow"] == [1001, 1002]
    assert reported["labels"] == {"1001": "US2000 trend", "1002": "GC breakout"}
    assert reported["source"]["portfolio"] == "Trend"


def test_the_copier_never_resolves_a_source_itself():
    """`source` is recorded and never acted on.

    The reporter is push-only. A copier that fetched its own risk
    configuration from a web service would have the market open while a
    failed GET decided which strategies it was copying -- so resolving a
    portfolio to a set of magics is an offline step that writes the
    resolved list through PATCH /slaves.
    """
    import inspect

    source = inspect.getsource(magic_filter)
    for forbidden in ("httpx", "requests", "urlopen", "fetch("):
        assert forbidden not in source, (
            f"magic_filter reaches the network via {forbidden}"
        )

    # A source naming a portfolio with no allow-list resolves to nothing
    # and is therefore not a filter: the slave still takes everything,
    # rather than silently taking nothing because a fetch never happened.
    takes = magic_filter.parse({"magic_filter": {
        "source": {"kind": "ops_hub_registry", "portfolio": "Trend"}}})
    assert takes.configured is False
    assert takes.accepts(sig(magic=1))[0] is True


# ------------------------------------------------------------- the fan-out


def test_the_bus_fans_a_signal_only_to_the_slaves_that_take_it():
    bus = SignalBus()
    trend = bus.register_slave("ibkr-trend", magic_filter.parse(
        {"magic_filter": [1001, 1002]}))
    mean_rev = bus.register_slave("ibkr-meanrev", magic_filter.parse(
        {"magic_filter": [2001]}))
    everything = bus.register_slave("mt5-icm-1")

    asyncio.run(bus.publish_raw(raw(magic=1001, ticket=11)))
    asyncio.run(bus.publish_raw(raw(magic=2001, ticket=12)))
    asyncio.run(bus.publish_raw(raw(magic=7777, ticket=13)))

    assert [s.ticket for s in _drain(trend)] == [11]
    assert [s.ticket for s in _drain(mean_rev)] == [12]
    assert [s.ticket for s in _drain(everything)] == [11, 12, 13]


def _drain(queue):
    out = []
    while not queue.empty():
        out.append(queue.get_nowait())
    return out


def test_the_reported_target_count_is_the_slaves_that_took_it():
    """`signal_received.targets` is what the Operations hub counts received
    against copied with. Counting every registered slave would make it
    wrong for every signal on a hub where each slave takes a selection.
    """
    seen = []

    class Collecting(EventBus):
        async def emit(self, event):
            seen.append(event)

    bus = SignalBus(events=Collecting())
    bus.register_slave("a", magic_filter.parse({"magic_filter": [1001]}))
    bus.register_slave("b", magic_filter.parse({"magic_filter": [1001]}))
    bus.register_slave("c", magic_filter.parse({"magic_filter": [2001]}))

    asyncio.run(bus.publish_raw(raw(magic=1001, ticket=21)))
    assert [e["targets"] for e in seen if e["type"] == "signal"] == [2]

    seen.clear()
    asyncio.run(bus.publish_raw(raw(magic=9999, ticket=22)))
    assert [e["targets"] for e in seen if e["type"] == "signal"] == [0], (
        "a signal no slave takes reached nothing, and says so"
    )


def test_a_signal_a_slave_does_not_take_never_reaches_its_queue():
    """Dropped in the engine instead, it would be indistinguishable in a log
    from one refused for a reason that matters."""
    bus = SignalBus()
    queue = bus.register_slave("s1", magic_filter.parse({"magic_filter": [1001]}))
    asyncio.run(bus.publish_raw(raw(magic=5555)))
    assert queue.empty()


def test_the_selection_can_be_changed_without_a_restart():
    """Every other part of a slave's config live-updates through PATCH."""
    bus = SignalBus()
    queue = bus.register_slave("s1", magic_filter.parse({"magic_filter": [1001]}))

    asyncio.run(bus.publish_raw(raw(magic=2002, ticket=31)))
    assert queue.empty()

    bus.set_magic_filter("s1", magic_filter.parse({"magic_filter": [1001, 2002]}))
    asyncio.run(bus.publish_raw(raw(magic=2002, ticket=32)))
    assert [s.ticket for s in _drain(queue)] == [32]


def test_changing_an_unknown_slaves_selection_does_nothing():
    bus = SignalBus()
    bus.set_magic_filter("never-registered", MagicFilter(allow=frozenset({1})))
    assert "never-registered" not in bus._magic


def test_unregistering_forgets_the_selection():
    bus = SignalBus()
    bus.register_slave("s1", magic_filter.parse({"magic_filter": [1001]}))
    bus.unregister_slave("s1")
    assert bus._magic == {}


def test_every_action_is_filtered_not_only_opens():
    """The master EA puts OrderMagicNumber() on OPEN, MODIFY and CLOSE
    alike, so filtering all three is safe -- and filtering only OPEN would
    send a slave the exits of trades it never entered.
    """
    bus = SignalBus()
    queue = bus.register_slave("s1", magic_filter.parse({"magic_filter": [1001]}))
    for action in ("OPEN", "MODIFY", "CLOSE"):
        asyncio.run(bus.publish_raw(raw(magic=2002, ticket=41, action=action)))
    assert queue.empty()


# ----------------------------------------------------- routes take a list


def test_a_route_can_name_several_magics():
    """Several strategies routinely want the same instrument on one slave,
    and a near-identical route for each is how one comes to differ by a
    typo nobody notices.
    """
    config = {"routes": [
        {"symbol": "US2000", "magic": [1001, 1002, 1003], "target": "M2K"},
    ]}
    assert [r.target for r in resolve_routes(config, sig(magic=1002))] == ["M2K"]
    assert resolve_routes(config, sig(magic=9999)) == []


def test_a_route_built_from_a_list_records_the_magic_that_matched():
    config = {"routes": [{"symbol": "US2000", "magic": [1001, 1002], "target": "M2K"}]}
    assert resolve_routes(config, sig(magic=1002))[0].magic == 1002


def test_a_single_magic_route_still_works():
    config = {"routes": [{"symbol": "US2000", "magic": 1001, "target": "M2K"}]}
    assert [r.magic for r in resolve_routes(config, sig(magic=1001))] == [1001]
    assert resolve_routes(config, sig(magic=1002)) == []


def test_a_magic_list_route_still_refuses_a_signal_with_no_magic():
    config = {"routes": [{"symbol": "US2000", "magic": [1001], "target": "M2K"}]}
    assert resolve_routes(config, sig(magic=None)) == []


def test_a_catch_all_route_is_unaffected():
    config = {"routes": [{"symbol": "US2000", "target": "M2K"}]}
    assert len(resolve_routes(config, sig(magic=9999))) == 1
    assert resolve_routes(config, sig(magic=9999))[0].magic is None


def test_the_refusal_message_survives_a_magic_list_and_a_catch_all():
    """It used to raise while explaining a refusal: a list is unhashable,
    and a tuple of (symbol, None) against (symbol, 1001) cannot be sorted.
    Either way the message that says why a signal was refused was itself
    the thing that failed.
    """
    config = {"routes": [
        {"symbol": "US2000", "magic": [1001, 1002], "target": "M2K"},
        {"symbol": "US2000", "target": "M2K"},
        {"symbol": "XAUUSD", "magic": 3001, "target": "MGC"},
    ]}
    message = describe_refusal(config, sig(magic=9999, symbol="BTCUSD"))
    assert "no route for symbol=BTCUSD magic=9999" in message
    assert "[1001, 1002]" in message
