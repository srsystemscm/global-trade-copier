"""Settings the adapter reads from itself take effect on save.

The adapter reads entry_chase_steps, entry_split_slices and the rest in
its CONSTRUCTOR, which runs once at startup. PATCH /slaves/{id}
live-updates the engine, so a route's setting took effect immediately
while a slave-level one sat in the config doing nothing until the next
restart.

Measured on 2026-11-17: the card read "entries split into 3 over 5s" and
831 MET went out in a single order, because the adapter still held the 0
it was built with ten hours earlier.

That is a setting which saves cleanly and does nothing, and this file has
had to go back and gate three of them -- the mode switch, sizing on a
route-sized slave, the contract roll on an ETF. Refreshing the adapter is
cheaper than adding a fourth.

THE CONNECTION DETAILS ARE DELIBERATELY NOT REFRESHED. host, port,
client_id and account are read once, to open a session; rewriting them on
a live adapter would leave it connected to one place and labelled
another.
"""
import pytest

from app.api_routes import _LIVE_ADAPTER_TUNABLES, _refresh_adapter_tunables


class _Adapter:
    """Only the attributes a real one exposes, so the hasattr guard is
    exercised rather than assumed."""
    def __init__(self):
        self.entry_chase_steps = 0
        self.entry_chase_interval = 300.0
        self.entry_chase_max_pct = 0.01
        self.entry_split_slices = 0
        self.entry_split_interval = 0.0
        self.quote_fallback = "strict"
        self.host = "127.0.0.1"
        self.port = 4002
        self.client_id = 1
        self.account = "DUR314653"


# ------------------------------------------------- the refresh ---------

def test_the_chase_settings_reach_a_running_adapter():
    """THE POINT. 60 seconds against a 30-second trade never fires, and
    the fix for that must not be 'restart the hub'."""
    a = _Adapter()
    _refresh_adapter_tunables(a, {
        "entry_chase_steps": 4,
        "entry_chase_interval": 5,
        "entry_chase_max_pct": 0.02,
    })
    assert a.entry_chase_steps == 4
    assert a.entry_chase_interval == 5.0
    assert a.entry_chase_max_pct == 0.02


def test_the_split_settings_reach_it_too():
    a = _Adapter()
    _refresh_adapter_tunables(a, {"entry_split_slices": 3,
                                  "entry_split_interval": 5})
    assert a.entry_split_slices == 3
    assert a.entry_split_interval == 5.0


def test_a_cleared_setting_goes_back_to_its_default():
    """Turning something off has to reach the adapter as surely as
    turning it on. A config with the key deleted means off, and an
    adapter still holding the old number would keep chasing."""
    a = _Adapter()
    a.entry_chase_steps = 4
    a.entry_split_slices = 3
    _refresh_adapter_tunables(a, {})
    assert a.entry_chase_steps == 0
    assert a.entry_split_slices == 0


def test_the_connection_is_never_rewritten():
    """A live adapter rewritten to another host is connected to one place
    and labelled another. Those need a restart and the UI says so."""
    for key in ("host", "port", "client_id", "account"):
        assert key not in _LIVE_ADAPTER_TUNABLES, (
            f"{key} is a connection detail and must not be refreshed under "
            "a running session"
        )

    a = _Adapter()
    _refresh_adapter_tunables(a, {"host": "10.0.0.9", "port": 7497,
                                  "client_id": 42, "account": "DU999"})
    assert (a.host, a.port, a.client_id, a.account) == (
        "127.0.0.1", 4002, 1, "DUR314653")


def test_a_value_that_will_not_cast_does_not_fail_the_save(caplog):
    """A config error is not a reason to abandon the rest of the refresh
    or to reject the whole PATCH."""
    import logging

    a = _Adapter()
    with caplog.at_level(logging.WARNING):
        _refresh_adapter_tunables(a, {"entry_chase_steps": "four",
                                      "entry_split_slices": 3})
    assert a.entry_split_slices == 3, "one bad value stopped the rest"
    assert "cannot be applied" in caplog.text


def test_no_adapter_is_not_an_error():
    """A slave with no engine running has no adapter to refresh."""
    _refresh_adapter_tunables(None, {"entry_chase_steps": 4})


def test_an_adapter_without_the_attribute_is_left_alone():
    """MT4Adapter has no entry chase. Setting one on it would invent a
    field nothing reads."""
    class _Bare:
        pass

    bare = _Bare()
    _refresh_adapter_tunables(bare, {"entry_chase_steps": 4})
    assert not hasattr(bare, "entry_chase_steps")


# ------------------------------------------------- it is called --------

def test_patch_slaves_refreshes_the_adapter():
    import inspect

    from app import api_routes

    src = inspect.getsource(api_routes.patch_slave)
    assert "_refresh_adapter_tunables(" in src, (
        "PATCH updates the engine but not the adapter, so a slave-level "
        "setting saves cleanly and does nothing until the hub restarts"
    )
