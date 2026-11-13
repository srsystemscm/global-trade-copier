"""The reporter's configuration, and the one rule it must never break.

A copier with no key configured has to behave exactly as one built before
the reporter existed. That is the most important property of the whole
feature: the copier's job is to copy trades, and nothing about reporting
on it may put that at risk. So `load()` never raises -- a missing file, a
malformed one, an unreadable key -- it logs and returns a disabled
configuration, which starts nothing at all.
"""
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from app.config import HUB_ROOT

logger = logging.getLogger(__name__)

CONFIG_FILE = HUB_ROOT / "config" / "hub_reporter.json"

# The environment variable the Operations hub's own reporters use for their
# key (ops_hub docs/payload-v1.md). Same name here deliberately: one copier
# on one host reports as one identity, and two names for one secret is how
# a key ends up configured in the place that is not being read.
KEY_ENV = "ODH_COPIER_KEY"

INGEST_PATH = "/api/v1/ingest/copier"

SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class HubReporterConfig:
    """See docs/hub-reporter.md, "Configuration"."""

    enabled: bool = False
    url: str = ""
    copier_id: str = "gtc"
    key_file: str = "config/hub_key.txt"
    snapshot_interval_s: float = 15.0
    timeout_s: float = 5.0
    queue_max: int = 10_000
    max_events_per_report: int = 200
    # One signal's legs land over a second or two -- the fan-out is
    # sequential and a deferred leg later still. Waiting that long after the
    # first of them means a signal and its copies travel in one report
    # instead of three, which is also how the hub sees them in the tab.
    debounce_s: float = 1.0

    # Never from the config file, never logged, never in a payload.
    key: str = ""

    @property
    def active(self) -> bool:
        """Switched on, pointed somewhere, and holding a key.

        All three, because `enabled: true` with no key would mean a sender
        task posting unauthenticated reports into a 401 loop, and that is a
        worse failure than being off.
        """
        return bool(self.enabled and self.url and self.key)

    @property
    def endpoint(self) -> str:
        return self.url.rstrip("/") + INGEST_PATH


def _read_key(key_file: str) -> str:
    """The key, from the environment first and a file second.

    The environment wins because that is how a deployed service is given a
    secret without one sitting in the repository's working tree.
    """
    from_env = (os.environ.get(KEY_ENV) or "").strip()
    if from_env:
        return from_env

    if not key_file:
        return ""
    path = Path(key_file)
    if not path.is_absolute():
        path = HUB_ROOT / path
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        # Not an error. No key is the default state, and the copier is
        # expected to run in it indefinitely.
        logger.debug("hub reporter: no key at %s and %s is unset", path, KEY_ENV)
        return ""


def _block(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    block = raw.get("hub_reporter", raw)
    return block if isinstance(block, dict) else {}


def load(path: Optional[Path] = None) -> HubReporterConfig:
    """Read `config/hub_reporter.json`, or return a disabled default.

    Deliberately total: every failure path ends in a configuration whose
    `active` is False, so a typo in a JSON file can never be the reason a
    copier does not start.
    """
    path = CONFIG_FILE if path is None else path
    block: Dict[str, Any] = {}
    if path.exists():
        try:
            block = _block(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "hub reporter: %s could not be read (%s) -- reporting is OFF, "
                "copying is unaffected", path, exc,
            )
            return HubReporterConfig()

    defaults = HubReporterConfig()
    try:
        cfg = HubReporterConfig(
            enabled=bool(block.get("enabled", defaults.enabled)),
            url=str(block.get("url", defaults.url) or ""),
            copier_id=str(block.get("copier_id", defaults.copier_id) or defaults.copier_id),
            key_file=str(block.get("key_file", defaults.key_file) or ""),
            snapshot_interval_s=float(
                block.get("snapshot_interval_s", defaults.snapshot_interval_s)),
            timeout_s=float(block.get("timeout_s", defaults.timeout_s)),
            queue_max=int(block.get("queue_max", defaults.queue_max)),
            max_events_per_report=int(
                block.get("max_events_per_report", defaults.max_events_per_report)),
            debounce_s=float(block.get("debounce_s", defaults.debounce_s)),
            key="",
        )
    except (TypeError, ValueError) as exc:
        logger.warning(
            "hub reporter: %s has a bad value (%s) -- reporting is OFF, copying "
            "is unaffected", path, exc,
        )
        return HubReporterConfig()

    # Read last and attached by replacement, so no code path can log `cfg`
    # before the key is in it and none can log it afterwards without this
    # being the only place that knows it came from.
    from dataclasses import replace
    return replace(cfg, key=_read_key(cfg.key_file))
