"""The IBKR leg has to report what it actually filled at.

Seen live on 2026-11-11: an MGC round trip copied from an XAUUSD master
filled at 3988.40 and closed at 3988.20, and the registry stored NULL for
both. The interface was already there -- OrderResult carries price/lots and
base.close() is declared -> Optional[float], which the MT4/MT5 adapters
honour -- but the IBKR adapter returned as soon as the order was
acknowledged, about two seconds before the fill arrived.

That made variance capture blind on the futures leg specifically, which is
the leg it exists to measure: the whole point of copying a CFD to a future
is to learn what the basis and the slippage do, and a NULL says nothing.

Also pinned here: the entry's time-in-force. Left unset, IBKR's account
preset supplies DAY and reports it as "Error 10349", cancelling the order and
re-creating it before it fills -- so the order passes through Cancelled on a
perfectly good trade.
"""
import asyncio

import pytest

from app.adapters import create_adapter
from app.adapters.base import OrderRequest
from app.slaves import SlaveConfig


class _Status:
    def __init__(self, avg=0.0, filled=0.0, status="PreSubmitted"):
        self.avgFillPrice = avg
        self.filled = filled
        self.status = status


class _Order:
    def __init__(self, order_id=1):
        self.orderId = order_id
        self.tif = None
        self.outsideRth = False
        self.account = None
        self.ocaGroup = None
        self.ocaType = None


class _Trade:
    """A trade that fills after `fills_after` polls, like ib_async's does."""

    def __init__(self, price=100.0, size=1.0, fills_after=2, order_id=1):
        self.order = _Order(order_id)
        self.orderStatus = _Status()
        self.fills = []
        self._price, self._size = price, size
        self._left = fills_after

    def poll(self):
        if self._left > 0:
            self._left -= 1
            if self._left == 0:
                self.fills = ["fill"]
                self.orderStatus = _Status(self._price, self._size, "Filled")


class _NeverFills(_Trade):
    def __init__(self):
        super().__init__(fills_after=10**9)


def _adapter(**cfg):
    base = {"mode": "autonomous", "host": "127.0.0.1", "port": 4002,
            "client_id": 1, "fill_timeout": 0.5}
    base.update(cfg)
    return create_adapter(SlaveConfig(id="i", name="i", broker_type="ibkr",
                                      enabled=True, paused=False, config=base))


def _drive(adapter, trade, coro):
    """Run `coro`, advancing `trade` the way the broker's reader would."""

    async def ticker():
        for _ in range(200):
            trade.poll()
            await asyncio.sleep(0.01)

    async def both():
        t = asyncio.create_task(ticker())
        try:
            return await coro
        finally:
            t.cancel()

    return asyncio.run(both())


# ---------------- the wait itself ----------------------------------------

def test_await_fill_returns_price_and_size():
    a = _adapter()
    trade = _Trade(price=3988.4, size=1.0)
    price, size = _drive(a, trade, a._await_fill(trade, "entry"))
    assert price == 3988.4
    assert size == 1.0


def test_await_fill_gives_up_without_claiming_failure():
    """A timeout means 'no price captured', never 'no position' -- the order
    is live at the broker, so raising here would unwind a trade that exists."""
    a = _adapter(fill_timeout=0.1)
    trade = _NeverFills()
    price, size = _drive(a, trade, a._await_fill(trade, "entry"))
    assert price is None
    assert size is None


def test_await_fill_ignores_a_transient_cancel():
    """Error 10349 cancels the order and re-creates it, so status passes
    through Cancelled on a good trade. Keying on the fill, not the status,
    is what makes that a non-event."""
    a = _adapter()
    trade = _Trade(price=3988.4)
    trade.orderStatus = _Status(0.0, 0.0, "Cancelled")
    price, _ = _drive(a, trade, a._await_fill(trade, "entry"))
    assert price == 3988.4


def test_await_fill_does_not_trust_a_fill_without_a_price():
    """status.filled can be set a beat before avgFillPrice lands; reporting
    0.0 as the fill price would poison variance capture far more quietly
    than reporting nothing."""
    a = _adapter(fill_timeout=0.1)
    trade = _NeverFills()
    trade.fills = ["fill"]
    trade.orderStatus = _Status(0.0, 1.0, "Filled")
    price, _ = _drive(a, trade, a._await_fill(trade, "entry"))
    assert price is None


# ---------------- time in force ------------------------------------------

def test_entry_tif_defaults_to_day():
    assert _adapter().entry_tif == "DAY"
    assert _adapter(entry_tif="GTC").entry_tif == "GTC"


def test_entry_and_close_state_a_tif():
    """Both placements, not just the entry: the closing order hit 10349 too."""
    import inspect
    import re
    from app.adapters.ibkr import IBKRAdapter

    open_src = re.sub(r"\s+", " ", inspect.getsource(IBKRAdapter.open))
    close_src = re.sub(r"\s+", " ", inspect.getsource(IBKRAdapter.close))
    assert "entry.tif = self.entry_tif" in open_src
    assert "closing_order.tif = self.entry_tif" in close_src


def test_brackets_keep_their_own_tif():
    """The entry expires with the session; a stop must not."""
    a = _adapter()
    assert a.bracket_tif == "GTC"
    assert a.entry_tif != a.bracket_tif


# ---------------- open()/close() surface it ------------------------------

class _FakeIB:
    def __init__(self, trades):
        self._trades = list(trades)
        self.placed = []

    def placeOrder(self, contract, order):
        self.placed.append(order)
        t = self._trades.pop(0) if self._trades else _Trade(fills_after=1)
        t.order = order
        if not hasattr(order, "orderId") or order.orderId is None:
            order.orderId = len(self.placed)
        return t

    def cancelOrder(self, order):
        pass

    def trades(self):
        return []

    def isConnected(self):
        return True


def _prepared(adapter, trade):
    """Give the adapter just enough to run open()/close() offline."""
    adapter.ib = _FakeIB([trade])
    adapter._contracts[("MGC", 0)] = object()

    async def _contract(symbol, month_slot, contract_id=None):
        return adapter._contracts[("MGC", 0)]

    adapter._contract_for_request = _contract
    return adapter


def test_open_reports_the_fill_into_order_result():
    a = _prepared(_adapter(), _Trade(price=3988.4, size=1.0, fills_after=2))
    trade = a.ib._trades[0]
    req = OrderRequest(symbol="MGC", direction="BUY", size=1.0, sl=None, tp=None,
                       master_ticket=382160920)
    res = _drive(a, trade, a.open(req))
    assert res.price == 3988.4
    assert res.lots == 1.0
    assert a.ib.placed[0].tif == "DAY"


def test_close_returns_the_exit_price():
    a = _prepared(_adapter(), _Trade(price=3988.2, size=1.0, fills_after=2))
    trade = a.ib._trades[0]
    a._positions[35] = {"direction": "BUY", "size": 1.0,
                        "contract": object(), "sl_order_id": None,
                        "tp_order_id": None}
    price = _drive(a, trade, a.close(35))
    assert price == 3988.2
    assert a.ib.placed[0].tif == "DAY"


def test_close_drops_the_position_before_waiting():
    """A second CLOSE arriving during the fill wait must not place a second
    closing order -- that would open an unintended short."""
    a = _prepared(_adapter(fill_timeout=0.3), _NeverFills())
    trade = a.ib._trades[0]
    a._positions[35] = {"direction": "BUY", "size": 1.0,
                        "contract": object(), "sl_order_id": None,
                        "tp_order_id": None}

    async def two_closes():
        first = asyncio.create_task(a.close(35))
        await asyncio.sleep(0.05)
        with pytest.raises(Exception):
            await a.close(35)
        await first

    _drive(a, trade, two_closes())
    assert len(a.ib.placed) == 1
