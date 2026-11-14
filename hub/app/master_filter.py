"""Which MASTERS a slave takes signals from.

Until now the bus fanned every signal to every slave queue and filtered on
magic alone. That is right with one master and wrong with several, for a
reason that is not obvious: **magic numbers are per-master and they will
collide.** Each fxDreema project numbers from MagicStart = 1001, so two
masters each running their own first strategy both stamp 1001. To every
slave those are the same strategy. A per-EA risk scale set on 1001 would
quietly apply to both, and an allow-list naming 1001 would take both.

So the account has to be selectable before the magic means anything. This
is deliberately the same shape as `magic_filter` -- allow-list, deny-list,
or neither -- so there is one idea to learn rather than two.

Account numbers are strings here, never integers. They are identifiers
rather than quantities, they are compared and never arithmetic, and MT5
account numbers are long enough that a JSON number is a precision question
nobody should have to think about.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Mapping, Optional, Tuple

from app.models import Signal

logger = logging.getLogger(__name__)

KEY = "master_filter"


def _accounts(raw: Any, where: str, labels: Dict[str, str]) -> FrozenSet[str]:
    """Read a list of account numbers, which may carry labels.

    Accepts ["44964870"], [44964870] and
    [{"account": "44964870", "label": "ICM main"}] -- the last because the
    hub's own registry names accounts, and a number on screen is worse than
    a name when there are eight of them.
    """
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError(f"{KEY}.{where} is {raw!r}; it should be a list of account numbers")

    out = set()
    for entry in raw:
        if isinstance(entry, dict):
            account = entry.get("account")
            if account is None:
                raise ValueError(f"{KEY}.{where} has an entry with no `account`: {entry!r}")
            label = entry.get("label")
            if label:
                labels[str(account)] = str(label)
        else:
            account = entry
        text = str(account).strip()
        if not text:
            raise ValueError(f"{KEY}.{where} contains an empty account number")
        out.add(text)
    return frozenset(out)


@dataclass(frozen=True)
class MasterFilter:
    """An allow-list, a deny-list, or neither."""

    # None is not an empty set. None means no allow-list was configured and
    # every master passes; an empty one would mean none does. Read as a
    # boolean the two look identical, which is how `{"allow": []}` could
    # come to pass every signal it was written to stop.
    allow: Optional[FrozenSet[str]] = None
    deny: FrozenSet[str] = frozenset()
    labels: Mapping[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return self.allow is not None or bool(self.deny)

    def accepts(self, signal: Signal) -> Tuple[bool, Optional[str]]:
        """Whether this slave takes the signal, and if not, why not."""
        if not self.configured:
            return True, None

        account = None if signal.account is None else str(signal.account)
        if not account:
            # Signal.account is never None in practice -- the bus refuses a
            # signal without one, because two masters can issue the same
            # ticket number. Handled anyway: a slave that has named its
            # masters has not named "unknown".
            if self.allow is None:
                return True, None
            return False, (
                "the signal carries no master account, and this slave has an "
                "explicit list of the masters it takes"
            )

        if account in self.deny:
            return False, f"master {self._named(account)} is on this slave's deny-list"
        if self.allow is not None and account not in self.allow:
            return False, (
                f"master {self._named(account)} is not among the "
                f"{len(self.allow)} this slave takes"
            )
        return True, None

    def _named(self, account: str) -> str:
        label = self.labels.get(account)
        return f"{account} ({label})" if label else account

    def describe(self) -> str:
        if not self.configured:
            return "every master"
        parts = []
        if self.allow is not None:
            parts.append(f"allow [{', '.join(self._named(a) for a in sorted(self.allow))}]")
        if self.deny:
            parts.append(f"deny [{', '.join(self._named(a) for a in sorted(self.deny))}]")
        return ", ".join(parts)

    def as_dict(self) -> Optional[Dict[str, Any]]:
        if not self.configured:
            return None
        out: Dict[str, Any] = {}
        if self.allow is not None:
            out["allow"] = sorted(self.allow)
        if self.deny:
            out["deny"] = sorted(self.deny)
        if self.labels:
            out["labels"] = dict(self.labels)
        return out


EVERY_MASTER = MasterFilter()


def parse(slave_config: Optional[Dict[str, Any]]) -> MasterFilter:
    """Read `master_filter` out of a slave's config.

    The terse form is a bare list, meaning an allow-list:
    `"master_filter": ["44964870"]`.
    """
    raw = (slave_config or {}).get(KEY)
    if raw is None:
        return EVERY_MASTER

    labels: Dict[str, str] = {}
    if isinstance(raw, dict):
        allow_raw = raw.get("allow")
        allow = None if allow_raw is None else _accounts(allow_raw, "allow", labels)
        deny_raw = raw.get("deny")
        deny = frozenset() if deny_raw is None else _accounts(deny_raw, "deny", labels)

        # A key this does not know is a typo, and a typo in an allow-list
        # is a slave that silently stops trading. Refuse rather than guess.
        unknown = set(raw) - {"allow", "deny", "labels"}
        if unknown:
            raise ValueError(
                f"{KEY} has unrecognised key(s) {sorted(unknown)}; "
                f"expected allow, deny or labels"
            )
        for account, label in (raw.get("labels") or {}).items():
            labels.setdefault(str(account), str(label))
    else:
        allow = _accounts(raw, "allow", labels)
        deny = frozenset()

    if allow is not None and not allow:
        raise ValueError(
            f"{KEY}.allow is empty, which would stop every signal. Remove the "
            f"key to take every master, or name the ones this slave takes."
        )

    overlap = allow & deny if allow is not None else frozenset()
    if overlap:
        raise ValueError(
            f"{KEY} lists master(s) {sorted(overlap)} in both allow and deny; "
            f"deny wins, so they would never trade -- say which is meant"
        )

    return MasterFilter(allow=allow, deny=deny, labels=labels)
