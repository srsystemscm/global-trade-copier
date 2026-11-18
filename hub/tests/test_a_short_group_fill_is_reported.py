"""When the MT5 bridge cannot open the whole size, somebody is told.

An MT5 symbol's SYMBOL_VOLUME_MAX can be far below a sized position --
Darwinex caps MBT_X at 20 lots against a 200-lot trade -- so SlaveBridge
cuts the volume into clips and holds them together as one group under
the first leg's ticket. The hub's model is unchanged: one master ticket,
one slave ticket, CLOSE and MODIFY acting on every leg.

If some clips open and others are refused it does NOT roll back. The
filled part is real and unwinding could fail too, so it reports the
shortfall in `warning` instead, with the comment "so it cannot pass
unnoticed".

It passed unnoticed. MT4Adapter.open() read slave_ticket, price and
lots, and dropped the warning on the floor. `lots` is what filled, so
the registry was right -- but nothing anywhere said the position was
smaller than the signal asked for. On a slave whose sizes routinely
exceed the per-order cap, that is the normal failure rather than a rare
one.
"""
import logging

import pytest

from app.adapters.base import OrderRequest
from app.adapters.mt4 import MT4Adapter


def _adapter(reply):
    a = object.__new__(MT4Adapter)
    a.slave_id = "mt5-dwx-futures-1"

    async def _request(_payload):
        return reply

    a._request = _request
    return a


def _order():
    return OrderRequest(symbol="MBT_X", direction="BUY", size=200.0, sl=60000.0)


def _open(adapter):
    import asyncio
    return asyncio.run(adapter.open(_order()))


def test_a_short_group_fill_is_reported(caplog):
    """THE BUG. The EA says so and the hub has to repeat it."""
    a = _adapter({
        "slave_ticket": 12345, "price": 64250.0, "lots": 120.0, "legs": 6,
        "warning": "only 6 of 10 clips filled (120.00 of 200.00 lots): "
                   "not enough money",
    })
    with caplog.at_level(logging.ERROR):
        result = _open(a)

    assert "opened SHORT" in caplog.text, (
        "the bridge reported a shortfall and the hub said nothing"
    )
    assert "120.00 of 200.00" in caplog.text, "the figures are not in the alert"
    # What filled is still what is recorded -- the position is real.
    assert result.lots == 120.0
    assert result.slave_ticket == 12345


def test_the_alert_says_what_it_means_for_the_copy(caplog):
    """A reader needs the consequence, not just the arithmetic: this copy
    carries less risk than the master's signal asked for.

    Asserted on what is LOGGED, not on the source. The first version of
    this searched inspect.getsource for the phrase and failed while the
    behaviour was working perfectly -- the sentence is split across two
    adjacent string literals, so it exists in the output and never in
    any single line of the file. A test of a message should read the
    message."""
    a = _adapter({"slave_ticket": 1, "price": 1.0, "lots": 1.0,
                  "warning": "only 1 of 2 clips filled"})
    with caplog.at_level(logging.ERROR):
        _open(a)
    assert "less risk than the master" in caplog.text


def test_a_whole_fill_says_nothing(caplog):
    """The ordinary case must stay quiet, or the alert stops being read."""
    a = _adapter({"slave_ticket": 12345, "price": 64250.0, "lots": 200.0,
                  "legs": 10})
    with caplog.at_level(logging.ERROR):
        result = _open(a)
    assert "opened SHORT" not in caplog.text
    assert result.lots == 200.0


def test_a_single_leg_open_is_unaffected(caplog):
    """Most opens are one deal and carry no group at all."""
    a = _adapter({"slave_ticket": 999, "price": 3850.0, "lots": 1.0})
    with caplog.at_level(logging.ERROR):
        result = _open(a)
    assert caplog.text == ""
    assert result.slave_ticket == 999
