"""The suite must not write into the copier's own state.

Two leaks of this shape have been found here. The first was the database:
a stray `s1` row left at OPEN by an engine test was found sitting in the
live registry, which is exactly the shape reconciliation treats as a real
open position. `conftest._isolated_db` fixed that.

The second was logging. TC_LOG_DIR moved the log to C:\\GTC\\logs, and the
suite inherited it -- so stub-hub runs appeared as `copier_id=gtc-test`
interleaved with the real copier's lines. A log nobody can trust at the
moment they are reading it to diagnose something real.

They leaked for different reasons, and the second is why this file exists
rather than a comment. `db.get_connection()` reads `settings.db_path` on
every call, so a fixture can redirect it. `setup_logging()` runs ONCE, at
`app.main` import -- during collection, before any fixture. The redirect
has to happen at conftest module scope, and nothing about a fixture-based
habit would have caught that.
"""
import logging
from pathlib import Path

from app.config import settings


def _live_paths():
    """Where a developer's own copier keeps its state, if configured."""
    out = []
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TC_LOG_DIR=") or line.startswith("TC_DB_PATH="):
                out.append(Path(line.split("=", 1)[1].strip()))
    return out


def test_logging_does_not_write_where_the_running_copier_does():
    live = [p for p in _live_paths()]
    if not live:
        # No .env here (CI). Nothing to collide with, and asserting on a
        # path that does not exist would pass for the wrong reason.
        return

    log_dir = Path(settings.log_dir).resolve()
    for configured in live:
        target = configured if configured.suffix else configured
        assert log_dir != target.resolve(), (
            f"the suite is logging into {log_dir}, which is the running "
            f"copier's own log directory"
        )


def test_the_file_handler_points_at_the_test_directory():
    """Not just the setting -- the handler that was actually installed.

    setup_logging() runs at import, so a redirect applied too late would
    leave the setting correct and the handler still open on the live file.
    That is precisely the failure this guards, and checking the setting
    alone would not see it.
    """
    # pytest's own logging plugin installs a FileHandler on the null
    # device -- the Windows spelling of it, or /dev/null elsewhere. It
    # writes nowhere, so it is not a leak, and counting it as one made
    # this test fail against a fix that was working.
    def _is_null(path: str) -> bool:
        tail = path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
        return tail.lower() in {"nul", "null"}

    handlers = [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler) and not _is_null(h.baseFilename)
    ]
    if not handlers:
        return  # app.main was never imported in this run; nothing installed

    test_dir = Path(settings.log_dir).resolve()
    for h in handlers:
        assert Path(h.baseFilename).resolve().parent == test_dir, (
            f"a file handler is open on {h.baseFilename}, outside the "
            f"test log directory {test_dir}"
        )


def test_the_database_is_a_temporary_one():
    """The fixture that already exists, pinned so it cannot quietly lapse."""
    db_path = Path(settings.db_path).resolve()
    for configured in _live_paths():
        if configured.suffix == ".db":
            assert db_path != configured.resolve(), (
                f"the suite is using {db_path}, the live registry"
            )
