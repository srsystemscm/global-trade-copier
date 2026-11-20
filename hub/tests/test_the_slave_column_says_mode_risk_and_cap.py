"""The slave column is read before a size is trusted, so it has to be true.

Every screen that shows a slave -- the dashboard tiles, the dashboard
list, the Settings Risk tab -- was folding its own line out of
`sizing_summary`, which is a sentence. Sentences drift. When the pct_risk
wording changed on 2026-11-16 the Risk tab's test for it stopped
matching and the card printed the raw summary where a calibration line
belonged, and the dashboard was reading the refusal ceiling back out of
the prose with a regex over "capped 2.50%". Chris, looking at it:
"stale info".

`sizing_facts` is those four values as values. These tests are built
from the four live slave configs as the hub reported them on
2026-11-17, so a change that breaks one breaks it against a real slave.
"""
from app.api_routes import _sizing_facts


# mt5-icm-1, as /status reported it on 2026-11-17.
PCT_RISK = {
    "mode": "autonomous",
    "sizing": {"mode": "pct_risk", "risk_pct": 0.01, "capital_base": "balance",
               "max_risk_pct": 0.025, "rounding": "nearest_min1"},
}


def test_a_slave_that_stakes_what_the_master_staked_reads_as_master():
    """pct_risk is not a percentage any more -- apply_master_risk replaces
    risk_pct with what the master's order actually staked -- so the word
    for it is where the size comes FROM, not the number it fell back on."""
    facts = _sizing_facts(PCT_RISK)
    assert facts["mode"] == "master"
    assert facts["basis"] == "balance"
    assert facts["cap_pct"] == 0.025


def test_a_slave_with_no_sizing_block_reads_as_mirror():
    """The case the reader most needs told: it sends the master's lot
    count across unchanged, and nothing else on the card says so.
    ibkr-paper-1 was in exactly this state after its block was deleted."""
    facts = _sizing_facts({"mode": "autonomous", "routes": [{"symbol": "XAUUSD"}]})
    assert facts["mode"] == "mirror"
    assert facts["basis"] is None
    assert facts["cap_pct"] is None


def test_a_pct_risk_block_with_no_capital_base_still_says_balance():
    """compute_size reads balance when nothing says otherwise, so a blank
    here would be the screen declining to answer a question it knows."""
    facts = _sizing_facts({"sizing": {"mode": "pct_risk", "risk_pct": 0.01}})
    assert facts["basis"] == "balance"


def test_a_route_sized_slave_reports_the_cap_its_routes_carry():
    """ibkr-etf-1 carries sizing on each of twenty routes and none at the
    slave level. Read at the slave level alone it has no ceiling at all,
    which is how a 2.50%% refusal threshold came to show as blank."""
    config = {
        "mode": "autonomous",
        "routes": [
            {"symbol": f"S{i}",
             "sizing": {"mode": "pct_risk", "capital_base": "balance",
                        "max_risk_pct": 0.025}}
            for i in range(20)
        ],
    }
    facts = _sizing_facts(config)
    assert facts["mode"] == "master"
    assert facts["cap_pct"] == 0.025


def test_a_route_that_overrides_only_the_ceiling_keeps_the_slave_s_mode():
    """merge_sizing lets a route name only the keys it changes. One such
    route exists today, on ETHUSD, carrying nothing but a higher ceiling
    -- and read on its own it has no `mode`, so it would report as
    mirroring the master's lots on a slave that does no such thing."""
    config = {
        "sizing": {"mode": "pct_risk", "capital_base": "balance", "max_risk_pct": 0.025},
        "routes": [
            {"symbol": "BTCUSD"},
            {"symbol": "ETHUSD", "sizing": {"max_risk_pct": 0.035}},
        ],
    }
    facts = _sizing_facts(config)
    assert facts["mode"] == "master"
    assert facts["basis"] == "balance"
    # The ceilings genuinely differ, and naming either one would be a lie
    # about the other route. Reported as the two numbers that bound it,
    # which fit in the same space "mixed" occupied and actually say
    # something: Chris, 2026-11-18, "show the cap as a range".
    assert facts["cap_pct"] == {"min": 0.025, "max": 0.035, "unbounded": False}


def test_blocks_that_disagree_read_as_mixed_rather_than_as_one_of_them():
    """schwab-demo-1's per-target form: one target fixed-contracts, the
    other dollar-notional. "fixed_contracts" alone would describe half
    the slave."""
    config = {"sizing": {"MGC": {"mode": "fixed_contracts", "contracts": 1},
                         "GLD": {"mode": "dollar_notional", "notional": 5000}}}
    assert _sizing_facts(config)["mode"] == "mixed"


def test_an_unset_risk_scale_reads_as_one_rather_than_as_nothing():
    """Unscaled is a setting. Three of the four live slaves store no
    risk_scale at all and take the EA's signalled risk whole."""
    assert _sizing_facts(PCT_RISK)["risk_scale"] == 1.0
    assert _sizing_facts({"risk_scale": 0.25})["risk_scale"] == 0.25


def test_strategies_on_their_own_figure_are_counted():
    """The headline calibration is not the whole story when some magics
    carry their own, and the count is what sends a reader to look."""
    facts = _sizing_facts({"risk_scale": 0.5, "risk_scale_by_magic": {"1001": 0.5, "1002": 2}})
    assert facts["scale_overrides"] == 2
    assert _sizing_facts(PCT_RISK)["scale_overrides"] == 0


def test_the_ceiling_is_a_range_when_the_routes_disagree():
    """ibkr-paper-1 as the hub reported it on 2026-11-18, which is what
    prompted this: 20% at the slave level, nine routes inheriting it, and
    ETHUSDRR/MET overriding to 3.5%. The card asserted 20% of a slave
    where one route is nearly six times tighter."""
    config = {
        "sizing": {"mode": "pct_risk", "capital_base": "balance",
                   "max_risk_pct": 0.2},
        "routes": [{"symbol": f"S{i}", "sizing": {"rounding": "floor"}}
                   for i in range(9)]
        + [{"symbol": "ETHUSDRR", "sizing": {"max_risk_pct": 0.035}}],
    }
    assert _sizing_facts(config)["cap_pct"] == {
        "min": 0.035, "max": 0.2, "unbounded": False}


def test_a_route_with_no_ceiling_at_all_is_not_folded_into_a_maximum():
    """A block with no max_risk_pct refuses nothing. Reporting the other
    routes' highest figure as the maximum would name a ceiling where
    there is none -- on the one route that has no ceiling."""
    config = {
        "routes": [
            {"symbol": "A", "sizing": {"mode": "pct_risk", "max_risk_pct": 0.02}},
            {"symbol": "B", "sizing": {"mode": "pct_risk"}},
        ],
    }
    cap = _sizing_facts(config)["cap_pct"]
    assert cap["unbounded"] is True
    assert cap["min"] == 0.02


def test_one_ceiling_everywhere_is_still_a_single_figure():
    """The guard against over-correcting: three of the four live slaves
    carry one ceiling and should read "2.50%", not a range of it to
    itself."""
    config = {"sizing": {"mode": "pct_risk", "max_risk_pct": 0.025},
              "routes": [{"symbol": "A"}, {"symbol": "B"}]}
    assert _sizing_facts(config)["cap_pct"] == 0.025
