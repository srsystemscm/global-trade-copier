"""The two files that pin this hub's dependencies must agree.

`requirements.txt` is what a hub installs; `constraints.txt` is the full
transitive set CI installs against. The nine direct dependencies appear in
both, which is redundant and therefore able to drift -- and a drift here
is the specific failure these files exist to prevent: CI would go green
against one set of versions while the VPS ran another.

So the redundancy is checked rather than tolerated.
"""
import re
from pathlib import Path

import pytest

HUB = Path(__file__).resolve().parent.parent
REQUIREMENTS = HUB / "requirements.txt"
CONSTRAINTS = HUB / "constraints.txt"
DEV = HUB / "requirements-dev.txt"

# name[extras]==version, which is the only form allowed in requirements.txt.
PINNED = re.compile(r"^([A-Za-z0-9._-]+)\s*(\[[^\]]*\])?\s*==\s*([^\s;#]+)")


def normalise(name: str) -> str:
    """PEP 503: `ib_async`, `ib-async` and `IB_Async` are one package.

    pip freeze writes whichever spelling the package declares -- this file
    holds `PyYAML`, `pydantic_core` and `ib_async` side by side -- so
    comparing the raw strings would report drift that does not exist.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(path: Path) -> dict:
    pins = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = PINNED.match(line)
        if match:
            pins[normalise(match.group(1))] = match.group(3)
    return pins


def test_every_runtime_dependency_is_pinned():
    """Not `>=`. An upgrade has to be a deliberate act, not whatever pip
    found newest on the day somebody reinstalled."""
    unpinned = []
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not PINNED.match(line):
            unpinned.append(line)
    assert unpinned == [], (
        f"requirements.txt must pin with ==, found: {unpinned}"
    )


def test_the_pins_agree_with_the_constraints():
    """Otherwise CI goes green against one set of versions while the hub
    runs another, which is the whole failure these files prevent."""
    requirements = parse(REQUIREMENTS)
    constraints = parse(CONSTRAINTS)
    assert requirements, "nothing parsed out of requirements.txt"

    missing = sorted(set(requirements) - set(constraints))
    assert missing == [], (
        f"pinned in requirements.txt but absent from constraints.txt: "
        f"{missing}. Regenerate it: "
        f"python -m pip freeze > constraints.txt"
    )

    disagree = {
        name: (requirements[name], constraints[name])
        for name in sorted(requirements)
        if requirements[name] != constraints[name]
    }
    assert disagree == {}, (
        f"requirements.txt and constraints.txt disagree (requirement, "
        f"constraint): {disagree}"
    )


def test_the_dev_dependencies_are_bounded():
    """Test-only packages may float within a major version, but not past
    one: a major bump is where a test runner changes behaviour, and that
    should be chosen rather than arrive."""
    lines = [
        line.strip() for line in DEV.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert lines, "nothing declared in requirements-dev.txt"
    for line in lines:
        assert "<" in line, (
            f"{line!r} has no upper bound; a major version bump would "
            f"arrive on its own"
        )


def test_the_ibkr_library_stays_out_of_the_runtime_requirements():
    """The adapter lazy-imports it so a hub with no IBKR slave need not
    install it. The suite does need it, which is what requirements-dev.txt
    is for -- the two lists are deliberately different."""
    runtime = parse(REQUIREMENTS)
    assert "ib-async" not in runtime
    assert "ib-async" in parse_dev_names(), (
        "the suite builds a real IBKRAdapter, so ib_async has to be "
        "declared somewhere"
    )


def parse_dev_names() -> set:
    names = set()
    for raw in DEV.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z0-9._-]+)", line)
        if match:
            names.add(normalise(match.group(1)))
    return names


@pytest.mark.parametrize("path", [REQUIREMENTS, CONSTRAINTS, DEV])
def test_the_files_exist_and_say_why(path):
    """Each one explains what it is for. Three files pinning overlapping
    sets of versions is confusing enough to need it."""
    text = path.read_text(encoding="utf-8")
    assert text.lstrip().startswith("#"), f"{path.name} has no header comment"
