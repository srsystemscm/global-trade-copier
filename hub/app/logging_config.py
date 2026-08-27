import logging
import logging.handlers
import sys

from app.config import settings


class AppOnlyFilter(logging.Filter):
    """Restricts a handler to our own code's log records -- third-party
    library noise (httpx's per-request INFO lines, etc.) still goes to the
    console/file handlers for troubleshooting, but doesn't get persisted.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name == "app" or record.name.startswith("app.")


class SQLiteLogHandler(logging.Handler):
    """Persists log records into the `logs` table for the Settings -> Logs
    UI. Never raises -- a log call must never be able to crash the caller,
    and the table may not exist yet on the very first few startup lines
    (this handler is wired up before init_db() runs).
    """

    def emit(self, record: logging.LogRecord) -> None:
        from app.db import insert_log  # deferred: avoids a circular import at module load time

        try:
            insert_log(record.created, record.levelname, record.name, self.format(record))
        except Exception:
            pass


def setup_logging() -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(settings.log_level)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_dir / "hub.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    db_handler = SQLiteLogHandler()
    db_handler.setFormatter(logging.Formatter("%(message)s"))
    db_handler.setLevel(logging.INFO)
    db_handler.addFilter(AppOnlyFilter())
    root.addHandler(db_handler)
