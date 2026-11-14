"""What is true now, assembled on demand.

The snapshot is never queued and never acked: it is replaced by the next
one, so a lost one costs nothing. That is why it is built here, fresh, on
every report rather than accumulated anywhere.

Everything in it is read from state the copier already keeps -- the slaves
table, the engines list, the registry, the ZMQ receiver's heartbeats. None
of it is cached, because a cached snapshot is the one failure mode a
snapshot cannot have: saying a slave is connected after it stopped being.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app import __version__, magic_filter
from app.config import settings
from app.contract_specs import get_contract_spec
from app.db import db_cursor, get_config_value
from app.hub_reporter.catalogue import iso
from app.sizing import resolve_balance_adjustment
from app.slaves import SlaveConfig, list_all_slaves

logger = logging.getLogger(__name__)


class Counters:
    """received / copied / refused / failed, today and since this start.

    In memory, and deliberately so for `since_start`: the phrase means
    exactly what it says, and persisting it would make it mean something
    else. `today` resets with the process too, which is the honest limit of
    counting from an event stream rather than from storage -- the hub keeps
    the events themselves and can count a real day from them, which is why
    these are a convenience for the tab's header and not the record.
    """

    BUCKETS = ("received", "copied", "refused", "failed")

    def __init__(self) -> None:
        self._today = {b: 0 for b in self.BUCKETS}
        self._since_start = {b: 0 for b in self.BUCKETS}
        self._day = self._utc_day()

    @staticmethod
    def _utc_day() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def record(self, bucket: Optional[str]) -> None:
        if bucket not in self._today:
            return
        day = self._utc_day()
        if day != self._day:
            self._day = day
            self._today = {b: 0 for b in self.BUCKETS}
        self._today[bucket] += 1
        self._since_start[bucket] += 1

    def as_dict(self) -> Dict[str, Dict[str, int]]:
        # Rolled over on read as well as on write, so a day with no events
        # after midnight does not report yesterday's totals as today's.
        if self._utc_day() != self._day:
            self._day = self._utc_day()
            self._today = {b: 0 for b in self.BUCKETS}
        return {"today": dict(self._today), "since_start": dict(self._since_start)}


class SlaveFacts:
    """Per-slave detail the catalogue and the snapshot both need.

    `last_error` lives here because nothing in the copier keeps it: the
    engines log a failure and move on. The reporter sees every alert-class
    event as it passes, so it is the one place that can answer "what went
    wrong with this slave, and when" without a log file.
    """

    def __init__(self) -> None:
        self._modes: Dict[str, str] = {}
        self._units: Dict[str, str] = {}
        self._last_error: Dict[str, Dict[str, Any]] = {}
        # Learned from the live adapter each snapshot, because an IBKR
        # login can hold several accounts and an MT terminal reports its
        # own login -- both are resolved at connect, not configured.
        self._accounts: Dict[str, Optional[str]] = {}

    def mode(self, slave_id: Optional[str]) -> str:
        """mirror or autonomous, reading the slaves table if need be.

        An event can be translated before any snapshot has been built, so
        this cannot wait for `learn()` to have run: an unknown slave is
        looked up once and then cached, and every snapshot refreshes the
        cache so a slave added through POST /slaves is not stuck with the
        default it was given here.
        """
        key = slave_id or ""
        if key not in self._modes:
            self.learn(list_all_slaves())
            self._modes.setdefault(key, "mirror")
        return self._modes[key]

    def learn(self, slaves: List[SlaveConfig]) -> None:
        for slave in slaves:
            self._modes[slave.id] = slave.config.get("mode", "mirror")
            configured = slave.config.get("size_unit")
            if configured:
                self._units[slave.id] = str(configured)

    def note_account(self, slave_id: str, account: Optional[str]) -> None:
        """Remember what the adapter resolved, so a leg row can carry it.

        Only overwritten by a real answer: an adapter that has not yet
        connected reports None, and forgetting a number already learned
        would make a leg unjoinable for as long as the terminal is shut.
        """
        if account:
            self._accounts[slave_id] = str(account)

    def slave_account(self, slave_id: Optional[str]) -> Optional[str]:
        """The account this slave trades, or None for "not known yet"."""
        return self._accounts.get(slave_id or "")

    def is_autonomous(self, slave_id: Optional[str]) -> bool:
        """Whether a leg's fill is comparable to the master's price.

        It is not, in autonomous mode: a gold CFD against a GC future is a
        contract basis, not slippage. On the row so a consumer cannot get
        that wrong by accident.
        """
        return self.mode(slave_id) == "autonomous"

    def record_error(self, slave_id: Optional[str], message: Optional[str], at: float) -> None:
        if not slave_id:
            return
        self._last_error[slave_id] = {"message": message, "at": at}

    def last_error(self, slave_id: str) -> Dict[str, Any]:
        return self._last_error.get(slave_id, {})

    def size_unit(self, slave_id: Optional[str], slave_symbol: Optional[str]) -> Optional[str]:
        """The slave broker's own unit for this instrument's size.

        Nothing is converted anywhere: 24 M2K contracts and 1.0 US2000 lots
        are the same signal, and the unit is what stops a tab adding them
        together. A slave may name its unit in config; otherwise it is
        derived, because the copier genuinely knows which it is --

        - mirror mode speaks to an MT4/MT5 terminal, whose volume is lots;
        - autonomous mode sizes in whole instruments, and `contract_specs`
          already distinguishes a future from an equity: a symbol with no
          futures spec is an ETF, i.e. shares.
        """
        if slave_id and slave_id in self._units:
            return self._units[slave_id]
        if self.mode(slave_id) != "autonomous":
            return "lots"
        if not slave_symbol:
            return None
        return "contracts" if get_contract_spec(slave_symbol) else "shares"


def _open_legs_by_slave() -> Dict[str, int]:
    with db_cursor() as cur:
        rows = cur.execute(
            "SELECT slave_id, COUNT(*) AS n FROM slave_trades "
            "WHERE status = 'OPEN' GROUP BY slave_id"
        ).fetchall()
    return {row["slave_id"]: row["n"] for row in rows}


def _last_signal_by_account() -> Dict[str, float]:
    """When each master account was last heard from, from the signal history.

    Read from the `trades` table rather than held in memory so it survives a
    restart: "the master has sent nothing since 20:05" is still true after
    the copier was restarted at 20:30, and an in-memory value would say
    "never" and look like a dead master.
    """
    with db_cursor() as cur:
        rows = cur.execute(
            "SELECT master_account, MAX(received_at) AS last FROM trades "
            "GROUP BY master_account"
        ).fetchall()
    return {row["master_account"]: row["last"] for row in rows if row["last"]}


def _masters(receiver: Any) -> List[Dict[str, Any]]:
    """Last-seen per master account, and no link state.

    There is no honest "connected" for a master: the hub binds SUB and a
    master that exited looks exactly like one with nothing to say. The
    heartbeat on the PULL socket is the only positive liveness signal there
    is, and only for masters that send one -- so both times are reported and
    the threshold is the tab's to choose.
    """
    heartbeats: Dict[str, float] = dict(getattr(receiver, "last_heartbeat", {}) or {})
    signals = _last_signal_by_account()
    accounts = sorted(set(heartbeats) | set(signals))
    return [
        {
            "account": account,
            "last_signal_at": iso(signals.get(account)),
            "last_heartbeat_at": iso(heartbeats.get(account)),
        }
        for account in accounts
    ]


def _slave_account(slave: SlaveConfig, engine: Any) -> Optional[str]:
    """The account number this slave trades, where the copier knows it.

    The adapter is asked first: an IBKR login can hold several accounts and
    the one in use is resolved at connect time, so the adapter's answer is
    the live truth and the config's is only what was asked for.
    """
    resolved = getattr(getattr(engine, "adapter", None), "account", None)
    if resolved:
        return str(resolved)
    for key in ("account", "account_id", "futures_account_id", "brokerage_account_id"):
        value = slave.config.get(key)
        if value:
            return str(value)
    return None


def _takes_magic(slave: SlaveConfig) -> Optional[Dict[str, Any]]:
    """Which strategies this slave takes, for the hub to show by name.

    Reported because the hub's own registry is what names a magic number,
    so this is the field that lets its Copier tab say "this slave takes the
    trend-following book" rather than listing five integers. Any names an
    imported selection brought with it ride along.

    Swallows a bad selection: a snapshot is not the place to discover that
    a config is unreadable. The slave itself fails to start and says so.
    """
    try:
        return magic_filter.parse(slave.config).as_dict()
    except ValueError as exc:
        logger.debug("slave=%s magic selection is unreadable: %s", slave.id, exc)
        return {"error": str(exc)}


def _session_state(engine: Any, connected: Optional[bool]) -> str:
    if engine is None:
        return "not_running"
    if not getattr(engine, "session_up", True):
        return "down"
    if connected is None:
        return "unknown"
    return "connected" if connected else "disconnected"


def _slaves(engines: List[Any], facts: SlaveFacts) -> List[Dict[str, Any]]:
    by_id = {getattr(e, "slave_id", None): e for e in engines or []}
    legs = _open_legs_by_slave()
    out = []
    for slave in list_all_slaves():
        engine = by_id.get(slave.id)
        watchdog = getattr(engine, "watchdog", None) if engine is not None else None
        connected = getattr(watchdog, "connected", None) if watchdog is not None else None
        error = facts.last_error(slave.id)
        preflight = getattr(engine, "preflight", None) if engine is not None else None
        # Resolved from the live adapter, and remembered: a leg row carries
        # the same number, and the terminal may be shut by the time one is
        # built.
        account = _slave_account(slave, engine)
        facts.note_account(slave.id, account)
        out.append({
            "id": slave.id,
            "name": slave.name,
            "broker_type": slave.broker_type,
            "mode": slave.config.get("mode", "mirror"),
            "enabled": slave.enabled,
            # The engine's own flag when one is running: PATCH /slaves can
            # pause a live engine, and the row is updated alongside it.
            "paused": engine.paused if engine is not None else slave.paused,
            "running": engine is not None,
            "connected": connected,
            "open_legs": legs.get(slave.id, 0),
            "account": account,
            "session_state": _session_state(engine, connected),
            "blocked_targets": dict(getattr(engine, "blocked_targets", {}) or {}),
            # null means "every strategy", which is not the same as an
            # empty selection and the hub has to tell them apart.
            "magic_filter": _takes_magic(slave),
            "preflight": (
                {"targets": preflight["targets"], "resolved": preflight["resolved"],
                 "at": iso(preflight["at"])}
                if isinstance(preflight, dict) else None
            ),
            "last_error": error.get("message"),
            "last_error_at": iso(error.get("at")),
        })
    return out


def _risk(slaves: List[SlaveConfig]) -> Dict[str, Any]:
    """The copier-wide risk state, and the per-slave adjustments.

    A balance adjustment changes what every size on that slave means -- it
    is risking a percentage of a number that is not the account's balance --
    so a figure in the tab would otherwise be unexplainable. Only the
    non-zero ones are listed: an adjustment of zero is the absence of one,
    and a dictionary of zeros reads as though something were configured.
    """
    adjustments = {}
    for slave in slaves:
        amount = resolve_balance_adjustment(slave.config)
        if amount:
            adjustments[slave.id] = amount
    return {
        "kill_switch_enabled": get_config_value("kill_switch_enabled") == "true",
        "balance_adjustments": adjustments,
    }


def build(
    state: Any,
    facts: SlaveFacts,
    counters: Counters,
    queue_stats: Dict[str, Any],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The whole `snapshot` block. See docs/hub-reporter.md.

    `state` is the app's own state object, read at call time rather than at
    construction: engines are appended to it while the copier runs (POST
    /slaves) and a list captured at startup would go stale.
    """
    now = time.time() if now is None else now
    started_at = getattr(state, "start_time", None)
    slaves = list_all_slaves()
    facts.learn(slaves)

    return {
        "copier": {
            "version": __version__,
            "started_at": iso(started_at),
            "uptime_s": round(now - started_at) if started_at else None,
            "http_port": settings.http_port,
        },
        "masters": _masters(getattr(state, "receiver", None)),
        "slaves": _slaves(getattr(state, "engines", None) or [], facts),
        "risk": _risk(slaves),
        "counters": counters.as_dict(),
        "queue": queue_stats,
    }


__all__ = ["Counters", "SlaveFacts", "build"]
