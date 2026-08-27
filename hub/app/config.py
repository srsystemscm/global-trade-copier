from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

HUB_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    http_port: int = 8000
    zmq_sub_port: int = 5555
    zmq_pull_port: int = 5557
    zmq_bind_host: str = "*"

    db_path: Path = HUB_ROOT / "data" / "tradecopier.db"
    log_dir: Path = HUB_ROOT / "logs"
    log_level: str = "INFO"

    # window used by the Signal Bus to forget old ticket+ts keys for dedup
    dedup_window_seconds: int = 600

    # Schwab OAuth2 + REST -- base URLs are overridable so tests can point
    # them at scripts/simulate_schwab.py instead of the real API.
    schwab_client_id: str = ""
    schwab_client_secret: str = ""
    schwab_redirect_uri: str = "https://127.0.0.1:8000/schwab/callback"
    schwab_auth_base: str = "https://api.schwabapi.com/v1/oauth"
    schwab_api_base: str = "https://api.schwabapi.com/trader/v1"
    schwab_market_base: str = "https://api.schwabapi.com/marketdata/v1"

    # Phase 5 hardening
    adapter_max_retries: int = 3
    adapter_retry_base_delay: float = 0.5

    watchdog_poll_interval_seconds: float = 15.0
    watchdog_stale_heartbeat_seconds: float = 30.0
    drawdown_alert_pct: float = 0.05

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""

    model_config = SettingsConfigDict(env_prefix="TC_", env_file=".env")


settings = Settings()
