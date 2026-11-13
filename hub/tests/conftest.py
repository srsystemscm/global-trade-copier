import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402  (must follow the sys.path insert)

from app import db  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Give every test its own empty database.

    Without this the suite wrote into hub/data/tradecopier.db -- the live
    registry the running hub reconciles against. A stray `s1` row left at
    OPEN by an engine test was found sitting in it, which is exactly the
    shape of row reconciliation treats as a real open position.

    Patching `settings.db_path` is enough because db.get_connection() reads
    it per call rather than holding a connection open.
    """
    monkeypatch.setattr(settings, "db_path", tmp_path / "tradecopier.db")
    db.init_db()
    yield
