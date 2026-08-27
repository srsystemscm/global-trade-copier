from app.adapters.base import (
    AccountSummary,
    AdapterConnectionError,
    AdapterError,
    AdapterRejectedError,
    AdapterTimeoutError,
    BrokerAdapter,
    OrderRequest,
    OrderResult,
)
from app.adapters.ibkr import IBKRAdapter
from app.adapters.mt4 import MT4Adapter
from app.adapters.schwab import SchwabAdapter
from app.config import settings
from app.schwab_auth import SchwabAuth
from app.slaves import SlaveConfig

__all__ = [
    "AccountSummary",
    "AdapterConnectionError",
    "AdapterError",
    "AdapterRejectedError",
    "AdapterTimeoutError",
    "BrokerAdapter",
    "OrderRequest",
    "OrderResult",
    "MT4Adapter",
    "SchwabAdapter",
    "IBKRAdapter",
    "create_adapter",
]


def create_adapter(slave: SlaveConfig) -> BrokerAdapter:
    if slave.broker_type == "mt4":
        return MT4Adapter(
            slave_id=slave.id,
            host=slave.config.get("host", "127.0.0.1"),
            port=slave.config["port"],
            timeout_ms=slave.config.get("timeout_ms", 5000),
        )

    if slave.broker_type == "schwab":
        auth = SchwabAuth(
            client_id=settings.schwab_client_id,
            client_secret=settings.schwab_client_secret,
            redirect_uri=settings.schwab_redirect_uri,
            auth_base=slave.config.get("schwab_auth_base", settings.schwab_auth_base),
        )
        return SchwabAdapter(
            slave_id=slave.id,
            auth=auth,
            futures_account_id=slave.config["futures_account_id"],
            brokerage_account_id=slave.config["brokerage_account_id"],
            api_base=slave.config.get("schwab_api_base", settings.schwab_api_base),
            market_base=slave.config.get("schwab_market_base", settings.schwab_market_base),
        )

    if slave.broker_type == "ibkr":
        return IBKRAdapter(
            slave_id=slave.id,
            host=slave.config.get("host", "127.0.0.1"),
            port=slave.config.get("port", 7497),  # TWS paper trading default; 7496 live, 4002/4001 for Gateway
            client_id=slave.config.get("client_id", 1),
        )

    raise ValueError(f"no adapter registered for broker_type={slave.broker_type!r}")
