"""The card's headline number had no field anywhere near it.

The risk card leads, folded, with "risk scale x1.00 of what the master
staked" -- risk scale being the calibration between what the EA signalled
and what this slave takes of it, and the one number on the card anybody
actually turns.

There was no control for it on the card. `RiskScaleForm` existed, fully
written, and was NEVER RENDERED: dead code. The only reachable field was
inside `StrategyForm`, two sections down, labelled "All strategies on this
slave", and hidden entirely while the selection mode was Accept none. For
a route-sized slave the card said outright that there was "nothing to edit
here", which was untrue of this value -- resolve_risk_scale() reads
slave_config whether or not the routes size themselves, so ibkr-etf-1's
scale was read on every signal and settable nowhere.

Chris, twice, the second time after I had reported it fixed: "there is no
field to adjust the the slave level risk scale. there is text claiming
whats staked, but no adjustment field still."

WHY A TEST READS A .jsx FILE, which nothing else in this suite does: the
defect was not a wrong behaviour, it was a component that was never
mounted. Nothing type-checks that, the UI has no test runner, and the
build succeeds happily either way -- `vite build` compiled this file
unchanged for however long the form sat unreachable. A string check is
crude; it is also the only thing here that would have failed.
"""
import re
from pathlib import Path

import pytest

SETTINGS = (Path(__file__).resolve().parents[2]
            / "ui" / "src" / "pages" / "Settings.jsx")


@pytest.fixture(scope="module")
def source():
    if not SETTINGS.exists():
        pytest.skip(f"no UI checkout at {SETTINGS}")
    return SETTINGS.read_text(encoding="utf-8")


def test_the_risk_scale_form_is_actually_rendered(source):
    """Defined is not rendered. This is the whole bug."""
    assert "function RiskScaleForm" in source, "the form is gone"
    assert "<RiskScaleForm" in source, (
        "RiskScaleForm is defined and never mounted, so the number the card "
        "leads with still has no control"
    )


def test_every_form_on_the_settings_page_is_mounted_somewhere(source):
    """Generalised from the defect rather than pinned to it. A form that
    is written, reviewed and never rendered looks finished in the diff and
    is absent from the page."""
    defined = set(re.findall(r"^function (\w+Form)\b", source, re.M))
    assert defined, "no forms found at all -- the pattern must have changed"
    orphans = sorted(name for name in defined if f"<{name}" not in source)
    assert orphans == [], f"defined but never rendered: {orphans}"
    # It found two more the moment it was written, which is why it is
    # worth keeping: RiskBasisForm (balance_adjustment and the sizing
    # floor, both printed on this very card and settable nowhere) and
    # MagicFilterForm, superseded by StrategyForm and since deleted.
    assert len(defined) >= 5, (
        "suspiciously few forms matched; the regex has probably stopped "
        "matching rather than the page having shrunk"
    )


def test_the_control_sits_on_the_risk_card_not_inside_strategy_selection(source):
    """It has to be reachable from the line that names it. Being somewhere
    on the page was already true and was not enough."""
    assert re.search(r'muted small">Risk calibration<', source), (
        "no Risk calibration row on the card"
    )
    # Ordered: the row belongs above the strategy selection it used to hide
    # inside, because it is read off the headline at the top.
    assert source.index('muted small">Risk calibration<') < source.index(
        'muted small">Strategy selection<'
    )


def test_the_slave_wide_scale_has_exactly_one_editor(source):
    """Two editors of one key is a clobbering hazger, not redundancy: each
    PATCHes the whole config from the snapshot it loaded on open, so
    saving the older form silently restores the value the other changed."""
    writes = len(re.findall(r"config\.risk_scale = ", source))
    assert writes == 1, (
        f"{writes} places write config.risk_scale; one of them will undo "
        f"the other"
    )


def test_a_route_sized_slave_is_told_what_is_still_slave_level(source):
    """ibkr-etf-1 sizes every route individually and still reads
    risk_scale off the slave. The card told it there was nothing to edit,
    which sent the one person who needed the field looking everywhere
    else.

    Asserted on the paragraph's new wording rather than on the absence of
    the old sentence: the first version of this test searched the whole
    file for that sentence and failed on the COMMENT above the fix, which
    quotes it. A string search cannot tell rendered text from a comment,
    so it has to look for what should be there instead of what should
    not."""
    assert re.search(r"no sizing block to edit here", source), (
        "the paragraph for a route-sized slave no longer distinguishes "
        "sizing, which really is per route, from risk scale, which is not"
    )
    paragraph = source[source.index("no sizing block to edit here"):][:600]
    assert "Risk <em>scale</em>" in paragraph, (
        "it says what cannot be edited without saying what still can"
    )


def test_the_headline_still_names_the_thing_the_field_sets(source):
    """If the wording drifts apart again, the field stops being findable
    from the text -- which is how this started.

    Both strings moved from "risk scale" to "risk calibration" on
    2026-11-15, Chris's term: the number is not a size but the ratio
    between what the EA signalled and what this slave takes of it."""
    assert "risk calibration x" in source, (
        "the headline no longer says risk calibration"
    )
    assert re.search(r"Risk calibration &mdash; this slave takes", source), (
        "the field is no longer labelled in the headline's own words"
    )


def test_the_card_does_not_describe_the_dials_as_replacing_each_other(source):
    """They MULTIPLY -- slave intake x per-strategy -- and the labels said
    the opposite for about ten minutes on 2026-11-15, because they were
    written against the override behaviour that had just been replaced.

    A wrong word here is a real mis-sizing: someone reading "replaces"
    sets a strategy to 0.50 expecting half of the signal and gets a
    quarter of it."""
    assert "replaces the default" not in source
    assert "multiplied by it" not in source, (
        "the side note still claims the per-strategy figure is NOT "
        "multiplied by the intake one, which is the old behaviour"
    )
    assert "<strong>multiplies</strong> the intake figure" in source, (
        "the side note no longer states that the dials compose"
    )
    # The worked example is the clearest statement of the model, so it is
    # pinned rather than left to drift.
    assert "a quarter of the original signal" in source
