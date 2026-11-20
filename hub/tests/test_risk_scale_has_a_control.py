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
    on the page was already true and was not enough.

    MATCHED ON THE LABEL, NOT ON ITS CSS CLASS. This read
    `muted small">Risk calibration<` until 2026-11-18, when every row
    label on the card moved to one shared style -- so it broke on a
    change that moved no control anywhere, which is not the thing it is
    here to catch. The ordering half went with it: the row it compared
    against, "Strategy selection", is now a control in the card's head,
    where it is above the whole body by construction.

    What replaces it is the property the name actually claims: the
    editor is mounted BY THE CARD and not from inside the strategy
    editor, which is where it used to hide."""
    assert re.search(r'>Risk calibration<', source), (
        "no Risk calibration row on the card"
    )
    card = _body(source, "SlaveRisk")
    assert "<RiskScaleForm" in card, (
        "the risk calibration editor is not mounted by the card that "
        "leads with the figure"
    )
    assert "<RiskScaleForm" not in _body(source, "StrategyForm"), (
        "the calibration editor is back inside the strategy selection, "
        "which is where it was unreachable from the line naming it"
    )


def _body(source: str, function: str) -> str:
    """One top-level function's source, by name."""
    start = source.index(f"function {function}(")
    rest = source[start + 1:]
    end = re.search(r"^function \w+\(", rest, re.M)
    return rest if end is None else rest[: end.start()]


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
    # Anchored on JSX MARKUP, which can only be rendered text. Plain
    # prose matched the explanatory comment above the code instead --
    # twice now, and this test's own docstring warns about exactly that.
    # <em> cannot appear in a // comment and mean anything.
    assert "Risk <em>sizing</em>" in source and "Risk <em>calibration</em>" in source, (
        "the paragraph for a route-sized slave no longer distinguishes "
        "sizing, which really is per route, from risk calibration, which "
        "is read off the slave either way"
    )
    para = source[source.index("Risk <em>sizing</em>"):][:420]
    assert "Risk <em>calibration</em>" in para, (
        "it says what cannot be edited without saying what still can"
    )


def test_a_route_sized_slave_can_edit_its_cap(source):
    """The stronger requirement, and the one actually asked for.

    Saying "sizing is per route" was accurate and useless: ibkr-etf-1's
    1.5% ceiling is real, lives on each of twenty routes, and was
    editable NOWHERE -- the routes are listed read-only, and the card's
    own Edit button is correctly hidden because a slave-level block would
    be saved and never read. Chris: "I cant adjust the max cap. that
    initial edit tab, top right, is gone on that slave."

    So the editor writes every route at once, which is what a slave-wide
    figure means on a slave that sizes per route."""
    assert "function RouteSizingForm" in source
    assert "<RouteSizingForm" in source, "written and never mounted again"
    # On the label, not on its CSS class: the labels on this card moved
    # to one shared style on 2026-11-18 and this broke without any
    # control having moved.
    assert re.search(r'>Sizing on every route<', source), (
        "no row to reach the editor from"
    )
    # It must say how many routes it is about to rewrite. A bulk edit
    # that does not state its blast radius is the kind that gets clicked.
    assert "Save to all ${count} routes" in source


def test_the_bulk_editor_re_reads_before_writing(source):
    """Same lost-update hazard as every other form here, and worse: this
    one rewrites twenty routes from a snapshot. It must not restore a
    config changed since the form was opened."""
    body = source[source.index("function RouteSizingForm"):]
    body = body[:body.index("function MasterFilterForm")]
    assert "await currentConfig(slave.id)" in body
    assert "full.config" not in body, (
        "it builds the patch from the snapshot it loaded on open"
    )


def test_the_bulk_editor_leaves_routes_that_size_by_inheritance_alone(source):
    """A route with no sizing block is inheriting on purpose. Giving it
    one would change how it sizes as a side effect of editing others."""
    body = source[source.index("function RouteSizingForm"):]
    body = body[:body.index("function MasterFilterForm")]
    assert "if (!r.sizing) return r;" in body


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
