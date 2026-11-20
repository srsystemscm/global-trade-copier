"""A split entry has to be visible somewhere.

Chris, after watching three runs work perfectly: "its working, but i dont
think the order split is showing. same with the copier tab on the copier
hub? just cant see the split orders anywhere."

He was right, and it follows from the design. A split entry is ONE
position with ONE size -- one registry row, one pair of protective legs,
one thing to roll, which is the whole reason it was built that way. So
nothing anywhere distinguished 33 MGC arriving in a single print from 33
MGC assembled out of three orders over ten seconds. The size column is
identical.

Three places, three questions:

  the LOG says what went out just now -- "slice 2/3 ... (22.0 of 33.0
  placed)". Already there.

  the EVENT says what was placed: size is the position's size and `legs`
  is how many broker orders it is being assembled from. That field was
  written for this before the splitter existed -- "a size that had to be
  split across several would be reported as the number it became" -- and
  was hardcoded to 1.

  the ROW says how it finished: slave_lots is the aggregate across
  slices and entry_slices is how many there actually were. A CLOSE
  arriving mid-window calls off the rest, so planned and actual differ
  and both matter.
"""
import inspect

from app.adapters.base import OrderResult
from app.hub_reporter import snapshot as snap
from app.hub_reporter import rows as leg_rows


# ======================================================== the leg row ===

class _Facts:
    def slave_account(self, _slave_id):
        return "DUR314653"

    def is_autonomous(self, _slave_id):
        return True

    def size_unit(self, _slave_id, _symbol):
        return "contracts"


def _row(**over):
    base = {
        "slave_id": "ibkr-paper-1", "master_account": "44964870",
        "master_ticket": 382958236, "slave_symbol": "MGC",
        "slave_ticket": 1994, "status": "CLOSED",
        "opened_at": 1.0, "closed_at": 2.0, "requested_at": 1.0,
        "filled_at": 1.5, "master_price": 3861.84, "slave_price": 3866.9,
        "slave_lots": 33.0, "master_close_price": 3860.7,
        "slave_close_price": 3859.845455,
        "broker_order_id": "99", "broker_order_ref": "ref",
        "entry_slices": 3,
    }
    base.update(over)
    return base


def test_the_leg_row_says_how_many_orders_the_entry_took():
    """The durable record. Without it the hub cannot tell a 33-lot print
    from 33 lots assembled over a window, and the slippage it computes
    means a different thing in each case."""
    leg = leg_rows._leg(_row(), _Facts())
    assert leg["entry_slices"] == 3
    # The size is the WHOLE position either way -- that is the point.
    assert leg["size"] == 33.0


def test_an_unsplit_entry_reports_one_or_nothing():
    assert leg_rows._leg(_row(entry_slices=1), _Facts())["entry_slices"] == 1
    assert leg_rows._leg(_row(entry_slices=None), _Facts())["entry_slices"] is None


def test_the_copier_tab_can_see_it_too():
    """The tab reads its legs from trade_registry.recent_trades, not from
    the reporter, so the column has to come through that query as well --
    a field on the ops hub contract alone would leave the dashboard
    exactly as blind as it was."""
    import app.trade_registry as reg

    src = inspect.getsource(reg)
    assert "slave_close_price, entry_slices" in src, (
        "the Copier tab's query does not select entry_slices, so the tab "
        "cannot show it however the ops hub is told"
    )


# ===================================================== what was placed ==

def test_the_open_result_carries_its_slice_count():
    """Reported by the adapter rather than recomputed by the engine: a
    size too small to go round comes back as fewer, larger slices than
    the config asked for, and only the adapter knows that happened."""
    assert OrderResult(slave_ticket=1).slices is None
    assert OrderResult(slave_ticket=1, slices=3).slices == 3


def test_the_event_fills_in_the_legs_field_it_was_given():
    """`legs` is documented as "how many broker orders the size became"
    and was hardcoded to 1 with a comment predicting this exact case."""
    src = inspect.getsource(
        __import__("app.autonomous_copy_engine", fromlist=["x"]))
    assert "legs=result.slices or 1" in src, (
        "slave_open still reports legs=1, so a split entry is "
        "indistinguishable from a single print on the ops hub"
    )


def test_the_event_size_is_the_position_not_the_first_slice():
    """size and legs have to describe ONE thing -- "15 contracts in 3
    orders", not "5 contracts in 3 orders". Setting OrderResult.lots to
    the first slice silently changed this, which is how a reporting
    field breaks without anyone touching the reporting."""
    src = inspect.getsource(
        __import__("app.autonomous_copy_engine", fromlist=["x"]))
    assert "size=sizing_result.size," in src, (
        "slave_open reports the first slice as the position's size"
    )


# ======================================================== the snapshot ==

class _Slave:
    def __init__(self, config):
        self.id = "ibkr-paper-1"
        self.config = config


def test_the_snapshot_reports_the_split_per_route():
    out = snap._split(_Slave({"routes": [
        {"symbol": "XAUUSD", "target": ["GC", "MGC"],
         "split_slices": 3, "split_interval": 5},
    ]}))
    assert out is not None, "a configured split was not reported at all"
    assert out["by_target"] == {"GC": {"slices": 3, "interval_s": 5.0}}


def test_the_snapshot_reports_a_slave_wide_split():
    out = snap._split(_Slave({
        "entry_split_slices": 4, "entry_split_interval": 30}))
    assert out["slices"] == 4
    assert out["interval_s"] == 30.0


def test_a_slave_with_no_split_reports_null():
    """Not an empty object. Every slave has this field and most of them
    are not splitting anything; a dictionary of nulls reads as though
    something were configured."""
    assert snap._split(_Slave({"routes": [{"target": "MGC"}]})) is None


def test_the_snapshot_reports_whether_the_loop_is_consuming():
    """The hub is what is watching when nobody is at the dashboard, and
    `running`/`connected` describe the object and the adapter. On
    2026-11-16 four engines reported both while consuming nothing."""
    import asyncio

    class _Task:
        @staticmethod
        def done():
            return True

    class _Engine:
        slave_id = "ibkr-paper-1"
        queue = asyncio.Queue()
        last_consumed_at = 0.0
        _consume_task = _Task()

    out = snap._consume(_Engine())
    assert out["alive"] is False, "a dead consume loop reported as alive"
    assert out["stalled"] is True


def test_the_snapshot_says_nothing_about_a_slave_with_no_engine():
    assert snap._consume(None) is None
