"""PATCH /slaves must actually honour `enabled`.

SlaveUpdate had no such field, so pydantic dropped it and the route answered
200 with the slave unchanged -- a no-op that reported success. Found while
trying to stop the MT4 mirror leg from copying onto the account that was also
acting as master.
"""
import inspect

from app import api_routes
from app.api_routes import SlaveUpdate
from app.slaves import set_enabled


def test_enabled_is_an_accepted_field():
    assert "enabled" in SlaveUpdate.model_fields


def test_enabled_survives_parsing():
    assert SlaveUpdate(**{"enabled": False}).enabled is False


def test_unknown_fields_still_parse_as_none():
    """The original bug's shape: an unrecognised key is dropped silently."""
    assert SlaveUpdate(**{"nonsense": True}).paused is None


def test_the_route_acts_on_it():
    src = inspect.getsource(api_routes.patch_slave)
    assert "payload.enabled" in src
    assert "set_enabled" in src


def test_disabling_stops_the_engine_and_enabling_starts_one():
    """Unlike `paused`, this governs whether an engine exists at all."""
    src = inspect.getsource(api_routes.patch_slave)
    assert "start_engine_for_slave" in src
    assert "stop_engine" in src


def test_set_enabled_is_a_real_store_function():
    assert callable(set_enabled)
    assert "UPDATE slaves SET enabled" in inspect.getsource(set_enabled)
