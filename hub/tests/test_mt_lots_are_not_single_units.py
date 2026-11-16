"""An MT lot is not one unit of quote currency, and sizing assumed it was.

2026-11-15, the daily test signal on XAUUSD:

    master  0.13 lots
    slave  13.00 lots      -- exactly 100x, exactly the contract size

compute_size falls back to `risk_per_unit = stop_distance` when it has no
ContractSpec, which is true for a share and wrong for every MT symbol
there is. get_contract_spec() carries listed futures only and returns
None for XAUUSD, XAGUSD, BTCUSD, SP500, EURUSD -- all of them. So a stop
149.45 wide was read as risking $149.45 a lot when IC Markets' XAUUSD is
100oz and it risks $14,944.70.

The sizes looked plausible, which is what let it run: 13 lots is not an
obviously absurd number, and the dollar risk the engine reported was
internally consistent. It was wrong by the contract size in every case,
and on a forex pair it would be wrong by 100,000.

THE FIGURES WERE ALREADY IN THE HUB. The bridge reports contract_size,
tick_size and tick_value per symbol, and MT4Adapter.get_symbol_spec has
returned them for a while. Nothing asked.

Taken from the BROKER rather than a table, because a CFD's contract size
is the broker's choice -- the same XAUUSD is 100oz at one and 10oz at
another -- and a table would be a second place to be wrong.
"""
import asyncio

import pytest

from app.adapters.base import AdapterError
from app.autonomous_copy_engine import AutonomousCopyEngine
from app.contract_specs import ContractSpec, get_contract_spec
from app.sizing import compute_size


# ---------------------------------------------- the bug, in arithmetic --

XAUUSD = ContractSpec(symbol="XAUUSD", tick_size=0.01, tick_value=1.0,
                      point_value=100.0, min_size=0.01)
SIZING = {"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance",
          "max_risk_pct": 0.025, "rounding": "nearest_min1"}


def _size(spec, balance=194350.0):
    entry, sl = 3874.38, 3724.932952041141   # the real D-run figures
    return compute_size(sizing_config=SIZING, master_lots=0.13,
                        entry_price=entry, stop_price=sl,
                        account_balance=balance, account_equity=balance,
                        contract_spec=spec, risk_scale=1.0)


def test_without_a_spec_the_size_is_a_hundred_times_too_large():
    """Pinned so the failure mode is documented rather than remembered.
    This is what shipped, and why 13.00 lots came back for 0.13."""
    assert _size(None).size == pytest.approx(13.0)


def test_with_the_brokers_spec_it_matches_the_master():
    """0.13 lots, which is what the master staked. The agreement is a
    coincidence of the two accounts being similarly sized -- the point is
    the 100x, not the match."""
    assert _size(XAUUSD).size == pytest.approx(0.13)


def test_every_mt_symbol_is_missing_from_the_static_registry():
    """The registry is for listed futures. If one of these ever gains an
    entry, the broker lookup stops being reached for it -- worth knowing
    rather than discovering."""
    for symbol in ("XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD", "SP500",
                   "US500", "USTEC", "EURUSD"):
        assert get_contract_spec(symbol) is None, symbol


def test_the_listed_futures_are_still_served_by_the_registry():
    """MGC and GC must not start coming from a broker lookup: the
    registry carries the exchange and the volume months too."""
    for symbol in ("GC", "MGC", "MES", "M2K"):
        assert get_contract_spec(symbol) is not None, symbol


# ---------------------------------------------- the lookup -------------

class _BrokerSpec:
    def __init__(self, tick_size=0.01, tick_value=1.0, volume_min=0.01,
                 contract_size=100.0):
        self.tick_size = tick_size
        self.tick_value = tick_value
        self.volume_min = volume_min
        self.contract_size = contract_size


class _Adapter:
    def __init__(self, spec=None, raises=None):
        self._spec = spec
        self._raises = raises
        self.asked = []

    async def get_symbol_spec(self, symbol):
        self.asked.append(symbol)
        if self._raises:
            raise self._raises
        return self._spec


def _engine(adapter):
    e = object.__new__(AutonomousCopyEngine)
    e.slave_id = "mt5-icm-1"
    e.adapter = adapter
    e._broker_specs = {}
    return e


def _get(engine, symbol="XAUUSD"):
    return asyncio.run(engine._broker_contract_spec(symbol))


def test_it_builds_a_spec_from_what_the_broker_reports():
    spec = _get(_engine(_Adapter(_BrokerSpec())))
    assert spec.tick_size == 0.01
    assert spec.tick_value == 1.0
    assert spec.min_size == 0.01, "rounding must happen in the broker's steps"
    assert spec.point_value == pytest.approx(100.0)


def test_the_contract_size_is_not_multiplied_in_as_well():
    """MT's tick value is ALREADY per one lot. Multiplying by
    contract_size too would reintroduce the same error in the other
    direction, and the result would look just as plausible."""
    spec = _get(_engine(_Adapter(_BrokerSpec(contract_size=100.0))))
    assert spec.tick_value == 1.0, "contract_size leaked into tick_value"
    assert _size(spec).size == pytest.approx(0.13)


def test_it_asks_once_per_symbol():
    """The SYMBOLS round trip is on the signal path; a contract size does
    not change under a running hub."""
    a = _Adapter(_BrokerSpec())
    e = _engine(a)
    for _ in range(4):
        _get(e)
    assert a.asked == ["XAUUSD"]


def test_an_adapter_that_cannot_answer_leaves_behaviour_unchanged():
    """IBKR has no get_symbol_spec; it must not start failing because MT
    gained one."""
    class _NoSpec:
        pass
    assert _get(_engine(_NoSpec())) is None


def test_a_broker_error_falls_back_rather_than_refusing_the_signal():
    assert _get(_engine(_Adapter(raises=AdapterError("EA too old")))) is None


def test_a_failed_lookup_is_not_retried_on_every_signal():
    a = _Adapter(raises=AdapterError("EA too old"))
    e = _engine(a)
    for _ in range(3):
        _get(e)
    assert a.asked == ["XAUUSD"], "a dead lookup was repeated on the signal path"


@pytest.mark.parametrize("bad", [
    _BrokerSpec(tick_size=0.0),
    _BrokerSpec(tick_value=0.0),
    _BrokerSpec(tick_size=-1.0),
])
def test_an_unusable_figure_is_refused_rather_than_defaulted(bad):
    """A zero divides by zero or sizes infinitely; a made-up 1.0 is the
    bug this exists to fix wearing a different hat. Neither is better
    than falling back and saying so."""
    assert _get(_engine(_Adapter(bad))) is None


# ---------------------------------------------- how it is wired --------

def test_the_engine_reaches_for_it_only_when_the_registry_is_silent():
    import inspect
    src = inspect.getsource(AutonomousCopyEngine._size_candidate)
    assert "spec = get_contract_spec(target_symbol)" in src
    assert "if spec is None:" in src
    assert "await self._broker_contract_spec(target_symbol)" in src


def test_a_broker_spec_does_not_make_the_slave_a_futures_account():
    """account_type decides which account is sized against. The static
    registry is what says 'listed future'; an MT symbol having a contract
    size must not reroute the balance lookup."""
    import inspect
    src = inspect.getsource(AutonomousCopyEngine._size_candidate)
    decided = src.index('account_type = "futures" if spec else "brokerage"')
    fetched = src.index("await self._broker_contract_spec(target_symbol)")
    assert decided < fetched, (
        "account_type is derived AFTER the broker spec is fetched, so an "
        "MT symbol would be sized against a futures account"
    )
    assert src.count('account_type = "futures"') == 1
