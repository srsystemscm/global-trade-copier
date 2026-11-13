"""A master SL/TP of 0 means "not set", not "a price of zero".

MQL4's OrderStopLoss()/OrderTakeProfit() return 0.0 when no level is set, and
StringFormat can't emit JSON null, so "no take profit" reaches the hub as
tp=0.0. The autonomous engine derives its ATR multiple from |tp - price|, so
taken literally that's a multiple of the entire price of the instrument.

Seen live on 2026-10-29: a master XAUUSD BUY at 4029.32 with no TP produced a
GC target of 7703 against a 4040 entry -- nearly double.
"""
from app.models import Signal


def _sig(**kw):
    base = dict(ticket=1, symbol="XAUUSD", action="OPEN", direction="BUY",
                lots=0.1, price=4029.32)
    base.update(kw)
    return Signal(**base)


def test_zero_take_profit_becomes_none():
    assert _sig(tp=0.0).tp is None


def test_zero_stop_loss_becomes_none():
    assert _sig(sl=0.0).sl is None


def test_real_levels_are_untouched():
    s = _sig(sl=3937.0, tp=4200.0)
    assert (s.sl, s.tp) == (3937.0, 4200.0)


def test_none_stays_none():
    s = _sig()
    assert s.sl is None and s.tp is None


def test_the_live_case():
    """Exactly what the master sent: a real stop, no target."""
    s = _sig(sl=3937.0, tp=0.0)
    assert s.sl == 3937.0
    assert s.tp is None, "a zero TP must not become a target at twice the price"
