"""A size the broker is sent should be the size that was decided.

MEASURED. hub.log, 2026-11-16, mt5-icm-1, an H1 XAUUSD signal:

    skipping OPEN ticket=382988392: nothing tradeable among XAUUSD --
      XAUUSD: size 0.8200000000000001 risks 3897.46, over the 2.50%

    opened XAUUSD master_ticket=382988520 -> slave_ticket=2094914536
      size=0.82 of an intended 0.8200000000000001, the rest in slices

82 * 0.01 is 0.8200000000000001 in IEEE754. MT accounts trade in
hundredths of a lot, so most sizes this engine produces for them come out
carrying that tail.

IT IS NOT COSMETIC. That number goes to the broker as a lot size, it is
compared against the risk ceiling, and a person reads it in the log. It
also made the engine announce "the rest in slices" on an adapter that
places one order per entry -- the check for a split was `placed !=
sizing_result.size`, and one ULP is a difference.

WHAT IT DID NOT DO, since that was the suspicion worth testing: it did
not cause the refusal above. 0.82 risks 3897.46 against a 2.50% ceiling,
and the tail moves that figure by about 5e-13. The trade was genuinely
over the cap.
"""
import pytest

from app.sizing import _snap


class TestSnap:
    def test_hundredths_of_a_lot_come_out_clean(self):
        """THE BUG, in one line."""
        assert _snap(82, 0.01) == 0.82
        assert repr(_snap(82, 0.01)) == "0.82", (
            f"still carrying a tail: {_snap(82, 0.01)!r}"
        )

    @pytest.mark.parametrize("units", range(1, 200))
    def test_no_multiple_of_a_hundredth_carries_a_tail(self, units):
        """Not just 82. Any lot size this engine can produce for an MT
        account has to be exactly what it says."""
        got = _snap(units, 0.01)
        assert got == pytest.approx(units / 100.0)
        assert len(repr(got).split(".")[-1]) <= 2, (
            f"{units} x 0.01 came out as {got!r}"
        )

    def test_the_step_decides_the_precision(self):
        """A step of 1 is contracts and has no decimals; a step of 0.001
        is three. Taken from how the step is WRITTEN, because 0.01 as a
        float has seventeen digits and none of them are the question."""
        assert _snap(7, 1.0) == 7.0
        assert _snap(3, 0.1) == 0.3
        assert _snap(13, 0.001) == 0.013

    def test_the_value_is_not_changed_only_its_tail(self):
        """Rounding to the step's own precision must never move a size to
        a different multiple of that step."""
        for units in (1, 7, 33, 82, 199):
            for step in (0.01, 0.1, 1.0):
                assert _snap(units, step) == pytest.approx(units * step)

    def test_a_step_that_makes_no_sense_does_not_raise(self):
        """A bad spec must not take the sizing path down with it. The
        size is still computable; it just keeps whatever the multiply
        gave rather than a rounding of nonsense."""
        assert _snap(2, float("inf")) == float("inf")
        nan = _snap(2, float("nan"))
        assert nan != nan  # still NaN, and no exception on the way


# ------------------------------------------------- end to end ----------

def test_a_computed_mt_lot_size_has_no_tail():
    """Through compute_size, which is what the engine actually calls."""
    from app.sizing import compute_size

    from app.contract_specs import ContractSpec

    # XAUUSD as an MT account trades it: hundredths of a lot, 100 oz.
    spec = ContractSpec(symbol="XAUUSD", tick_size=0.01, tick_value=1.0,
                        point_value=100.0, min_size=0.01)
    result = compute_size(
        sizing_config={"mode": "pct_risk", "risk_pct": 0.02,
                       "capital_base": "balance"},
        master_lots=0.78,
        entry_price=3875.59,
        stop_price=3851.88,
        account_balance=100000.0,
        account_equity=100000.0,
        contract_spec=spec,
    )
    assert not result.skipped, result.reason
    tail = repr(result.size).split(".")[-1]
    assert len(tail) <= 2, (
        f"compute_size produced {result.size!r}, which is what reaches the "
        "broker and the risk check"
    )


def test_the_split_message_is_decided_by_the_slice_count():
    """The engine announced a split on an MT5 leg because two floats
    differed by one ULP. The adapter is what knows whether it split."""
    import inspect

    from app.autonomous_copy_engine import AutonomousCopyEngine

    src = inspect.getsource(AutonomousCopyEngine._open_one) \
        if hasattr(AutonomousCopyEngine, "_open_one") \
        else inspect.getsource(AutonomousCopyEngine)
    assert "if (result.slices or 1) > 1:" in src, (
        "the split message is still decided by comparing sizes, so any "
        "float tail makes a non-splitting adapter claim slices are coming"
    )
    assert "if placed != sizing_result.size:" not in src
