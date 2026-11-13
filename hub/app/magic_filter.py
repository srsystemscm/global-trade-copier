"""Which strategies a slave takes, decided per slave by magic number.

One master terminal can run thirty or forty origin EAs. They all publish
down the same PUB socket, because an EA has no idea which slaves exist --
and it must not: putting the selection in the EA would mean editing forty
charts every time a slave changes, and a slave's own business is not
something a master should have to know.

So the selection lives on the slave, here, and is applied once in the
Signal Bus's fan-out rather than in each engine. Two reasons it belongs
there and not in the engines:

- `signal_received.targets` is reported to the Operations hub as "how many
  slaves this signal fanned to". Filtered in the engines, that number
  would count every registered slave and be wrong for every signal.
- a signal a slave is not configured to take should not reach its queue at
  all. Dropped later it is indistinguishable, in a log, from one the
  engine refused for a reason that matters.

`routes` on an autonomous slave does something related but narrower: it
says which instrument a given (symbol, magic) becomes. This says whether
the slave hears the signal in the first place, and it works for mirror
slaves too, which have no routes.

Configuration, in the slave's config:

    "magic_filter": [1001, 1002, 1003]

    "magic_filter": {"allow": [1001, 1002, 1003], "deny": [1002]}

The terse form is an allow-list, which is the common case. `deny` is
applied after `allow`, so a block always wins -- that way one magic can be
taken out of service without editing the list it belongs to.

A slave with no `magic_filter` takes everything, which is what every slave
does today.

Importing a selection
---------------------

The Operations hub's registry -- its portfolio manager -- is where a magic
number is given a strategy name and a portfolio, many-to-many, with CSV
import and export. That makes it the authority on "which magics are the
trend-following book", and a selection maintained by hand here is a second
copy of an answer that already exists somewhere better.

So an entry may be an OBJECT carrying whatever the registry knows, and the
copier reads `magic` and keeps the rest:

    "magic_filter": {
      "allow": [
        {"magic": 1001, "strategy": "US2000 trend", "portfolio": "Trend"},
        {"magic": 1002, "strategy": "GC breakout",  "portfolio": "Trend"}
      ],
      "source": {"kind": "ops_hub_registry", "portfolio": "Trend",
                 "imported_at": "2026-11-12T20:05:02Z", "revision": 7}
    }

An export can therefore be dropped in verbatim, and the names come with
it: they end up in the log line that says what a slave takes, and in the
snapshot the copier reports, so the hub can show its own strategy names
beside each slave without the copier resolving anything.

`source` is recorded and never acted on. The copier does not fetch it --
it has no idea what a portfolio is, and it must not: the reporter is push
only, and a copier that pulled its own risk configuration from a web
service would have the market open while a failed GET decided which
strategies it was copying. Resolving a portfolio to a set of magics is an
offline step that writes this block through PATCH /slaves, which already
takes effect without a restart.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from app.models import Signal

logger = logging.getLogger(__name__)

KEY = "magic_filter"


@dataclass(frozen=True)
class MagicFilter:
    """An allow-list, a deny-list, or neither."""

    # None is not the same as an empty set: None means no allow-list was
    # configured and everything passes, while an empty one would mean
    # nothing does. Read as a boolean the two are identical, which is how a
    # filter configured as `{"allow": []}` could come to pass every signal
    # it was written to stop.
    allow: Optional[FrozenSet[int]] = None
    deny: FrozenSet[int] = frozenset()
    # Whatever an imported entry said each magic was, by magic number.
    # Never used to decide anything -- it exists so a log line and the
    # Operations hub's Copier tab can say "US2000 trend" instead of 1001.
    labels: Mapping[int, str] = field(default_factory=dict)
    # Where an imported selection came from, recorded verbatim and never
    # acted on. See the module docstring.
    source: Optional[Mapping[str, Any]] = None

    @property
    def configured(self) -> bool:
        return self.allow is not None or bool(self.deny)

    def accepts(self, signal: Signal) -> Tuple[bool, Optional[str]]:
        """Whether this slave takes the signal, and if not, why not.

        The reason is returned rather than logged here so the caller can
        decide how loud it is: for a signal a slave was never meant to see,
        the answer is "not at all".
        """
        if not self.configured:
            return True, None

        magic = signal.magic
        if magic is None:
            # Pre-1.02 bridge EAs send no magic at all. A slave with an
            # allow-list has stated which strategies it takes, and "cannot
            # tell" is not one of them -- the same reasoning as strict
            # `routes`, where an unrecognised strategy landing on a known
            # strategy's contract is the worst available outcome. A slave
            # with only a deny-list still takes it: nothing was excluded.
            if self.allow is None:
                return True, None
            return False, (
                "the signal carries no magic number (a bridge EA older than "
                "1.02?) and this slave has an explicit allow-list, so there "
                "is no way to tell whether it is one of the strategies this "
                "slave takes"
            )

        if magic in self.deny:
            return False, f"magic {magic} is on this slave's deny-list"
        if self.allow is not None and magic not in self.allow:
            return False, (
                f"magic {magic} is not among the "
                f"{len(self.allow)} this slave takes"
            )
        return True, None

    def _named(self, magics: FrozenSet[int]) -> str:
        return ", ".join(
            f"{m} ({self.labels[m]})" if m in self.labels else str(m)
            for m in sorted(magics)
        )

    def describe(self) -> str:
        if not self.configured:
            return "every magic"
        parts = []
        if self.allow is not None:
            parts.append(f"allow [{self._named(self.allow)}]")
        if self.deny:
            parts.append(f"deny [{self._named(self.deny)}]")
        if self.source:
            parts.append(f"from {dict(self.source)}")
        return ", ".join(parts)

    def as_dict(self) -> Optional[Dict[str, Any]]:
        """The selection as the Operations hub is told about it.

        None when nothing is configured, which is "this slave takes every
        strategy" -- not the same as an empty selection, and the hub has to
        be able to tell those apart.
        """
        if not self.configured:
            return None
        out: Dict[str, Any] = {
            "allow": None if self.allow is None else sorted(self.allow),
            "deny": sorted(self.deny),
        }
        if self.labels:
            out["labels"] = {str(m): n for m, n in sorted(self.labels.items())}
        if self.source:
            out["source"] = dict(self.source)
        return out


ALL = MagicFilter()


_LABEL_KEYS = ("strategy", "name", "label")


def _magics(raw: Any, where: str, labels: Dict[int, str]) -> FrozenSet[int]:
    """A set of magic numbers from whatever the config says.

    An entry is a number, or an object carrying one under `magic` -- which
    is what lets a registry export be pasted in with its strategy names
    attached. Any name it brings is collected into `labels` and otherwise
    ignored; so is every other key, because this has no business failing on
    a field the portfolio manager added for its own reasons.

    Raises rather than skipping a value it cannot read. A filter that
    quietly dropped an unparseable entry would be WIDER than the one that
    was written, which is the wrong direction for a rule whose whole job is
    to keep a strategy off a slave. Raised here, the slave fails to start
    and says why -- see start_engines(), which keeps every other slave
    running.
    """
    if isinstance(raw, (str, bytes, dict)) or not hasattr(raw, "__iter__"):
        raw = [raw]
    out = set()
    for value in raw:
        label = None
        if isinstance(value, dict):
            if "magic" not in value:
                raise ValueError(
                    f"{KEY}.{where} contains {value!r}, an entry with no "
                    f"`magic` key. An imported entry may carry anything "
                    f"else it likes, but it has to say which magic it is."
                )

            label = next(
                (str(value[k]) for k in _LABEL_KEYS if value.get(k)), None)
            value = value["magic"]
        try:
            magic = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{KEY}.{where} contains {value!r}, which is not a magic "
                f"number. Refusing to start rather than running a filter "
                f"narrower or wider than the one configured."
            ) from exc
        out.add(magic)
        if label:
            labels[magic] = label
    return frozenset(out)


def parse(slave_config: Optional[Dict[str, Any]]) -> MagicFilter:
    """Read `magic_filter` out of a slave's config."""
    raw = (slave_config or {}).get(KEY)
    if raw is None:
        return ALL

    labels: Dict[int, str] = {}
    if isinstance(raw, dict):
        allow_raw = raw.get("allow")
        allow = None if allow_raw is None else _magics(allow_raw, "allow", labels)
        deny_raw = raw.get("deny")
        deny = frozenset() if deny_raw is None else _magics(deny_raw, "deny", labels)
        source = raw.get("source")
        if source is not None and not isinstance(source, dict):
            raise ValueError(
                f"{KEY}.source is {source!r}; it should be an object saying "
                f"where the selection was imported from"
            )
        # A key this does not know is a typo, and a typo in an allow-list
        # fails open -- which is the one direction this must not fail. The
        # exception is `source`, whose contents are nobody's business here.
        unknown = set(raw) - {"allow", "deny", "source"}
        if unknown:
            raise ValueError(
                f"{KEY} has unknown key(s) {sorted(unknown)}; it takes "
                f"`allow`, `deny` and `source`, or a bare list meaning "
                f"`allow`"
            )
        filt = MagicFilter(allow=allow, deny=deny, labels=labels, source=source)
    else:
        filt = MagicFilter(allow=_magics(raw, "allow", labels), labels=labels)

    if filt.allow is not None and not filt.allow:
        raise ValueError(
            f"{KEY} allows nothing, so this slave would never copy a "
            f"signal. Remove the key to take every magic, or disable the "
            f"slave if that is what was meant."
        )
    overlap = filt.allow & filt.deny if filt.allow is not None else frozenset()
    if overlap:
        # Harmless -- deny wins -- but it is far more likely to be a
        # mistake than an intention, and silence here means nobody finds
        # out which half of the config they got wrong.
        logger.warning(
            "%s lists magic %s under both allow and deny; the deny wins",
            KEY, sorted(overlap),
        )
    return filt
