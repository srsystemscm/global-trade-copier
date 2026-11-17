"""The splitter has a control, and only where it does something.

Chris: "I need access to adjusting the order splitting. both functions
(splits and delays) per slave."

The gate matters as much as the fields. `split_slices` and
`split_interval` travel on every OrderRequest and only the IBKR adapter
acts on them, so a control offered on an MT slave would save cleanly and
change nothing -- which is the exact failure this page has had to go
back and fix twice before, on sizing and on the contract roll.
"""
from app.adapters.ibkr import IBKRAdapter
from app.adapters.mt4 import MT4Adapter


def test_only_the_adapter_that_can_split_says_it_can():
    assert IBKRAdapter.supports_order_split is True
    assert getattr(MT4Adapter, "supports_order_split", False) is False


def test_the_summary_reports_the_capability():
    """None when no engine is running -- "not known yet", which is not
    the same as "no", and the page must not render the second for the
    first."""
    import inspect

    from app import api_routes

    src = inspect.getsource(api_routes._slave_summary)
    assert '"supports_order_split"' in src
    assert "if engine is not None else None" in src, (
        "a slave with no engine would report False, which reads as "
        "'this broker cannot split' rather than 'not known yet'"
    )
