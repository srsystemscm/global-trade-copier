"""A held ETF could not be restored, so it could never be closed.

`_contract_by_id` rebuilds the adapter's handle on a contract from its
id. It built one shape and one only:

    ib_mod.Future(conId=contract_id, exchange=get_contract_spec(symbol).exchange)

`get_contract_spec` returns None for anything that is not a listed
future -- its docstring says so, and says to treat those as equities --
so on an ETF this raised "'NoneType' object has no attribute 'exchange'"
before it ever reached IBKR.

It is reached from restore_position, which runs at startup to give the
adapter back its handle on positions the hub already held. So the
failure is restart-only, and on ibkr-etf-1 -- a slave whose whole book
is ETFs -- it meant that after ANY restart no held position could be
restored, and therefore none could be closed. Every CLOSE answered "no
tracked IBKR position for slave_ticket=N", forever.

Measured on 2026-11-18: ETHT 1485 and UGL 1256 both stranded, ETHT for
twenty-four hours and 1,047 attempts across five restarts, with IBKR
holding all 789 shares throughout.

    19:37:04 ERROR could not restore adapter state for ETHT
                   slave_ticket=1485: 'NoneType' object has no attribute
                   'exchange'

These tests are about which contract is BUILT, not about IBKR: the
failure happened on this side of the wire.
"""
import pytest


class _Future:
    def __init__(self, conId=None, exchange=None, **kw):
        self.conId = conId
        self.exchange = exchange
        self.secType = "FUT"


class _Stock:
    def __init__(self, conId=None, symbol=None, exchange=None, currency=None, **kw):
        self.conId = conId
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency
        self.secType = "STK"


class _IbModule:
    Future = _Future
    Stock = _Stock


class _Ib:
    def __init__(self):
        self.asked = []

    async def qualifyContractsAsync(self, probe):
        self.asked.append(probe)
        return [probe]


def adapter():
    from app.adapters.ibkr import IBKRAdapter

    a = IBKRAdapter.__new__(IBKRAdapter)
    a.slave_id = "ibkr-etf-1"
    a.ib = _Ib()
    a._require_ib = lambda: _IbModule
    return a


def resolve(a, symbol, contract_id=788426882):
    import asyncio

    return asyncio.run(a._contract_by_id(symbol, contract_id))


# --- the defect ------------------------------------------------------

@pytest.mark.parametrize("symbol", ["ETHT", "UGL", "TQQQ", "SPXL", "BOIL"])
def test_an_etf_is_rebuilt_as_a_stock(symbol):
    """The five ETFs ibkr-etf-1 was holding when this was found. Each one
    has no futures spec, so each one used to raise."""
    a = adapter()
    built = resolve(a, symbol)
    assert built.secType == "STK", (
        f"{symbol} was rebuilt as a {built.secType} -- get_contract_spec "
        f"returns None for it, which means equity, not future"
    )
    assert built.conId == 788426882


def test_the_id_is_what_identifies_it_not_the_symbol():
    """An ETF's conId is the whole point of this path: the position was
    opened against one specific contract and must be rebuilt against the
    same one, not re-derived from a name."""
    a = adapter()
    assert resolve(a, "ETHT", contract_id=12345).conId == 12345


# --- and the futures it was written for, unchanged -------------------

@pytest.mark.parametrize("symbol", ["MGC", "MBT", "M2K"])
def test_a_future_is_still_rebuilt_as_a_future(symbol):
    """The guard against over-correcting. This path exists for the
    contract-roll destination, and those are futures with a real spec."""
    from app.contract_specs import get_contract_spec

    spec = get_contract_spec(symbol)
    assert spec is not None, f"{symbol} should have a futures spec"

    a = adapter()
    built = resolve(a, symbol)
    assert built.secType == "FUT"
    assert built.exchange == spec.exchange


def test_a_contract_ibkr_will_not_qualify_is_still_refused():
    """Unchanged, and the reason _qualified_only exists: ib_async returns
    a list with None in it rather than an empty one."""
    from app.adapters.base import AdapterRejectedError

    class _Refuses(_Ib):
        async def qualifyContractsAsync(self, probe):
            return [None]

    a = adapter()
    a.ib = _Refuses()
    with pytest.raises(AdapterRejectedError, match="could not qualify"):
        resolve(a, "ETHT")
