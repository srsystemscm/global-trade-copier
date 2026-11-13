"""Protective legs must outlive the session.

Seen live on 2026-10-29: the adapter didn't set a time-in-force, so IBKR's
account-level order preset supplied DAY ("Error 10349: Order TIF was set to
DAY based on order preset"). The stop protecting a filled GC position would
have expired at the session close, leaving it naked overnight with nothing in
the system saying so. That matters most for exactly the long-hold systems the
contract roll exists for.
"""
from app.adapters import create_adapter
from app.adapters.ibkr import IBKRAdapter
from app.slaves import SlaveConfig


class _Order:
    def __init__(self):
        self.tif = None


def _adapter(**cfg):
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002, "client_id": 1}
    base.update(cfg)
    return create_adapter(SlaveConfig(id="i", name="i", broker_type="ibkr",
                                      enabled=True, paused=False, config=base))


def test_default_is_gtc():
    assert _adapter().bracket_tif == "GTC"


def test_protective_legs_are_stamped():
    a = _adapter()
    assert a._protective(_Order()).tif == "GTC"


def test_tif_is_configurable_per_slave():
    a = _adapter(bracket_tif="DAY")
    assert a._protective(_Order()).tif == "DAY"


def test_entry_orders_are_left_alone():
    """A market order that can't fill is better expiring than resting as GTC,
    so only the protective legs are stamped."""
    import inspect
    import re

    # Whitespace-normalised: this reads the source, so without it the test
    # fails whenever a call is wrapped across lines, which says nothing
    # about whether entries are stamped.
    src = re.sub(r"\s+", " ", inspect.getsource(IBKRAdapter.open))
    assert "_protective( ib_mod.StopOrder" in src or "_protective(ib_mod.StopOrder" in src
    assert "_protective( ib_mod.LimitOrder" in src or "_protective(ib_mod.LimitOrder" in src
    assert "_protective(ib_mod.MarketOrder" not in src
    assert "_protective( ib_mod.MarketOrder" not in src


def test_modify_rounds_and_stamps_too():
    """modify() rebuilds the bracket when the stop trails -- it was neither
    rounding to the tick nor setting TIF, so a trailing stop would have
    reintroduced both bugs on a position that started out correct."""
    import inspect
    src = inspect.getsource(IBKRAdapter.modify)
    assert src.count("_protective(") == 2
    assert src.count("_round_to_tick(") == 2
