"""An ETF leg that cannot be priced yet rests; it is not thrown away.

Sunday 2026-11-15, 21:26 ET. Three test signals on XAUUSD, and the ETF
slave answered each one the same way:

    UGL: no quote (IBKR returned no usable market price ... got -1.0.
    A negative price means IBKR has no market for it right now --
    usually the instrument's session is closed.)
    ... skipping OPEN ticket=382737649: nothing tradeable among UGL

US equities have no Sunday session; pre-market opens 04:00 ET Monday.
The routes were already limit + outside_rth with a 26-hour life, written
precisely so an evening signal could reach the next session -- but the
limit price is derived from a live quote, so with no quote there was
nothing to price and the signal was DISCARDED six and a half hours
before the instrument became tradeable. The configuration looked right
and could never work.

THE DISTINCTION BEING TESTED, which is the whole of it:

  DECLINED   "risks more than the 1.5% ceiling", "rounds to nothing"
             A decision. Retrying it every thirty seconds is a loop
             arguing with a policy. Still dropped.

  COULD NOT  "no quote", "no price history", "cannot reach the account"
             A circumstance. Exactly what retrying is for. Now held.

The deferral it now reaches is not new -- retry loop, disk persistence,
restore across restarts, and a drop when the master closes first all
existed. The no-quote path simply never reached it, because a candidate
that cannot be priced returns a reason where only a raised AdapterError
was deferred.
"""
import asyncio

import pytest

from app.adapters.base import AdapterError
from app.autonomous_copy_engine import (AutonomousCopyEngine, Refusal,
                                        Unavailable)


# ------------------------------------------------- the marker itself ----

def test_unavailable_is_a_string_everywhere_it_is_used():
    """A str subclass on purpose: these reasons are joined into log lines
    and events all over the engine, and none of that should have to know
    this type exists."""
    u = Unavailable("no quote (session closed)")
    assert isinstance(u, str)
    assert "; ".join(["UGL: %s" % u]) == "UGL: no quote (session closed)"
    assert "%s" % u == "no quote (session closed)"


def test_a_plain_reason_is_not_mistaken_for_a_transient_one():
    """The default has to be 'declined'. A reason that loses its marker
    degrades to the old behaviour -- dropped -- rather than becoming an
    infinite retry."""
    assert not isinstance("over the 1.50% ceiling", Unavailable)
    assert not isinstance("computed size 0.4 < min size 1", Unavailable)


# ------------------------------------- which reasons are marked ---------

def _source():
    import inspect
    return inspect.getsource(AutonomousCopyEngine._size_candidate)


def test_the_reasons_that_mean_could_not_are_marked():
    src = _source()
    for reason in ('no quote (', 'no price history (', 'no usable ATR ('):
        line = [l for l in src.splitlines() if reason in l and "return" in l]
        assert line, f"no return site for {reason!r}"
        assert all("Unavailable(" in l for l in line), (
            f"{reason!r} is not marked transient, so a shut market still "
            f"discards the signal"
        )


def test_a_decision_is_not_marked():
    """`sizing_result.reason` covers 'over the ceiling' and 'rounds to
    nothing'. Both say the same thing on every retry, so deferring them
    would hold a signal for hours to re-derive an answer already known."""
    src = _source()
    assert "return None, sizing_result.reason" in src
    assert "Unavailable(sizing_result.reason)" not in src
    # A target preflight could not resolve is not transient either.
    assert "return None, blocked" in src
    assert "Unavailable(blocked)" not in src


# ------------------------------------------- the behaviour --------------

class _Route:
    def __init__(self, target="UGL"):
        self.target = target
        self.candidates = [target]
        self.month_slot = 0
        self.atr_period = None
        self.bar_size = None
        self.sizing = None
        self.risk_scale = None
        self.netting = None
        self.order_type = "limit"
        self.outside_rth = True
        self.limit_offset_pct = 0.01
        self.tif = "GTC"
        self.entry_timeout = 93600
        self.good_till = None
        self.direction = None
        self.target_direction = None
        self.broker_brackets = False
        self.risk_management = None


def _engine(rejections, refused_flags=()):
    """An engine whose candidate sizing always fails, with the reasons
    given. Everything else on the open path is stubbed: the question here
    is only what happens when nothing can be traded."""
    e = object.__new__(AutonomousCopyEngine)
    e.slave_id = "ibkr-etf-1"
    e.blocked_targets = {}
    e._rejections = list(rejections)

    # **kw, not the exact signature. _size_candidate gained
    # has_smaller_alternative on 2026-11-15 and every stub mirroring its
    # parameters broke at once -- six tests failing on a keyword, saying
    # nothing about whether a shut market still defers.
    async def _size(signal, route, candidate, sl_mult, tp_mult, **kw):
        return None, e._rejections.pop(0)

    async def _emit(*a, **k):
        return None

    async def _preflight():
        return None

    async def _margin(*a, **k):
        return None

    e._size_candidate = _size
    e._emit = _emit
    e._await_preflight = _preflight
    e._margin_refusal = _margin
    e._netting_check = lambda *a, **k: None
    return e


def _open(engine, route):
    class _Sig:
        ticket = 382737649
        account = "44964870"
        direction = "BUY"
        symbol = "XAUUSD"
        magic = 221003
        price = 3884.64
        sl = 3836.72
        tp = None
        lots = 0.39
    return asyncio.run(engine._open_one(_Sig(), route, 1.5, None))


def test_a_shut_market_raises_so_the_route_loop_defers_it():
    """THE CASE. One candidate, no quote, nothing refused on policy.

    AdapterError is what the `for route in routes` loop already catches
    and hands to _defer_open, so this is how the signal reaches a
    subsystem that was always there."""
    e = _engine([Unavailable(
        "no quote (IBKR returned no usable market price for symbol=UGL "
        "(got -1.0, market_data_type=3))")])
    with pytest.raises(AdapterError) as exc:
        _open(e, _Route("UGL"))
    assert "UGL" in str(exc.value)
    assert "yet" in str(exc.value), (
        "the message should read as 'not tradeable YET', since the whole "
        "point is that this is temporary"
    )


def test_a_policy_refusal_is_still_dropped():
    """The 1.5% ceiling refusal from the same evening. Retrying a
    decision every thirty seconds until a 26-hour ceiling is a loop
    arguing with itself."""
    e = _engine(["size 6491.0 risks 17511.06, over the 1.50% ceiling of 13083.15"])
    assert _open(e, _Route("UGL")) is None, "a declined signal must not raise"


def test_a_size_that_rounds_to_nothing_is_dropped():
    e = _engine(["computed size 0.4 < min size 1, skipping"])
    assert _open(e, _Route("UGL")) is None


def test_one_tradeable_later_candidate_is_enough_to_hold_the_signal():
    """BITU has no quote, BITX has no quote: both could become tradeable,
    so the signal waits rather than being discarded."""
    r = _Route("BITU")
    r.candidates = ["BITU", "BITX"]
    e = _engine([Unavailable("no quote (session closed)"),
                 Unavailable("no quote (session closed)")])
    with pytest.raises(AdapterError):
        _open(e, r)


def test_a_mix_of_declined_and_unpriceable_keeps_the_old_behaviour():
    """DELIBERATELY STRICT. If any candidate was turned away by a RULE,
    this drops as before rather than retrying a decision on the chance
    that a sibling instrument starts quoting.

    The alternative -- defer if anything at all was transient -- would
    turn a netting refusal into a retry loop whenever one leg of a pair
    happened to be unquoted, which is most evenings."""
    r = _Route("BITU")
    r.candidates = ["BITU", "BITX"]

    e = _engine([Unavailable("no quote (session closed)"),
                 "already holding 3 legs on BITX"])
    # The second candidate's refusal has to go through _netting_check to
    # land in `refused`, so drive it that way.
    calls = {"n": 0}

    def _netting(*a, **k):
        calls["n"] += 1
        return Refusal(reason="already holding 3 legs", overflowable=False)

    e._netting_check = _netting
    e._rejections = [Unavailable("no quote (session closed)"), None]

    async def _size(signal, route, candidate, sl_mult, tp_mult, **kw):
        nxt = e._rejections.pop(0)
        if nxt is None:
            return {"symbol": candidate, "account": object(),
                    "real_account": object()}, None
        return None, nxt

    e._size_candidate = _size
    assert _open(e, r) is None, (
        "a rule refusal anywhere in the list must not become a retry loop"
    )
