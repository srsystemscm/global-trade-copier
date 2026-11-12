"""How much of the master's risk each leg carries.

risk_scale 1.0 carries the configured risk unchanged, 0.5 halves the
position. Settable per slave and overridable per strategy (route).

What makes the number mean what it says: stops come from ATR multiples and
never from size, so dollar risk scales linearly with it. Halving the size
really does halve the risk carried across -- which is only true as long as
nothing downstream re-derives a stop from the size.
"""
import asyncio

import pytest

from app.contract_specs import get_contract_spec
from app.copy_engine import CopyEngine
from app.models import Signal
from app.routing import resolve_routes
from app.sizing import compute_size, resolve_risk_scale
from tests.conftest_fakes import FakeBroker

ACCT = "ICM-44916920"


def _size(scale, **cfg):
    base = {"mode": "dollar_notional", "notional": 10000, "rounding": "floor"}
    base.update(cfg)
    return compute_size(
        sizing_config=base, master_lots=1.0, entry_price=100.0, stop_price=90.0,
        account_balance=100000, account_equity=100000, contract_spec=None,
        risk_scale=scale,
    )


# ---------------- the dial itself ----------------------------------------

def test_one_carries_the_configured_size_unchanged():
    assert _size(1.0).size == 100       # 10000 / 100
    assert _size(1.0).size == _size(None or 1.0).size


def test_a_half_halves_the_position():
    assert _size(0.5).size == 50


def test_it_scales_up_as_well_as_down():
    assert _size(2.0).size == 200


def test_the_default_changes_nothing():
    """An absent setting must never quietly resize anything."""
    base = {"mode": "dollar_notional", "notional": 10000, "rounding": "floor"}
    unscaled = compute_size(
        sizing_config=base, master_lots=1.0, entry_price=100.0, stop_price=90.0,
        account_balance=100000, account_equity=100000, contract_spec=None,
    )
    assert unscaled.size == _size(1.0).size


@pytest.mark.parametrize("bad", [0.0, -0.5, -1])
def test_a_non_positive_scale_is_refused_not_silently_zeroed(bad):
    """A zero would send a zero-size order and a negative would invert the
    side -- a short where the master is long."""
    r = _size(bad)
    assert r.skipped
    assert r.size == 0
    assert "risk_scale" in r.reason


def test_it_applies_to_every_mode():
    for cfg in (
        {"mode": "dollar_notional", "notional": 10000, "rounding": "floor"},
        {"mode": "lot_multiplier", "multiplier": 100, "rounding": "floor"},
        {"mode": "pct_risk", "risk_pct": 0.01, "rounding": "floor"},
    ):
        full = _size(1.0, **cfg).size
        half = _size(0.5, **cfg).size
        assert half == pytest.approx(full / 2, rel=0.02), cfg["mode"]


# ---------------- interaction with the rounding and the ceiling ----------

def test_scaling_down_can_bring_a_trade_under_the_risk_ceiling():
    """The ceiling is checked on the FINAL size, so the dial is also a way to
    make an otherwise-refused trade tradeable."""
    cfg = {"mode": "fixed_contracts", "contracts": 10,
           "rounding": "floor", "max_risk_pct": 0.01}
    spec = get_contract_spec("MGC")
    # MGC is $10/point, so a 120-point stop risks $1200 per contract.
    # A $200k account's 1% ceiling is $2000: ten contracts is far over it,
    # one is comfortably under.
    common = dict(sizing_config=cfg, master_lots=1.0, entry_price=4000.0,
                  stop_price=3880.0, account_balance=200000,
                  account_equity=200000, contract_spec=spec)
    assert compute_size(**common, risk_scale=1.0).skipped       # 10 x $1200
    assert not compute_size(**common, risk_scale=0.1).skipped   # 1 x $1200


def test_a_single_contract_cannot_be_fractionally_scaled():
    """The trap worth knowing: under fixed_contracts 1 with nearest_min1,
    0.5 rounds straight back to 1 and the dial does nothing. Futures come in
    whole contracts, so sizing down means a smaller contract (MGC for GC),
    not a smaller number."""
    cfg = {"mode": "fixed_contracts", "contracts": 1, "rounding": "nearest_min1"}
    spec = get_contract_spec("MGC")
    common = dict(sizing_config=cfg, master_lots=1.0, entry_price=4000.0,
                  stop_price=3900.0, account_balance=1000000,
                  account_equity=1000000, contract_spec=spec)
    assert compute_size(**common, risk_scale=1.0).size == 1
    assert compute_size(**common, risk_scale=0.5).size == 1

    # floor is the honest alternative: it skips rather than quietly rounding
    # back up to full size.
    floored = dict(common)
    floored["sizing_config"] = dict(cfg, rounding="floor")
    assert compute_size(**floored, risk_scale=0.5).skipped


# ---------------- where it is read from ----------------------------------

def test_the_route_overrides_the_slave():
    """A slave can run at half size overall while one strategy on it runs at
    a quarter."""
    assert resolve_risk_scale({"risk_scale": 0.5}, None) == 0.5
    assert resolve_risk_scale({"risk_scale": 0.5}, 0.25) == 0.25
    assert resolve_risk_scale({}, None) == 1.0
    assert resolve_risk_scale({}, 2.0) == 2.0


def test_a_route_scale_of_one_overrides_a_slave_wide_cut():
    """0 and 1.0 are real values, not 'unset' -- a route must be able to opt
    back up to full size on a slave that is globally scaled down."""
    assert resolve_risk_scale({"risk_scale": 0.25}, 1.0) == 1.0


def test_routes_carry_it():
    cfg = {"routes": [
        {"symbol": "XAUUSD", "target": "MGC", "risk_scale": 0.25},
        {"symbol": "XAUUSD", "target": "UGL"},
    ]}
    sig = Signal(ticket=1, symbol="XAUUSD", action="OPEN", direction="BUY",
                 lots=0.1, price=4000.0, account=ACCT, magic=1001)
    routes = {r.target: r for r in resolve_routes(cfg, sig)}
    assert routes["MGC"].risk_scale == 0.25
    assert routes["UGL"].risk_scale is None   # falls back to the slave's


# ---------------- mirror slaves ------------------------------------------

_TICKET = iter(range(9001, 9999))


def _mirror(scale=None, lots=0.20):
    cfg = {} if scale is None else {"risk_scale": scale}
    broker = FakeBroker()
    queue: asyncio.Queue = asyncio.Queue()
    engine = CopyEngine("m", broker, queue, cfg)
    # A fresh ticket each time: the registry is shared within a test, and a
    # repeat OPEN on a ticket already held is correctly ignored.
    sig = Signal(ticket=next(_TICKET), symbol="XAUUSD", action="OPEN",
                 direction="BUY", lots=lots, price=4000.0, sl=3900.0,
                 account=ACCT)

    async def go():
        engine.start()
        await queue.put(sig)
        await asyncio.sleep(0.1)
        await engine.stop()

    asyncio.run(go())
    return broker


def test_a_mirror_slave_scales_the_master_lots():
    """Mirror mode copies lots verbatim, so without this the dial would only
    reach the autonomous legs -- and 'per slave' has to mean every slave."""
    assert _mirror(None).opened[0]["size"] == 0.20
    assert _mirror(1.0).opened[0]["size"] == 0.20
    assert _mirror(0.5).opened[0]["size"] == 0.10


def test_mirror_lots_are_rounded_to_the_volume_grid():
    """MT4/MT5 volumes are quoted to 0.01; 0.15 x 0.5 is 0.075, which the
    terminal rejects rather than clamping."""
    # Floored, never rounded up: this dial exists to carry less risk than
    # the master, so 0.075 becomes 0.07 rather than 0.08.
    assert _mirror(0.5, lots=0.15).opened[0]["size"] == 0.07


def test_a_mirror_scale_below_the_minimum_volume_skips_the_trade():
    """Better a skip with a reason than an order the terminal refuses."""
    broker = _mirror(0.01, lots=0.10)   # 0.001 -> under 0.01
    assert broker.opened == []


def test_a_non_positive_mirror_scale_sends_nothing():
    assert _mirror(0.0).opened == []
    assert _mirror(-1.0).opened == []
