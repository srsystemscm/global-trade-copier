from datetime import datetime, timezone

import app.risk_controls as risk_controls


def _fake_config(values):
    def get(key):
        return values.get(key)
    return get


def test_kill_switch_off_by_default(monkeypatch):
    monkeypatch.setattr(risk_controls, "get_config_value", _fake_config({}))
    assert risk_controls.is_kill_switch_active() is False


def test_kill_switch_on(monkeypatch):
    monkeypatch.setattr(risk_controls, "get_config_value", _fake_config({"kill_switch_enabled": "true"}))
    assert risk_controls.is_kill_switch_active() is True


def test_kill_switch_explicit_false(monkeypatch):
    monkeypatch.setattr(risk_controls, "get_config_value", _fake_config({"kill_switch_enabled": "false"}))
    assert risk_controls.is_kill_switch_active() is False


def test_trading_hours_disabled_always_true(monkeypatch):
    monkeypatch.setattr(risk_controls, "get_config_value", _fake_config({}))
    assert risk_controls.is_within_trading_hours(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)) is True


def test_trading_hours_normal_window_inside(monkeypatch):
    monkeypatch.setattr(
        risk_controls, "get_config_value",
        _fake_config({"trading_hours_enabled": "true", "trading_hours_start": "08:00", "trading_hours_end": "17:00"}),
    )
    assert risk_controls.is_within_trading_hours(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)) is True


def test_trading_hours_normal_window_outside(monkeypatch):
    monkeypatch.setattr(
        risk_controls, "get_config_value",
        _fake_config({"trading_hours_enabled": "true", "trading_hours_start": "08:00", "trading_hours_end": "17:00"}),
    )
    assert risk_controls.is_within_trading_hours(datetime(2026, 1, 1, 20, 0, tzinfo=timezone.utc)) is False


def test_trading_hours_midnight_wrap_inside(monkeypatch):
    # window 22:00 -> 06:00 (spans midnight)
    monkeypatch.setattr(
        risk_controls, "get_config_value",
        _fake_config({"trading_hours_enabled": "true", "trading_hours_start": "22:00", "trading_hours_end": "06:00"}),
    )
    assert risk_controls.is_within_trading_hours(datetime(2026, 1, 1, 23, 30, tzinfo=timezone.utc)) is True
    assert risk_controls.is_within_trading_hours(datetime(2026, 1, 2, 2, 0, tzinfo=timezone.utc)) is True


def test_trading_hours_midnight_wrap_outside(monkeypatch):
    monkeypatch.setattr(
        risk_controls, "get_config_value",
        _fake_config({"trading_hours_enabled": "true", "trading_hours_start": "22:00", "trading_hours_end": "06:00"}),
    )
    assert risk_controls.is_within_trading_hours(datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)) is False


def test_trading_hours_boundary_inclusive(monkeypatch):
    monkeypatch.setattr(
        risk_controls, "get_config_value",
        _fake_config({"trading_hours_enabled": "true", "trading_hours_start": "08:00", "trading_hours_end": "17:00"}),
    )
    assert risk_controls.is_within_trading_hours(datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)) is True
    assert risk_controls.is_within_trading_hours(datetime(2026, 1, 1, 17, 0, tzinfo=timezone.utc)) is True
