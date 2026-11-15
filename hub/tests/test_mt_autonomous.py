"""An MT slave can compute its own ATR, so autonomous mode is reachable.

The last gap. `get_quote` was wired to SYMBOLS on 2026-11-15, but
`get_price_history` still raised: no EA command answered bars, so an
autonomous MT slave would start cleanly and refuse every signal for want
of the slave's own volatility.

Autonomous does not copy the master's stop. The master says how many
ATRs away its stop was, and that multiple is measured against the
instrument actually being traded -- which is the whole point on a slave
whose instrument is not the master's.

SlaveBridge.mq5 1.07 answers BARS. This pins the contract between the
two halves, because they are deployed separately: the hub upgrades from
git and the EA upgrades when somebody recompiles it in a terminal.
"""
import asyncio

import pytest

from app.adapters.base import AdapterError, AdapterRejectedError
from app.adapters.mt4 import MT4Adapter, _mql_timeframe
from app.atr import compute_atr


def _adapter():
    return MT4Adapter("test-mt", host="127.0.0.1", port=5599, timeout_ms=10)


def _bars(n, high=101.0, low=99.0, close=100.0):
    return {"status": "ok", "symbol": "ETHUSD", "timeframe": "D1",
            "count": n, "bars": [{"h": high, "l": low, "c": close} for _ in range(n)]}


def _wired(adapter, reply, record=None):
    async def _ask(command, name):
        if record is not None:
            record.append(command)
        return reply
    adapter._ask_tolerating_old_ea = _ask
    return adapter


# ------------------------------------------------- the bars come back ----

def test_bars_become_the_type_compute_atr_wants():
    a = _wired(_adapter(), _bars(19))
    bars = asyncio.run(a.get_price_history("ETHUSD", 14))
    assert len(bars) == 19
    assert bars[0].high == 101.0 and bars[0].low == 99.0 and bars[0].close == 100.0
    # The real point: the result is usable by the thing that asked for it.
    assert compute_atr(bars, 14) == pytest.approx(2.0)


def test_it_asks_for_more_than_the_minimum():
    """compute_atr needs period + 1 and refuses fewer, so asking for
    exactly the minimum makes one missing bar the difference between a
    position and a refusal. period + 5, matching the IBKR adapter."""
    sent = []
    a = _wired(_adapter(), _bars(19), record=sent)
    asyncio.run(a.get_price_history("ETHUSD", 14))
    assert sent[0]["count"] == 19
    assert sent[0]["cmd"] == "BARS"
    assert sent[0]["symbol"] == "ETHUSD"


def test_short_history_is_refused_with_what_to_do_about_it():
    """A cold symbol, or a timeframe never opened on a chart, comes back
    short while the terminal is still fetching. compute_atr would raise
    ValueError -- an arithmetic complaint for a data problem."""
    a = _wired(_adapter(), _bars(9))
    with pytest.raises(AdapterError) as got:
        asyncio.run(a.get_price_history("ETHUSD", 14))
    msg = str(got.value)
    assert "9" in msg and "15" in msg, "neither what arrived nor what was needed"
    assert "chart" in msg, "no hint at the fix"


def test_an_empty_reply_is_refused_rather_than_averaged():
    a = _wired(_adapter(), {"status": "ok", "count": 0, "bars": []})
    with pytest.raises(AdapterError):
        asyncio.run(a.get_price_history("ETHUSD", 14))


def test_an_old_ea_degrades_so_the_caller_can_fall_back():
    """A hub upgraded ahead of the EAs on someone's desktop is ordinary.
    NotImplementedError, not an error, so the engine reports a
    configuration state rather than a fault."""
    a = _adapter()

    async def _old(command, name):
        raise NotImplementedError("slave EA predates the BARS command")

    a._ask_tolerating_old_ea = _old
    with pytest.raises(NotImplementedError):
        asyncio.run(a.get_price_history("ETHUSD", 14))


# ------------------------------------------------- the timeframe ---------

def test_a_route_that_says_nothing_gets_daily():
    """Every route omitting bar_size has always meant daily."""
    assert _mql_timeframe(None) == "D1"
    assert _mql_timeframe("1 day") == "D1"


def test_the_hub_s_own_bar_sizes_all_map():
    """These are the strings the IBKR adapter already accepts, so a route
    reads the same whichever broker it lands on."""
    assert _mql_timeframe("4 hours") == "H4"
    assert _mql_timeframe("15 mins") == "M15"
    assert _mql_timeframe("1 hour") == "H1"
    assert _mql_timeframe("1 week") == "W1"


def test_singular_and_plural_both_work():
    """Config is typed by a person."""
    assert _mql_timeframe("4 hour") == _mql_timeframe("4 hours")
    assert _mql_timeframe("15 min") == _mql_timeframe("15 mins")


def test_an_unknown_bar_size_is_refused_not_defaulted():
    """Substituting D1 for a route that asked for H4 would size every
    position on it against the wrong volatility, and nothing would say
    so."""
    with pytest.raises(AdapterRejectedError):
        _mql_timeframe("3 fortnights")


def test_the_timeframe_travels_to_the_ea():
    sent = []
    a = _wired(_adapter(), _bars(19), record=sent)
    asyncio.run(a.get_price_history("ETHUSD", 14, bar_size="4 hours"))
    assert sent[0]["timeframe"] == "H4"


# ------------------------------------------------- the EA half -----------

def _ea_source():
    from pathlib import Path
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "bridge_ea" / "SlaveBridge.mq5"
        if candidate.exists():
            return candidate.read_text(encoding="utf-8", errors="replace")
    pytest.skip("SlaveBridge.mq5 not found")


def test_the_ea_answers_the_command_the_adapter_sends():
    src = _ea_source()
    assert 'if(cmd == "BARS")' in src, "the adapter asks for a command the EA drops"
    assert "string HandleBars" in src


def test_the_ea_version_says_it_has_bars():
    """The adapter's recompile warning names 1.07 for BARS, so the EA
    carrying it has to actually be 1.07."""
    src = _ea_source()
    assert '#define   BRIDGE_VERSION "1.07"' in src
    assert '#property version   "1.07"' in src


def test_the_ea_skips_the_forming_bar():
    """Bar 0 is still forming. A partial range reads as a quiet day,
    which sizes the next position too large -- in the direction nobody
    notices."""
    src = _ea_source()
    body = src[src.index("string HandleBars"):src.index("ENUM_TIMEFRAMES TimeframeFromName")]
    assert "CopyRates(symbol, tf, 1," in body, (
        "start_pos is not 1, so the incomplete current bar is included in "
        "the ATR"
    )


def test_the_ea_returns_bars_oldest_first():
    """compute_atr walks them forward using the previous close as the gap
    reference. Reversed, every gap inverts without anything failing."""
    src = _ea_source()
    body = src[src.index("string HandleBars"):src.index("ENUM_TIMEFRAMES TimeframeFromName")]
    assert "ArraySetAsSeries(rates, false)" in body


def test_the_ea_puts_the_market_watch_back():
    """An operator's Market Watch is theirs. CopyRates needs the symbol
    selected, so it is added and then restored."""
    src = _ea_source()
    body = src[src.index("string HandleBars"):src.index("ENUM_TIMEFRAMES TimeframeFromName")]
    assert "SymbolSelect(symbol, true)" in body
    assert "SymbolSelect(symbol, false)" in body


def test_the_ea_refuses_a_timeframe_it_does_not_know():
    src = _ea_source()
    body = src[src.index("ENUM_TIMEFRAMES TimeframeFromName"):]
    assert "(ENUM_TIMEFRAMES)-1" in body[:900], "an unknown timeframe defaults silently"
