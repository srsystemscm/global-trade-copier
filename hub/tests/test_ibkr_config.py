"""IBKR slave config -> IBKRAdapter wiring.

These are config-plumbing tests, not API tests: they need no ib_async and no
Gateway. They exist because every one of these fields is only exercised at the
moment a real order is placed, which is the worst time to discover a typo.
"""
import pytest

from app.adapters import create_adapter
from app.adapters.ibkr import IBKRAdapter
from app.slaves import SlaveConfig


def _slave(**config):
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002, "client_id": 7}
    base.update(config)
    return SlaveConfig(id="ibkr-1", name="IBKR", broker_type="ibkr",
                       enabled=True, paused=False, config=base)


def test_endpoint_and_client_id_pass_through():
    a = create_adapter(_slave())
    assert isinstance(a, IBKRAdapter)
    assert (a.host, a.port, a.client_id) == ("127.0.0.1", 4002, 7)


def test_market_data_type_passes_through():
    """A fresh IBKR account has no market data subscriptions, so live data
    returns NaN and get_quote() fails. 3 = delayed, which is free."""
    assert create_adapter(_slave(market_data_type=3)).market_data_type == 3


def test_market_data_type_defaults_to_none():
    """None means 'don't call reqMarketDataType', leaving IBKR's own default,
    so an account with real subscriptions is unaffected."""
    assert create_adapter(_slave()).market_data_type is None


def test_quote_timeout_defaults_and_overrides():
    assert create_adapter(_slave()).quote_timeout == 1.0
    assert create_adapter(_slave(quote_timeout=3.0)).quote_timeout == 3.0


def test_port_default_is_tws_paper():
    s = _slave()
    del s.config["port"]
    assert create_adapter(s).port == 7497


def test_constructing_the_adapter_does_not_import_ib_async():
    """ib_async is lazy-imported so non-IBKR deployments don't need it.
    Constructing must not touch it; only _require_ib() resolves the module
    (and here, where ib_async IS installed, it returns it)."""
    import sys

    before = "ib_async" in sys.modules
    a = create_adapter(_slave())
    assert ("ib_async" in sys.modules) == before, "constructing imported ib_async"

    mod = a._require_ib()
    assert hasattr(mod, "IB") and hasattr(mod, "ContFuture")
