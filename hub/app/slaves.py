import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config import HUB_ROOT
from app.db import db_cursor

logger = logging.getLogger(__name__)

SEED_FILE = HUB_ROOT / "config" / "slaves.json"


@dataclass
class SlaveConfig:
    id: str
    name: str
    broker_type: str
    enabled: bool
    paused: bool
    config: Dict[str, Any]


def _row_to_slave(row) -> SlaveConfig:
    return SlaveConfig(
        id=row["id"],
        name=row["name"],
        broker_type=row["broker_type"],
        enabled=bool(row["enabled"]),
        paused=bool(row["paused"]),
        config=json.loads(row["config_json"]),
    )


def seed_slaves_if_empty() -> None:
    """Loads hub/config/slaves.json into the slaves table on first run.

    The Phase 4 connection wizard is now the normal way to add slaves; this
    only matters for a brand new, empty database.
    """
    with db_cursor() as cur:
        count = cur.execute("SELECT COUNT(*) AS n FROM slaves").fetchone()["n"]
        if count > 0 or not SEED_FILE.exists():
            return

        seed = json.loads(SEED_FILE.read_text())
        for entry in seed:
            cur.execute(
                """
                INSERT INTO slaves (id, name, broker_type, enabled, paused, config_json, created_at)
                VALUES (?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    entry["id"],
                    entry["name"],
                    entry["broker_type"],
                    int(entry.get("enabled", True)),
                    json.dumps(entry.get("config", {})),
                    time.time(),
                ),
            )
        logger.info("seeded %d slave(s) from %s", len(seed), SEED_FILE)


def load_enabled_slaves() -> List[SlaveConfig]:
    with db_cursor() as cur:
        rows = cur.execute("SELECT * FROM slaves WHERE enabled = 1").fetchall()
    return [_row_to_slave(row) for row in rows]


def list_all_slaves() -> List[SlaveConfig]:
    with db_cursor() as cur:
        rows = cur.execute("SELECT * FROM slaves ORDER BY created_at").fetchall()
    return [_row_to_slave(row) for row in rows]


def get_slave(slave_id: str) -> Optional[SlaveConfig]:
    with db_cursor() as cur:
        row = cur.execute("SELECT * FROM slaves WHERE id = ?", (slave_id,)).fetchone()
    return _row_to_slave(row) if row else None


def insert_slave(
    slave_id: str, name: str, broker_type: str, config: Dict[str, Any], enabled: bool = True
) -> SlaveConfig:
    with db_cursor() as cur:
        cur.execute(
            """
            INSERT INTO slaves (id, name, broker_type, enabled, paused, config_json, created_at)
            VALUES (?, ?, ?, ?, 0, ?, ?)
            """,
            (slave_id, name, broker_type, int(enabled), json.dumps(config), time.time()),
        )
    return get_slave(slave_id)


def update_slave(
    slave_id: str, name: Optional[str] = None, config: Optional[Dict[str, Any]] = None
) -> Optional[SlaveConfig]:
    existing = get_slave(slave_id)
    if existing is None:
        return None
    with db_cursor() as cur:
        cur.execute(
            "UPDATE slaves SET name = ?, config_json = ? WHERE id = ?",
            (
                name if name is not None else existing.name,
                json.dumps(config) if config is not None else json.dumps(existing.config),
                slave_id,
            ),
        )
    return get_slave(slave_id)


def set_paused(slave_id: str, paused: bool) -> None:
    with db_cursor() as cur:
        cur.execute("UPDATE slaves SET paused = ? WHERE id = ?", (int(paused), slave_id))


def delete_slave(slave_id: str) -> None:
    with db_cursor() as cur:
        cur.execute("DELETE FROM slaves WHERE id = ?", (slave_id,))
