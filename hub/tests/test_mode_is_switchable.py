"""Mirror and autonomous have to be selectable, not baked in.

Asked for on 2026-11-15: "mirror and autonomous absolutely need to be
selectable and not baked in... I want to test the switching function
now."

They were set in the Add Slave wizard and never again. engine_manager
reads the value once, at start, to choose CopyEngine or
AutonomousCopyEngine -- so the switch is not a config edit, it is
stopping one engine and starting another.

Which is why it is refused while the slave holds positions.
position_store is keyed by SLAVE, not by engine, so the incoming engine's
restore() genuinely would pick these up and begin managing them: mirror
aims at the master's own stop level, autonomous trails its own from ATR.
A position opened under one and adopted by the other is protected by
rules it was never opened under, and nothing on any screen would say so.
"""
import inspect

import pytest
from fastapi import HTTPException

from app import api_routes
from app.api_routes import (SlaveUpdate, _mode_switch,
                            _refuse_switch_while_holding, held_position_count)


class _Slave:
    def __init__(self, config):
        self.config = config


class _Engine:
    def __init__(self, positions):
        self._positions = positions


# ----------------------------------------------- what counts as a switch --

def test_changing_the_mode_is_recognised():
    assert _mode_switch(_Slave({"mode": "mirror"}),
                        {"mode": "autonomous"}) == ("mirror", "autonomous")
    assert _mode_switch(_Slave({"mode": "autonomous"}),
                        {"mode": "mirror"}) == ("autonomous", "mirror")


def test_saving_the_same_mode_is_not_a_switch():
    """Every other edit on that form sends the whole config back, mode
    included. Restarting the engine on an unrelated change would make
    editing a risk setting interrupt copying."""
    assert _mode_switch(_Slave({"mode": "mirror"}), {"mode": "mirror"}) is None
    assert _mode_switch(_Slave({"mode": "autonomous"}),
                        {"mode": "autonomous"}) is None


def test_an_edit_that_does_not_mention_the_mode_is_not_a_switch():
    assert _mode_switch(_Slave({"mode": "mirror"}), {"risk_scale": 0.5}) is None
    assert _mode_switch(_Slave({"mode": "mirror"}), {}) is None
    assert _mode_switch(_Slave({"mode": "mirror"}), None) is None


def test_a_slave_with_no_mode_set_counts_as_mirror():
    """engine_manager defaults to mirror, so this has to agree -- or the
    switch to autonomous would look like a no-op and never restart."""
    assert _mode_switch(_Slave({}), {"mode": "autonomous"}) == ("mirror", "autonomous")
    assert _mode_switch(_Slave({}), {"mode": "mirror"}) is None


def test_a_mode_that_is_not_a_mode_is_refused_rather_than_stored():
    """It chooses the engine. A typo stored here means engine_manager
    falls through to mirror, and the slave quietly runs the other one."""
    with pytest.raises(HTTPException) as got:
        _mode_switch(_Slave({"mode": "mirror"}), {"mode": "atonomous"})
    assert got.value.status_code == 400


# ------------------------------------------- the guard on open positions --

def test_switching_is_refused_while_the_slave_holds_positions():
    with pytest.raises(HTTPException) as got:
        _refuse_switch_while_holding(
            "ibkr-paper-1", _Engine({("a", 1, "MGC"): {}, ("a", 2, "MES"): {}}),
            ("autonomous", "mirror"))
    assert got.value.status_code == 409
    detail = got.value.detail
    assert "2 position(s)" in detail, "the count is the actionable part"
    assert "autonomous" in detail and "mirror" in detail


def test_a_flat_slave_switches():
    assert _refuse_switch_while_holding(
        "ibkr-paper-1", _Engine({}), ("mirror", "autonomous")) is None


def test_a_slave_with_no_engine_switches():
    """Disabled, or the hub has not started it. Nothing to protect."""
    assert _refuse_switch_while_holding(
        "ibkr-paper-1", None, ("mirror", "autonomous")) is None


def test_the_ui_flag_and_the_api_refusal_read_the_same_count():
    """They disagreed once by construction -- the summary counted
    positions one way and the guard another. One function now."""
    engine = _Engine({("a", 1, "MGC"): {}})
    assert held_position_count(engine) == 1
    assert held_position_count(_Engine({})) == 0
    assert held_position_count(None) == 0
    src = inspect.getsource(api_routes._slave_summary)
    assert "held_position_count" in src, (
        "the summary counts positions its own way again, so the control can "
        "be enabled on a slave the API will refuse"
    )


# -------------------------------------------------- the route does it ----

def test_the_route_restarts_the_engine_on_a_switch():
    src = inspect.getsource(api_routes.patch_slave)
    assert "_mode_switch" in src
    assert "_refuse_switch_while_holding" in src
    assert "stop_engine" in src and "start_engine_for_slave" in src


def test_the_config_is_written_before_the_engine_restarts():
    """start_engine_for_slave reads the mode from the STORED config, so a
    restart before the write would start the engine that was already
    running and report success."""
    src = inspect.getsource(api_routes.patch_slave)
    assert src.index("update_slave(") < src.index("await stop_engine")


def test_a_disabled_slave_is_not_started_by_a_switch():
    """Changing how a stopped slave would copy must not start it."""
    src = inspect.getsource(api_routes.patch_slave)
    after = src[src.index("if switching is not None:", src.index("set_magic_filter")):]
    assert "slave.enabled" in after[:900]


def test_mode_travels_in_config_rather_than_its_own_field():
    """It lives in config, so SlaveUpdate needs nothing new -- and the
    wizard's existing shape keeps working."""
    assert "config" in SlaveUpdate.model_fields
    assert SlaveUpdate(**{"config": {"mode": "autonomous"}}).config["mode"] == "autonomous"
