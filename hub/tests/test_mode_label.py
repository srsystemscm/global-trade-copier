"""The word on screen has to match what the slave does.

Raised on 2026-11-15, looking at the dashboard: every slave showed
"mirror" or "autonomous" beside its broker, and neither word could be
acted on. The reading was that it was an engine carry-over.

Half right. `mode` is load-bearing -- engine_manager picks CopyEngine or
AutonomousCopyEngine from it -- but the LABEL had stopped being true.
"mirror" reads as "copies the master's lots", and that is what it meant
back when a mirror slave had no sizing of its own. Both live mirror
slaves (mt5-icm-1, mt5-dwx-futures-1) carry a pct_risk block: they size
from their own capital and mirror only the stop LEVEL.

A word that means the opposite of what it says is worse than no word,
because a position gets sized against it.
"""
from app.api_routes import describe_mode


def test_a_mirror_slave_that_sizes_itself_does_not_claim_to_mirror_lots():
    """mt5-icm-1's actual config. The old label said "mirror"."""
    label = describe_mode({
        "mode": "mirror",
        "sizing": {"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance"},
    })
    assert "own size" in label
    assert "lots" not in label, (
        f"{label!r} still tells the reader this slave copies the master's "
        f"lot count, which is what it stopped doing when it got a sizing block"
    )


def test_a_mirror_slave_with_no_sizing_does_mirror_lots_and_says_so():
    """The guard against over-correcting: this case is real and the
    reader most needs telling, because nothing else on screen
    distinguishes it from a slave that sizes itself."""
    label = describe_mode({"mode": "mirror"})
    assert "lots" in label


def test_both_mirror_cases_say_the_stop_is_the_master_s():
    """What mirror mode genuinely still means in every case."""
    for config in ({"mode": "mirror"},
                   {"mode": "mirror", "sizing": {"mode": "pct_risk"}}):
        assert "master's stops" in describe_mode(config)


def test_an_autonomous_slave_says_the_stop_is_its_own():
    label = describe_mode({"mode": "autonomous", "routes": [{"symbol": "XAUUSD"}]})
    assert "own stops" in label
    assert "master" not in label


def test_the_routed_instrument_count_is_the_thing_a_reader_checks():
    """ibkr-etf-1 has 20 routes for 10 symbols, because an inverse ETF
    needs a route per direction. The count is how you notice that."""
    config = {"mode": "autonomous", "routes": [{"symbol": f"S{i}"} for i in range(20)]}
    assert "20 routed instruments" in describe_mode(config)
    one = {"mode": "autonomous", "routes": [{"symbol": "XAUUSD"}]}
    assert describe_mode(one).endswith("1 routed instrument"), "plural on a single route"


def test_a_missing_mode_is_described_rather_than_left_blank():
    """engine_manager defaults to mirror, so the label must too --
    otherwise a slave with no mode set shows nothing and reads as broken."""
    assert describe_mode({})
    assert describe_mode(None)
