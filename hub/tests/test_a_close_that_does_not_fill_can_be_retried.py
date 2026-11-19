"""A close that never filled left the position unreachable forever.

IBKRAdapter.close() dropped its own record of a position the moment the
closing order was ACCEPTED, before any fill:

    trade = self._place(pos["contract"], closing_order)
    del self._positions[slave_ticket]

The reason was sound -- a second CLOSE arriving during the wait must not
place a second closing order. The cost was not: when the closing order
rested, was refused or expired, the position stayed at the broker while
the adapter had forgotten it, and every later attempt died at the lookup
with "no tracked IBKR position for slave_ticket=N".

MEASURED, not imagined. ibkr-etf-1 held 789 ETHT like that from
2026-11-17 18:00: 965 attempts over twenty-four hours, through four log
rotations and several hub restarts. Each restart reloaded the position
from engine_positions, granted exactly one more attempt, and lost it
again -- which is why it looked like a stuck retry rather than a
one-way door.

The entry is kept now and carries the id of the order closing it, and
the double-close guard reads that order's real status from IBKR. These
tests hold the two halves apart: an exit still working must block a
second one, and an exit that died must not.
"""
import pytest

from app.adapters.base import AdapterRejectedError


class _Order:
    def __init__(self, order_id):
        self.orderId = order_id
        self.action = "SELL"
        self.orderType = "MKT"
        self.tif = "DAY"
        self.outsideRth = False
        self.lmtPrice = 0.0


class _Status:
    def __init__(self, status):
        self.status = status


class _Trade:
    def __init__(self, order_id, status):
        self.order = _Order(order_id)
        self.orderStatus = _Status(status)
        self.contract = object()


class _Ib:
    """Only `trades()`, which is all _live_closing_order touches."""
    def __init__(self, trades):
        self._trades = trades

    def trades(self):
        return self._trades


def adapter(trades):
    from app.adapters.ibkr import IBKRAdapter

    a = IBKRAdapter.__new__(IBKRAdapter)
    a.slave_id = "ibkr-etf-1"
    a.ib = _Ib(trades)
    a._positions = {}
    return a


# --- the guard -------------------------------------------------------

@pytest.mark.parametrize("status", ["ApiPending", "PendingSubmit",
                                    "PreSubmitted", "Submitted"])
def test_an_exit_still_working_blocks_a_second_one(status):
    """The property the old `del` was there to protect, kept. A second
    closing order would sell a position that is already being sold."""
    a = adapter([_Trade(4972, status)])
    assert a._live_closing_order({"closing_order_id": 4972}) is not None


@pytest.mark.parametrize("status", ["Cancelled", "ApiCancelled", "Inactive",
                                    "Filled", "Expired"])
def test_an_exit_that_is_over_does_not_block_a_retry(status):
    """The defect. A closing order that rested and expired left a live
    position that nothing could ever close again."""
    a = adapter([_Trade(4972, status)])
    assert a._live_closing_order({"closing_order_id": 4972}) is None


def test_a_position_with_no_close_in_flight_is_free_to_close():
    a = adapter([_Trade(4972, "Submitted")])
    assert a._live_closing_order({}) is None


def test_an_order_id_the_broker_no_longer_lists_does_not_block():
    """IBKR drops finished orders from trades() after a while. An id that
    matches nothing is an order that is over, not one still working --
    reading it the other way would strand the position again, by the
    same mechanism and for the opposite reason."""
    a = adapter([_Trade(1111, "Submitted")])
    assert a._live_closing_order({"closing_order_id": 4972}) is None


def test_the_position_s_own_stop_is_not_mistaken_for_a_close():
    """A protective STOP is a SELL on the same contract, so matching on
    contract and direction would have a close refuse ITSELF forever --
    worse than the bug being fixed. Only the remembered order id counts."""
    stop = _Trade(5050, "PreSubmitted")
    stop.order.orderType = "STP"
    a = adapter([stop])
    assert a._live_closing_order({"closing_order_id": 4972}) is None


# --- what close() does with it ---------------------------------------

def test_close_still_refuses_a_position_it_never_knew():
    """Unchanged, and still the right answer: this is a CLOSE for
    something this adapter never opened."""
    import asyncio

    a = adapter([])
    a._require_ib = lambda: None
    a._require_connected = lambda: None
    with pytest.raises(AdapterRejectedError, match="no tracked IBKR position"):
        asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
            a.close(1485))


def test_the_record_is_dropped_on_a_fill_and_not_on_a_placement():
    """Said as source, because the two lines are three hundred apart and
    the whole defect was that the drop happened at the wrong one."""
    import inspect

    from app.adapters import ibkr

    def code_only(func):
        """Source with the comments taken out.

        Needed because the fix's own comment QUOTES the deleted line to
        explain it, and the first version of this test matched that
        explanation and reported the bug as unfixed. A test that reads
        prose is a test that fails on good writing."""
        lines = []
        for line in inspect.getsource(func).splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            lines.append(line.split("  #")[0])
        return chr(10).join(lines)

    close_src = code_only(ibkr.IBKRAdapter.close)
    assert "del self._positions[" not in close_src, (
        "close() drops the position again -- a close that does not fill "
        "will strand it exactly as before"
    )
    assert "closing_order_id" in close_src

    await_src = code_only(ibkr.IBKRAdapter.await_exit_fill)
    assert await_src.count("self._positions.pop(slave_ticket, None)") == 2, (
        "the position must be dropped on BOTH confirmed-fill paths -- the "
        "first wait and the chase -- and on neither of the paths where the "
        "order is still live"
    )
