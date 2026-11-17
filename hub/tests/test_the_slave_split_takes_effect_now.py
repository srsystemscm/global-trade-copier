"""A slave-level split works without waiting for a restart.

MEASURED on 2026-11-17, across one suite on one process:

    BTCUSD  XNGUSD  XTIUSD  US2000  XAGUSD   split 3 ways
    ETHUSD                                   placed 831 MET in ONE order

The five that split had their own route-level override. ETHUSD had none
and was relying on the slave-level setting, which never reached the
adapter: entry_split_slices is read once, in the adapter's constructor,
at hub startup. PATCH /slaves/{id} live-updates engine.slave_config, so
a route's split takes effect immediately -- and the slave's is frozen at
whatever it was when the hub booted.

So the form saved cleanly, the card read "entries split into 3 over 5s",
and nothing split. A setting that saves and does nothing is the exact
failure this codebase keeps having to go back and gate, and this one was
built into the slave-level form itself.

The engine resolves it now, from the config that PATCH keeps current.
"""
import inspect

import pytest

from app.autonomous_copy_engine import AutonomousCopyEngine


SRC = inspect.getsource(AutonomousCopyEngine)


def _resolve(route_value, slave_value):
    """What the engine now passes for split_slices.

    Mirrors the expression under test rather than reaching into
    _open_one, which needs a broker, a quote and an account to get as far
    as building an OrderRequest.
    """
    return route_value if route_value is not None else slave_value


# ------------------------------------------------- the rule ------------

def test_the_engine_reads_the_slave_setting_from_the_live_config():
    """THE BUG. Not the adapter's constructor value, which is a snapshot
    of startup."""
    assert 'self.slave_config.get("entry_split_slices")' in SRC, (
        "the engine still leaves the slave-level split to the adapter's "
        "constructor, so it cannot take effect until the hub restarts"
    )
    assert 'self.slave_config.get("entry_split_interval")' in SRC


def test_a_route_still_wins_over_the_slave():
    assert _resolve(4, 3) == 4


def test_a_route_with_no_opinion_takes_the_slaves():
    """ETHUSD's case: no override, and the slave says 3."""
    assert _resolve(None, 3) == 3


def test_zero_on_a_route_is_an_opt_out_not_an_absence():
    """`is not None` rather than truthiness, or a route set to 0 would
    fall through to the slave's and split anyway -- the opposite of what
    it asked for."""
    assert _resolve(0, 3) == 0


def test_nothing_configured_anywhere_is_still_nothing():
    assert _resolve(None, None) is None


def test_the_choosing_is_done_on_is_not_none():
    """Pinned in the source, because truthiness here reads as correct
    and silently breaks the opt-out above."""
    assert "route.split_slices if route.split_slices is not None" in SRC
    assert "route.split_interval if route.split_interval is not None" in SRC
