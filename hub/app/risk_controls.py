from datetime import datetime, timezone
from typing import Optional

from app.db import get_config_value


def is_kill_switch_active() -> bool:
    return get_config_value("kill_switch_enabled") == "true"


def is_within_trading_hours(now: Optional[datetime] = None) -> bool:
    """UTC HH:MM window, e.g. "22:00" -> "06:00" wraps past midnight.
    Disabled (the default) always returns True.
    """
    if get_config_value("trading_hours_enabled") != "true":
        return True

    start = get_config_value("trading_hours_start") or "00:00"
    end = get_config_value("trading_hours_end") or "23:59"
    current = (now or datetime.now(timezone.utc)).strftime("%H:%M")

    if start <= end:
        return start <= current <= end
    return current >= start or current <= end
