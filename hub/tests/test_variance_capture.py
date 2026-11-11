"""What each copy actually executed at.

The point of these fields is not any single row -- it is the distribution
across many, which is what distinguishes spread from latency from sizing.
That only works if the data exists from the first trade: a fill price is
reported once, in the reply to the order, and cannot be reconstructed
afterwards from anything else the hub keeps.

So what is pinned here is mostly that nothing silently discards it, and that
"not reported" stays distinguishable from "zero".
"""
import asyncio

from app import trade_registry
from app.adapters.mt4 import MT4Adapter
from app.copy_engine import CopyEngine
from app.db import db_cursor
from app.models import Signal
from tests.conftest_fakes import FakeBroker

SLAVE = "slave-1"
ACCT = "ICM-44916920"


def _row(master_ticket=5001, slave_symbol="XAUUSD"):
    with db_cursor() as cur:
        r = cur.execute(
            "SELECT * FROM slave_trades WHERE slave_id = ? AND master_account = ? "
            "AND master_ticket = ? AND slave_symbol = ?",
            (SLAVE, ACCT, master_ticket, slave_symbol),
        ).fetchone()
    return dict(r) if r else None


def _journey(broker, signals, settle=0.08):
    queue: asyncio.Queue = asyncio.Queue()
    engine = CopyEngine(SLAVE, broker, queue)

    async def go():
        engine.start()
        for s in signals:
            await queue.put(s)
            await asyncio.sleep(0.02)
        await asyncio.sleep(settle)
        await engine.stop()

    asyncio.run(go())


def _open(**kw):
    base = dict(ticket=5001, symbol="XAUUSD", action="OPEN", direction="BUY",
                lots=0.10, price=4029.32, sl=3937.0, account=ACCT)
    base.update(kw)
    return Signal(**base)


# --- the open side --------------------------------------------------------

def test_the_fill_price_is_recorded_rather_than_discarded():
    """Every adapter returned OrderResult.price and nothing stored it, so the
    copier could not answer what the slave filled at versus the master --
    the headline number for copy quality."""
    broker = FakeBroker(fill_price=4029.57)
    _journey(broker, [_open()])

    row = _row()
    assert row["slave_price"] == 4029.57
    assert row["master_price"] == 4029.32


def test_slippage_comes_out_signed_the_way_it_costs():
    """A BUY filled above the master's price cost money; a SELL filled below
    it did too. The sign has to survive, or a distribution of these averages
    to roughly nothing."""
    broker = FakeBroker(fill_price=4029.57)
    _journey(broker, [_open(direction="BUY", price=4029.32)])
    row = _row()
    assert row["slave_price"] - row["master_price"] > 0

    broker2 = FakeBroker(fill_price=4029.10)
    _journey(broker2, [_open(ticket=5002, direction="SELL", price=4029.32)])
    row2 = _row(master_ticket=5002)
    assert row2["master_price"] - row2["slave_price"] > 0


def test_the_two_halves_of_latency_stay_separable():
    """requested_at brackets the adapter call so hub-and-queue time can be
    told apart from broker time. Blaming the wrong half is the usual way
    latency work goes nowhere."""
    broker = FakeBroker(fill_price=4029.57)
    sig = _open()
    _journey(broker, [sig])

    row = _row()
    assert row["requested_at"] is not None and row["filled_at"] is not None
    assert row["requested_at"] <= row["filled_at"]
    assert row["requested_at"] >= sig.ts, "the order cannot predate its signal"


def test_lots_record_what_actually_went_on():
    broker = FakeBroker(fill_price=4029.57)
    _journey(broker, [_open(lots=0.37)])
    assert _row()["slave_lots"] == 0.37


def test_a_broker_that_reports_no_fill_price_records_none_not_zero():
    """IBKR's market order comes back with an order id and fills later. None
    has to keep meaning "not captured" -- a zero would enter the sample as a
    four-thousand-point slippage."""
    broker = FakeBroker(fill_price=None)
    _journey(broker, [_open()])

    row = _row()
    assert row["slave_price"] is None
    assert row["master_price"] == 4029.32, "the master side is still worth having"


# --- the close side -------------------------------------------------------

def test_both_exit_prices_are_recorded():
    """The other half of the round trip. Without it only the entry's variance
    can be measured, and entry slippage alone doesn't say whether the copy
    tracked the master."""
    broker = FakeBroker(fill_price=4029.57, close_price=4055.10)
    _journey(broker, [_open(), _open(action="CLOSE", price=4055.00)])

    row = _row()
    assert row["status"] == "CLOSED"
    assert row["slave_close_price"] == 4055.10
    assert row["master_close_price"] == 4055.00


def test_round_trip_divergence_is_derivable():
    """What the whole thing is for: the slave made 25.53 points where the
    master made 25.68, so this copy gave up 0.15."""
    broker = FakeBroker(fill_price=4029.57, close_price=4055.10)
    _journey(broker, [_open(price=4029.32), _open(action="CLOSE", price=4055.00)])

    row = _row()
    master_move = row["master_close_price"] - row["master_price"]
    slave_move = row["slave_close_price"] - row["slave_price"]
    assert round(master_move - slave_move, 5) == 0.15


def test_reconciliation_closing_a_row_does_not_wipe_captured_prices():
    """reconcile_slave() closes rows it finds the broker no longer holds, and
    it has no prices to offer. Assigning NULL there would erase the half of
    the round trip that WAS measured."""
    broker = FakeBroker(fill_price=4029.57)
    _journey(broker, [_open()])

    # no prices passed, exactly as reconciliation calls it
    trade_registry.record_close(SLAVE, ACCT, 5001, "XAUUSD")

    row = _row()
    assert row["status"] == "CLOSED"
    assert row["slave_price"] == 4029.57, "the entry fill was erased on close"


# --- what the EAs send ----------------------------------------------------

def _adapter(reply):
    a = MT4Adapter(slave_id="t", host="127.0.0.1", port=1)

    async def fake_request(command, retries=None):
        return reply

    a._request = fake_request  # type: ignore[assignment]
    return a


def test_open_parses_the_fill_price_and_lots_from_the_ea():
    from app.adapters.base import OrderRequest

    res = asyncio.run(_adapter({"status": "ok", "slave_ticket": 900101,
                                "price": 4029.57, "lots": 0.10})
                      .open(OrderRequest(symbol="XAUUSD", direction="BUY", size=0.10)))
    assert (res.slave_ticket, res.price, res.lots) == (900101, 4029.57, 0.10)


def test_a_zero_fill_price_from_an_ea_reads_as_absent():
    """MQL formats numbers rather than emitting null, so an EA with nothing
    to report sends 0.0 -- which as a price would be a four-thousand-point
    outlier in the sample."""
    from app.adapters.base import OrderRequest

    res = asyncio.run(_adapter({"status": "ok", "slave_ticket": 1, "price": 0.0})
                      .open(OrderRequest(symbol="XAUUSD", direction="BUY", size=0.1)))
    assert res.price is None


def test_close_returns_the_exit_fill():
    assert asyncio.run(_adapter({"status": "ok", "price": 4055.10}).close(900101)) == 4055.10


def test_an_already_closed_leg_reports_no_exit_price():
    """It was closed by something outside the copier, so there is no copier
    exit fill to report. None rather than an invented number."""
    assert asyncio.run(
        _adapter({"status": "ok", "already_closed": True, "slave_ticket": 1}).close(1)
    ) is None


def test_the_simulator_reports_fills_like_the_eas_do():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "_sim", Path(__file__).resolve().parent.parent / "scripts" / "simulate_slave.py")
    sim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sim)

    opened = sim.handle({"cmd": "OPEN", "master_ticket": 1, "symbol": "XAUUSD",
                         "direction": "BUY", "lots": 0.1, "price": 4029.32,
                         "sl": None, "tp": None})
    assert opened["price"] == 4029.32 and opened["lots"] == 0.1
    assert "price" in sim.handle({"cmd": "CLOSE", "ticket": opened["slave_ticket"]})


# --- migrating a database that predates the columns ----------------------

def test_the_columns_are_added_to_an_existing_table():
    """These are added columns, not a key change, so SQLite can ALTER TABLE
    rather than needing the rebuild the account keys required."""
    from app.db import init_db

    with db_cursor() as cur:
        for col in ("master_price", "slave_price", "slave_lots", "requested_at",
                    "filled_at", "master_close_price", "slave_close_price"):
            cur.execute(f"ALTER TABLE slave_trades DROP COLUMN {col}")
        cur.execute(
            "INSERT INTO slave_trades (slave_id, master_account, master_ticket, slave_symbol, "
            "slave_ticket, status) VALUES (?, ?, 777, 'XAUUSD', 42, 'OPEN')", (SLAVE, ACCT))

    init_db()

    row = _row(master_ticket=777)
    assert row is not None, "the existing row was lost"
    assert row["slave_ticket"] == 42
    assert row["slave_price"] is None
