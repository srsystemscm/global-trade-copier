"""A portfolio from PM decides what a slave takes, so it is read strictly.

The step this replaces was typing magic numbers and their risk scales in
by hand, one at a time. Chris, 2026-11-18: "I dont want to have to (if
possible) introduce a manual, error prone step like adding magic numbers
and their risk scale by hand."

What makes that safe to automate is that the file is refused whole when
it cannot be read, and that applying it replaces the selection rather
than merging with it. A merge would leave a strategy running after it was
taken out of the book, which is the one outcome an import must not have.

The contract is docs/portfolio-import.md.
"""
import pytest

from app import portfolio_import as pi


def export(strategies, **portfolio):
    meta = {"id": "pf_7f3a9c", "name": "Trend", "revision": 7}
    meta.update(portfolio)
    return {
        "schema": "gtc.portfolio-import/1.0",
        "portfolio": meta,
        "strategies": strategies,
    }


TWO = export([
    {"magic": 1001, "name": "US2000 trend", "risk_scale": 0.5},
    {"magic": 1002, "name": "GC breakout", "risk_scale": 1.0},
])


def test_a_portfolio_becomes_a_selection_and_its_scales():
    """The two halves of one decision, written as the two keys the engine
    already reads -- so an import needs no restart and no new concept."""
    config = pi.apply({}, pi.parse(TWO))

    assert [e["magic"] for e in config["magic_filter"]["allow"]] == [1001, 1002]
    assert config["risk_scale_by_magic"] == {"1001": 0.5, "1002": 1.0}
    # The names travel, so a log line and the ops hub's Copier tab can say
    # "US2000 trend" instead of 1001.
    assert config["magic_filter"]["allow"][0]["strategy"] == "US2000 trend"


def test_everything_the_import_does_not_own_is_carried_across():
    """PATCH /slaves/{id} replaces config_json WHOLESALE. An import that
    returned only its own two keys would delete this slave's sizing, its
    routes and every limit on it."""
    before = {
        "mode": "autonomous",
        "sizing": {"mode": "pct_risk", "max_risk_pct": 0.025},
        "routes": [{"symbol": "XAUUSD", "magic": 1001, "target": "MGC"}],
        "netting": {"max_legs": 3},
        "entry_chase_steps": 6,
    }
    after = pi.apply(before, pi.parse(TWO))

    for key, value in before.items():
        assert after[key] == value, f"{key} was not carried across"


def test_a_strategy_the_file_does_not_mention_is_dropped():
    """Replaced, not merged. The file states the portfolio in full, and a
    magic missing from it is one the slave stops taking -- the only
    reading that cannot leave a strategy trading after it was removed
    from the book."""
    before = {
        "magic_filter": {"allow": [1001, 1003]},
        "risk_scale_by_magic": {"1001": 1.0, "1003": 0.5},
    }
    after = pi.apply(before, pi.parse(TWO))

    assert [e["magic"] for e in after["magic_filter"]["allow"]] == [1001, 1002]
    assert "1003" not in after["risk_scale_by_magic"]


def test_a_scale_of_zero_is_kept_rather_than_dropped():
    """0 means "do not trade this strategy here", which is a decision.
    Dropping the magic instead would say "never told about it", and the
    two are indistinguishable afterwards."""
    config = pi.apply({}, pi.parse(export([
        {"magic": 1001, "risk_scale": 0},
        {"magic": 1002, "risk_scale": 1.0},
    ])))

    assert config["risk_scale_by_magic"]["1001"] == 0
    assert 1001 in [e["magic"] for e in config["magic_filter"]["allow"]]


def test_where_a_selection_came_from_is_recorded():
    """So an imported selection can be told from a hand-written one, and
    the two sides can talk about the same book."""
    source = pi.apply({}, pi.parse(TWO))["magic_filter"]["source"]

    # THE AGREED WORD, not a new one. The reporter already sends this
    # block to the ops hub, and docs/hub-reporter.md settled "kind" as
    # ops_hub_registry before this importer existed -- the hub's registry
    # IS its portfolio manager. A second name for one thing would have
    # had the hub read every imported selection as hand-written.
    assert source["kind"] == "ops_hub_registry"
    assert source["portfolio_id"] == "pf_7f3a9c"
    assert source["revision"] == 7
    assert source["imported_at"].endswith("Z")


def test_fields_the_copier_does_not_know_are_ignored():
    """PM adds fields for its own reasons and the copier has no business
    failing on one."""
    document = export([
        {"magic": 1001, "risk_scale": 1.0, "sharpe": 1.7, "tags": ["trend"]},
    ], owner="chris", colour="#ff0000")
    document["notes"] = "whatever PM wants to say"

    assert pi.parse(document).magics == [1001]


# --- the refusals ----------------------------------------------------
#
# Each one is refused WHOLE, with nothing written, because a partly
# applied selection is a slave trading a book nobody chose.

def test_an_empty_portfolio_is_refused_rather_than_written():
    """An empty selection and no selection are opposites that read
    identically as a boolean -- the reasoning that already refuses
    {"allow": []}. An empty list is what an export looks like when
    something went wrong producing it, and a slave given one would
    silently stop trading."""
    with pytest.raises(pi.PortfolioImportError, match="no strategies"):
        pi.parse(export([]))


def test_a_schema_this_copier_does_not_read_is_refused():
    """A later major version may mean something different by a field this
    one recognises, which is worse than not reading it at all."""
    document = export([{"magic": 1001, "risk_scale": 1.0}])
    document["schema"] = "gtc.portfolio-import/2.0"
    with pytest.raises(pi.PortfolioImportError, match="2.0"):
        pi.parse(document)

    document["schema"] = "some.other.thing/1.0"
    with pytest.raises(pi.PortfolioImportError, match="does not look like"):
        pi.parse(document)


def test_a_magic_listed_twice_with_two_scales_is_refused():
    """Which one wins is not a thing to guess at."""
    with pytest.raises(pi.PortfolioImportError, match="appears twice"):
        pi.parse(export([
            {"magic": 1001, "risk_scale": 0.5},
            {"magic": 1001, "risk_scale": 1.0},
        ]))


def test_the_same_magic_on_two_masters_is_refused():
    """THE MULTI-MASTER SEAM, found on 2026-11-18 while checking whether
    the hub is plumbed for more than one master. `routes` entries carry
    their own `account`; magic_filter and risk_scale_by_magic do not --
    they are keyed by magic alone. So a hub following two masters that
    both use magic 1001 cannot tell them apart, and writing this would
    bake that ambiguity into a slave. Better refused here than found the
    first evening both masters signal at once."""
    with pytest.raises(pi.PortfolioImportError, match="two masters"):
        pi.parse(export([
            {"magic": 1001, "risk_scale": 0.5, "account": "44964870"},
            {"magic": 1001, "risk_scale": 0.5, "account": "53116707"},
        ]))


def test_a_negative_scale_is_refused():
    """Not a smaller position: an inverted one."""
    with pytest.raises(pi.PortfolioImportError, match="negative"):
        pi.parse(export([{"magic": 1001, "risk_scale": -0.5}]))


def test_a_scale_far_above_one_is_refused_as_a_unit_mistake():
    """50 meaning 50% is a likelier reading than 50 times what the EA
    staked. A genuine figure above the ceiling can still be set by hand,
    where somebody is looking at that one number."""
    with pytest.raises(pi.PortfolioImportError, match="typo or a unit mistake"):
        pi.parse(export([{"magic": 1001, "risk_scale": 50}]))

    # The ceiling itself is allowed: it is a limit, not a wall short of it.
    assert pi.parse(export([{"magic": 1001, "risk_scale": pi.MAX_RISK_SCALE}]))


def test_a_strategy_with_no_scale_is_refused_rather_than_assumed():
    """1.0 is a value -- "take this EA's signal whole" -- not a default to
    assume on the file's behalf."""
    with pytest.raises(pi.PortfolioImportError, match="no risk_scale"):
        pi.parse(export([{"magic": 1001, "name": "US2000 trend"}]))


def test_magic_zero_is_a_real_strategy_and_is_accepted():
    """OrderMagicNumber() is 0 for a trade placed by hand, so 0 is a real
    value. Only the unreadable ones are refused."""
    assert pi.parse(export([{"magic": 0, "risk_scale": 1.0}])).magics == [0]

    with pytest.raises(pi.PortfolioImportError, match="not a whole number"):
        pi.parse(export([{"magic": True, "risk_scale": 1.0}]))


# --- the plan --------------------------------------------------------

def test_the_plan_says_what_applying_would_change():
    """Returned for a dry run and again for the real thing, so what was
    approved and what happened are the same words."""
    before = {
        "magic_filter": {"allow": [1001, 1003]},
        "risk_scale_by_magic": {"1001": 1.0, "1003": 0.5},
    }
    plan = pi.plan(before, pi.parse(TWO))

    assert plan["magics_added"] == [1002]
    assert plan["magics_removed"] == [1003]
    assert plan["scales_changed"] == [{"magic": 1001, "from": 1.0, "to": 0.5}]
    assert plan["portfolio"]["id"] == "pf_7f3a9c"


def test_narrowing_from_every_strategy_is_called_out():
    """Every slave today takes every strategy. The first import on one is
    the largest change this can make, and "2 added" understates it."""
    assert pi.plan({}, pi.parse(TWO))["narrows_from_every_magic"] is True
    assert pi.plan(
        {"magic_filter": {"allow": [1001]}}, pi.parse(TWO),
    )["narrows_from_every_magic"] is False


def test_an_import_over_an_earlier_one_says_which_it_replaces():
    """So a newer revision of the same portfolio reads as that, and a
    DIFFERENT portfolio landing on top of one reads as that."""
    config = pi.apply({}, pi.parse(TWO))
    newer = export([{"magic": 1001, "risk_scale": 0.25}], revision=8)

    replaced = pi.plan(config, pi.parse(newer))["replaces_import"]
    assert replaced["portfolio_id"] == "pf_7f3a9c"
    assert replaced["revision"] == 7


def test_a_hand_written_selection_is_not_claimed_as_an_import():
    assert pi.plan({"magic_filter": {"allow": [1001]}},
                   pi.parse(TWO))["replaces_import"] is None


def test_the_reported_source_is_the_word_the_ops_hub_already_reads():
    """This block does not stay on the slave: the reporter sends it in
    `snapshot.slaves[].magic_filter`, which is how the hub's Copier tab
    shows which book a slave is running. docs/hub-reporter.md fixed the
    word for it before this importer existed, and inventing a second one
    would have made every imported selection read as hand-written."""
    from pathlib import Path

    contract = (Path(__file__).resolve().parents[2]
                / "docs" / "hub-reporter.md").read_text(encoding="utf-8")
    assert f'"kind": "{pi.SOURCE_KIND}"' in contract, (
        f"{pi.SOURCE_KIND!r} is not the value the reporter contract "
        f"documents, so the hub will not recognise an imported selection"
    )


def test_a_selection_written_under_the_older_word_is_still_an_import():
    """It existed for an afternoon on 2026-11-18. Read back as a hand
    edit, an import over it would say it replaces nothing."""
    config = {"magic_filter": {"allow": [1001],
                               "source": {"kind": "portfolio_manager",
                                          "portfolio_id": "pf_old",
                                          "revision": 2}}}
    assert pi.plan(config, pi.parse(TWO))["replaces_import"]["portfolio_id"] == "pf_old"


# --- the route -------------------------------------------------------
#
# Read off the source, as the other route guarantees in this suite are:
# there is no app fixture here, and these two properties are the ones
# that would be quietly lost in a refactor.

def test_the_route_does_not_write_unless_it_is_told_to():
    """An import replaces a slave's whole selection, so "what would this
    change" has to be answerable without answering it by doing it."""
    import inspect

    from app import api_routes

    signature = inspect.signature(api_routes.import_portfolio)
    assert signature.parameters["dry_run"].default is True

    src = inspect.getsource(api_routes.import_portfolio)
    assert "if dry_run:" in src and "return {\"applied\": False" in src


def test_the_route_writes_through_patch_slave_rather_than_around_it():
    """patch_slave validates the selection before storing it, updates the
    bus so the change takes effect without a restart, and keeps the
    engine's live tunables consistent. An import that called update_slave
    itself would get one of those three and silently drop the others --
    a slave still taking the old strategies until the next restart."""
    import inspect

    from app import api_routes

    src = inspect.getsource(api_routes.import_portfolio)
    assert "await patch_slave(" in src
    assert "update_slave(" not in src, (
        "the import writes the config itself, so the bus is never told "
        "which strategies this slave now takes"
    )


def test_an_imported_portfolio_reaches_the_ops_hub_as_it_stands():
    """The slave reports the portfolio it is running, with no further
    plumbing. Chris asked whether it could; it already does.

    `snapshot.slaves[].magic_filter` is magic_filter.parse(config).as_dict()
    verbatim, so everything the import writes travels on the next snapshot
    -- the magics, the strategy NAMES, and which portfolio at which
    revision they were resolved from. That is what lets the hub's Copier
    tab say "this slave takes the trend-following book" in its own words,
    and spot one that has drifted since.

    Spans two modules that know nothing about each other, which is why it
    is worth a test: either could stop carrying `source` without the other
    noticing.
    """
    from app import magic_filter

    config = pi.apply({"mode": "autonomous"}, pi.parse(TWO))
    reported = magic_filter.parse(config).as_dict()

    assert reported["allow"] == [1001, 1002]
    assert reported["labels"] == {"1001": "US2000 trend", "1002": "GC breakout"}
    assert reported["source"]["portfolio_id"] == "pf_7f3a9c"
    assert reported["source"]["revision"] == 7
    assert reported["source"]["kind"] == pi.SOURCE_KIND
