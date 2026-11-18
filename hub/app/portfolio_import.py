"""A portfolio from the Portfolio Manager, turned into a slave's selection.

Which strategies a slave takes, and what share of each EA's signalled risk
it takes, are two halves of one decision -- and both were typed in by hand,
magic number by magic number. Chris, 2026-11-18: "I dont want to have to
(if possible) introduce a manual, error prone step like adding magic
numbers and their risk scale by hand."

PM already owns that answer. This reads the file it exports and writes the
two keys the engine already reads: `magic_filter`, which decides whether a
signal reaches the slave at all, and `risk_scale_by_magic`, which decides
what it stakes when it does. Neither is new, so an import needs no restart.

The contract is `docs/portfolio-import.md`, and it is the document PM is
written against. Everything here is a pure function over dicts: `parse`
reads a file, `plan` says what applying it would change, and `apply`
returns a new config. The route does the IO.

Nothing here fetches anything. The copier never calls PM -- the reporter is
push-only, which is what lets this run behind a VPS with no inbound route,
and a copier that pulled its own risk configuration from a web service
would have the market open while a failed GET decided which strategies it
was copying.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

SCHEMA_PREFIX = "gtc.portfolio-import/"
SUPPORTED_MAJOR = 1

# Ten times what the EA staked is not a calibration, it is a typo or a unit
# mistake -- 50 meaning 50%. A genuine figure above this can still be set by
# hand, where a human is looking at that one number rather than at a file of
# forty.
MAX_RISK_SCALE = 10.0


class PortfolioImportError(ValueError):
    """The file cannot be applied, and nothing has been written."""


@dataclass(frozen=True)
class Strategy:
    magic: int
    risk_scale: float
    name: Optional[str] = None
    account: Optional[str] = None


@dataclass(frozen=True)
class Portfolio:
    id: str
    name: Optional[str] = None
    revision: Any = None
    exported_at: Optional[str] = None
    strategies: List[Strategy] = field(default_factory=list)

    @property
    def magics(self) -> List[int]:
        return [s.magic for s in self.strategies]


def parse(document: Any) -> Portfolio:
    """Read an export, or raise PortfolioImportError saying what is wrong.

    Unknown fields are ignored at every level: PM adds fields for its own
    reasons and this has no business failing on one. What is checked is
    only what gets written.
    """
    if not isinstance(document, dict):
        raise PortfolioImportError(
            "a portfolio export is a JSON object; this file is a "
            f"{type(document).__name__}")

    _check_schema(document.get("schema"))

    meta = document.get("portfolio")
    if not isinstance(meta, dict) or not str(meta.get("id") or "").strip():
        raise PortfolioImportError(
            "the file does not say which portfolio it is: "
            "`portfolio.id` is required")

    raw = document.get("strategies")
    if not isinstance(raw, list) or not raw:
        # An empty selection and no selection are opposites that read
        # identically as a boolean -- the reasoning that already refuses
        # `{"allow": []}`. An empty list is what an export looks like when
        # something went wrong producing it.
        raise PortfolioImportError(
            "this portfolio lists no strategies. An empty selection is not "
            "the same as taking everything, so it is refused rather than "
            "written: a slave given one would silently stop trading")

    strategies: List[Strategy] = []
    seen: Dict[int, Strategy] = {}
    for index, entry in enumerate(raw):
        parsed = _strategy(entry, index)
        earlier = seen.get(parsed.magic)
        if earlier is not None:
            # Which risk_scale wins is not a thing to guess at.
            if earlier.risk_scale != parsed.risk_scale:
                raise PortfolioImportError(
                    f"magic {parsed.magic} appears twice with different risk "
                    f"scales ({earlier.risk_scale:g} and "
                    f"{parsed.risk_scale:g})")
            # The same magic on two masters. magic_filter and
            # risk_scale_by_magic are keyed by magic ALONE -- they have no
            # master dimension, unlike `routes`, which has its own
            # `account` -- so there is no way to apply this and no way to
            # pick one. Better found here than the first evening both
            # masters signal at once.
            if (earlier.account or None) != (parsed.account or None):
                raise PortfolioImportError(
                    f"magic {parsed.magic} is listed for two masters "
                    f"({earlier.account!r} and {parsed.account!r}). A slave's "
                    f"strategy selection and its per-strategy risk scales are "
                    f"keyed by magic number alone, so the two cannot be told "
                    f"apart once written")
            continue
        seen[parsed.magic] = parsed
        strategies.append(parsed)

    return Portfolio(
        id=str(meta["id"]).strip(),
        name=_text(meta.get("name")),
        revision=meta.get("revision"),
        exported_at=_text(meta.get("exported_at")),
        strategies=strategies,
    )


def _check_schema(value: Any) -> None:
    text = str(value or "")
    if not text.startswith(SCHEMA_PREFIX):
        raise PortfolioImportError(
            f"this does not look like a portfolio export: `schema` is "
            f"{value!r}, expected something starting {SCHEMA_PREFIX!r}")
    version = text[len(SCHEMA_PREFIX):]
    try:
        major = int(version.split(".", 1)[0])
    except ValueError:
        raise PortfolioImportError(
            f"cannot read a version out of schema {text!r}") from None
    if major != SUPPORTED_MAJOR:
        # A later major may mean something different by a field this
        # version recognises, which is worse than not reading it at all.
        raise PortfolioImportError(
            f"this copier reads {SCHEMA_PREFIX}{SUPPORTED_MAJOR}.x and the "
            f"file is {text}")


def _strategy(entry: Any, index: int) -> Strategy:
    where = f"strategies[{index}]"
    if not isinstance(entry, dict):
        raise PortfolioImportError(f"{where} is not an object")

    raw_magic = entry.get("magic")
    # bool is an int in Python, and True would read as magic 1.
    if isinstance(raw_magic, bool) or not isinstance(raw_magic, int):
        if isinstance(raw_magic, str) and raw_magic.strip().lstrip("-").isdigit():
            raw_magic = int(raw_magic.strip())
        else:
            raise PortfolioImportError(
                f"{where} has magic {entry.get('magic')!r}, which is not a "
                f"whole number. 0 IS a real magic -- a trade placed by hand "
                f"-- so it is only the unreadable ones that are refused")

    if "risk_scale" not in entry or entry["risk_scale"] is None:
        raise PortfolioImportError(
            f"{where} (magic {raw_magic}) has no risk_scale. The file has to "
            f"say what share of this EA's signal the slave takes; 1 is a "
            f"value, not a default to assume on its behalf")
    try:
        scale = float(entry["risk_scale"])
    except (TypeError, ValueError):
        raise PortfolioImportError(
            f"{where} (magic {raw_magic}) has risk_scale "
            f"{entry['risk_scale']!r}, which is not a number") from None

    if scale < 0:
        # Not a smaller position: an inverted one.
        raise PortfolioImportError(
            f"{where} (magic {raw_magic}) has a negative risk_scale "
            f"({scale:g})")
    if scale > MAX_RISK_SCALE:
        raise PortfolioImportError(
            f"{where} (magic {raw_magic}) has risk_scale {scale:g}. Anything "
            f"above {MAX_RISK_SCALE:g} is a typo or a unit mistake far more "
            f"often than it is a decision -- 50 meaning 50% -- so an import "
            f"will not write it. Set it by hand if it is meant")

    name = entry.get("name") or entry.get("strategy") or entry.get("label")
    account = entry.get("account")
    return Strategy(
        magic=raw_magic,
        risk_scale=scale,
        name=_text(name),
        account=None if account is None else str(account),
    )


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def source_block(portfolio: Portfolio, now: Optional[float] = None) -> Dict[str, Any]:
    """Where a selection came from, recorded and never acted on."""
    stamp = time.gmtime(now if now is not None else time.time())
    block: Dict[str, Any] = {
        "kind": "portfolio_manager",
        "portfolio_id": portfolio.id,
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", stamp),
    }
    if portfolio.name is not None:
        block["portfolio"] = portfolio.name
    if portfolio.revision is not None:
        block["revision"] = portfolio.revision
    if portfolio.exported_at is not None:
        block["exported_at"] = portfolio.exported_at
    return block


def apply(config: Mapping[str, Any], portfolio: Portfolio,
          now: Optional[float] = None) -> Dict[str, Any]:
    """The slave's config with this portfolio's selection on it.

    READ-MODIFY-WRITE, because PATCH /slaves/{id} replaces config_json
    wholesale: every key this import does not own -- sizing, routes,
    masters, the roll, the splitter, the chase -- is carried across exactly
    as it was.

    The selection is REPLACED, not merged. A magic the file does not
    mention is one the slave stops taking, which is the only reading of
    "this is the portfolio" that cannot quietly leave a strategy running
    after it was taken out of the book.
    """
    out = dict(config)
    out["magic_filter"] = {
        "allow": [_allow_entry(s, portfolio) for s in portfolio.strategies],
        "source": source_block(portfolio, now),
    }
    # Written for every strategy, 1.0 included: the file is the record of
    # what was decided, and an absent key would make "unscaled on purpose"
    # indistinguishable from "never mentioned".
    out["risk_scale_by_magic"] = {
        str(s.magic): s.risk_scale for s in portfolio.strategies
    }
    return out


def _allow_entry(strategy: Strategy, portfolio: Portfolio) -> Dict[str, Any]:
    entry: Dict[str, Any] = {"magic": strategy.magic}
    if strategy.name is not None:
        entry["strategy"] = strategy.name
    if portfolio.name is not None:
        entry["portfolio"] = portfolio.name
    # Recorded per entry as well as checked above, so a later version that
    # does key by master has the answer already stored rather than having
    # to ask for the file again.
    if strategy.account is not None:
        entry["account"] = strategy.account
    return entry


def plan(config: Mapping[str, Any], portfolio: Portfolio) -> Dict[str, Any]:
    """What applying this would change, for someone about to decide.

    Returned for a dry run and again for the real thing, so what was
    approved and what happened are the same words.
    """
    before_scales = _scales(config.get("risk_scale_by_magic"))
    before_magics = _selected_magics(config)
    after_magics = set(portfolio.magics)

    changed: List[Dict[str, Any]] = []
    unchanged = 0
    for s in portfolio.strategies:
        was = before_scales.get(s.magic)
        if was is None:
            if s.risk_scale != 1.0:
                changed.append({"magic": s.magic, "from": None, "to": s.risk_scale})
            else:
                unchanged += 1
        elif was != s.risk_scale:
            changed.append({"magic": s.magic, "from": was, "to": s.risk_scale})
        else:
            unchanged += 1

    return {
        "portfolio": {
            "id": portfolio.id,
            "name": portfolio.name,
            "revision": portfolio.revision,
        },
        "strategies": len(portfolio.strategies),
        "magics_added": sorted(after_magics - before_magics)
        if before_magics is not None else sorted(after_magics),
        "magics_removed": sorted(before_magics - after_magics)
        if before_magics is not None else [],
        # None means the slave takes every strategy today, which is not the
        # same as taking none of them -- and an import narrows it to a list
        # for the first time, which is the largest change this can make.
        "narrows_from_every_magic": before_magics is None,
        "scales_changed": changed,
        "unchanged": unchanged,
        "replaces_import": _previous_import(config),
    }


def _selected_magics(config: Mapping[str, Any]):
    """The magics this slave takes today, or None for 'every strategy'."""
    from app import magic_filter

    try:
        selection = magic_filter.parse(config)
    except ValueError:
        # A selection that will not parse is refused at PATCH time, so this
        # is a config written before that check or edited by hand. Treating
        # it as "everything" would understate what the import changes.
        return set()
    if selection.accept_none:
        return set()
    if selection.allow is None:
        return None
    return set(selection.allow)


def _scales(raw: Any) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for key, value in (raw or {}).items():
        try:
            out[int(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return out


def _previous_import(config: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The PM import already on this slave, if there is one.

    So that re-importing the same portfolio at a newer revision is visibly
    that, and dropping a different portfolio on top of one is visibly that.
    """
    block = config.get("magic_filter")
    if not isinstance(block, dict):
        return None
    source = block.get("source")
    if not isinstance(source, dict) or source.get("kind") != "portfolio_manager":
        return None
    return {
        "portfolio_id": source.get("portfolio_id"),
        "portfolio": source.get("portfolio"),
        "revision": source.get("revision"),
        "imported_at": source.get("imported_at"),
    }
