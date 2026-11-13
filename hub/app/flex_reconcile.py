"""Check the copier's record of what it did against the broker's own.

`app/flex.py` fetches and parses an IBKR Activity Flex statement. This
stores it and compares it to the Trade Registry, on a daily schedule,
without anybody running anything by hand.

The join key, and the mistake that was made about it
----------------------------------------------------

This was first written believing the two sides were already keyed the
same way: `IBKRAdapter.open()` recorded `slave_ticket =
trade.order.orderId`, Flex reports `ibOrderID`, so the join looked like a
plain equality.

They are different identifiers. `orderId` is TWS's CLIENT-side sequence
number -- small, and restarting with the session (497, 295, 404 in this
hub's registry). `ibOrderID` is IBKR's own, nine digits (244184125). Run
against a real 30-day statement the join matched **0 of 109 orders** and
called every one of them untracked, which would have alerted every day
and been wrong every day.

So the adapter now records `trade.order.permId` -- IBKR's permanent,
broker-side id, and the one that actually appears in a statement -- into
`slave_trades.broker_order_id`, and that is what the join uses.

Rows written before that change have no `broker_order_id`, and nothing can
recover it for them. A pass that cannot join says so and claims nothing:
see `Summary.join_available`. Claiming drift on the strength of a key that
is not there is how a reconciliation becomes an alarm nobody reads.

What is compared, and what deliberately is not
----------------------------------------------

Reported, because the statement positively says it:

- **matched** -- an execution whose order id this hub has a registry row
  for. Its price and size are checked against what the hub captured at
  fill time, which is the point of the whole exercise: a fill price is
  reported once over the API and is simply wrong if it was mis-captured,
  and nothing other than the broker's own statement can say so.
- **untracked** -- the broker executed an order this copier has no record
  of. Protective legs are not untracked: their order ids are in
  `engine_positions.sl_order_id` / `tp_order_id`, and counting a stop fill
  as untracked would make every protected position look like drift.
- **unjoinable** -- executions with no `ibOrderID`, because the Flex query
  does not have it ticked. Reported as a number so the cause of an empty
  reconciliation is visible rather than looking like "nothing to check".

NOT reported: registry rows with no execution in the statement. It cannot
be computed honestly. A Flex statement covers completed statement days
only, so a position opened today is legitimately absent, one opened before
the statement window is legitimately absent, and deciding which is which
means comparing the registry's epoch timestamps against statement dates in
the account's configured report timezone -- which the file does not state.
The figure would be mostly false positives, and a number that is usually
wrong teaches the reader to ignore the event it is in.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.db import db_cursor
from app.events import EventBus
from app.flex import FlexClient, FlexError, FlexStatement, FlexTrade
from app.flex import client_from_settings
from app.notifier import Notifier

logger = logging.getLogger(__name__)

# How far a price may differ before it is a mismatch rather than rounding.
# Relative, because a disagreement of 0.01 is nothing on gold at 4,040 and
# everything on a 0.25-tick index future.
PRICE_TOLERANCE = 1e-4
SIZE_TOLERANCE = 1e-6

# Flex error codes that no amount of retrying will fix: they need a person
# in Client Portal. Retried on the normal schedule rather than backed off
# into silence, and notified once.
NEEDS_A_HUMAN = {"1012", "1014", "1015"}


# --------------------------------------------------------------- storing


def store(statement: FlexStatement, now: Optional[float] = None) -> Tuple[int, int]:
    """Write the statement's executions down. Returns (stored, already had).

    INSERT OR IGNORE on the execution id, because Flex periods overlap by
    design: a query configured for "last 30 days" re-sends 29 of them every
    time it runs.
    """
    now = time.time() if now is None else now
    stored = seen = 0
    with db_cursor() as cur:
        for trade in statement.trades:
            key = trade.exec_id or trade.trade_id
            if not key:
                # Nothing to deduplicate on. Storing it would mean a new row
                # on every import for the same execution, so the honest
                # thing is to leave it out and let `unjoinable` carry it.
                logger.warning(
                    "Flex execution on %s has neither ibExecID nor tradeID; "
                    "not stored, as there would be no way to avoid storing "
                    "it again tomorrow", trade.symbol,
                )
                continue
            cur.execute(
                """
                INSERT OR IGNORE INTO flex_trades
                    (exec_key, account, symbol, underlying, conid, ib_order_id,
                     direction, quantity, price, proceeds, commission, net_cash,
                     open_close, exchange, trade_time, order_time,
                     statement_to, imported_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, trade.account, trade.symbol, trade.underlying,
                    trade.conid, trade.ib_order_id, trade.direction,
                    trade.quantity, trade.price, trade.proceeds,
                    trade.commission, trade.net_cash, trade.open_close,
                    trade.exchange,
                    trade.trade_time.isoformat() if trade.trade_time else None,
                    trade.order_time.isoformat() if trade.order_time else None,
                    statement.to_date, now,
                ),
            )
            if cur.rowcount:
                stored += 1
            else:
                seen += 1
    return stored, seen


# ------------------------------------------------------------ comparing


@dataclass
class OrderFill:
    """Every execution of one broker order, added up.

    An order fills in pieces -- a 24-lot entry can arrive as four
    executions at three prices -- and the figure comparable with what the
    hub captured is the volume-weighted average of them, not any one.
    """

    ib_order_id: str
    symbol: str
    direction: str
    # How much was filled, and how much of it was net. An IB order is one
    # side, so for a sane statement `filled` is `abs(net)` -- but they are
    # tracked separately rather than derived from each other, because
    # deriving the size from the net sum means a buy and a sell appearing
    # under one order id cancel out and the order reads as a zero-size
    # fill. That then shows up as a size mismatch against whatever the hub
    # recorded, which is a true statement about nothing: the hub's figure
    # was right and the odd thing was the pairing.
    filled: float = 0.0             # sum of |quantity|
    net: float = 0.0                # signed
    # The copier's own reference, as the broker handed it back. Empty for
    # an order this hub did not place.
    order_ref: Optional[str] = None
    # Whether these executions OPENED the position. The registry row holds
    # the entry's price and size, so only an opening fill can be checked
    # against it -- an exit legitimately fills somewhere else entirely, and
    # comparing a stop against the price it was protecting would report a
    # mismatch on every protected position that ever closed.
    #
    # None where the statement does not say, in which case nothing is
    # claimed either way.
    opening: Optional[bool] = None
    executions: int = 0
    commission: float = 0.0
    _notional: float = 0.0

    def add(self, trade: FlexTrade) -> None:
        self.executions += 1
        if trade.order_ref and not self.order_ref:
            self.order_ref = trade.order_ref
        if trade.open_close in ("O", "C"):
            opening = trade.open_close == "O"
            # A single order that both opens and closes is not something to
            # check against an entry price either way.
            self.opening = opening if self.opening in (None, opening) else None
        self.filled += abs(trade.quantity)
        self.net += trade.quantity
        self._notional += abs(trade.quantity) * trade.price
        if trade.commission:
            self.commission += trade.commission

    @property
    def avg_price(self) -> Optional[float]:
        """The volume-weighted average of the executions.

        A 24-lot entry can arrive as four executions at three prices, and
        the figure comparable with what the hub captured is this, not any
        one of them.
        """
        return self._notional / self.filled if self.filled else None

    @property
    def two_sided(self) -> bool:
        """Both sides under one order id: nothing a sane statement contains."""
        return abs(abs(self.net) - self.filled) > SIZE_TOLERANCE

    def as_dict(self) -> Dict[str, Any]:
        out = {
            "ib_order_id": self.ib_order_id,
            "slave_symbol": self.symbol,
            "direction": self.direction,
            "size": round(self.filled, 8),
            "avg_price": self.avg_price,
            "executions": self.executions,
            "commission": round(self.commission, 4) if self.commission else 0.0,
        }
        if self.two_sided:
            out["net"] = round(self.net, 8)
        return out


def _by_order(trades: List[FlexTrade]) -> Tuple[Dict[str, OrderFill], int]:
    """Group executions by broker order. Returns (orders, unjoinable count)."""
    orders: Dict[str, OrderFill] = {}
    unjoinable = 0
    for trade in trades:
        if not trade.ib_order_id:
            unjoinable += 1
            continue
        fill = orders.get(trade.ib_order_id)
        if fill is None:
            fill = OrderFill(trade.ib_order_id, trade.symbol, trade.direction)
            orders[trade.ib_order_id] = fill
        fill.add(trade)
    return orders, unjoinable


@dataclass
class RegistryRow:
    slave_id: str
    slave_symbol: str
    price: Optional[float]
    size: Optional[float]


def _registry_by_order_id() -> Tuple[Dict[str, RegistryRow], int]:
    """Copies keyed by the BROKER's order id, and how many lack one.

    Only IBKR slaves. `broker_order_id` is only ever written for them, and
    an MT4 slave's `slave_ticket` lives in a different numbering space
    entirely -- joining across both would match by coincidence of value,
    which for small integers happens.

    The second return value is the number of IBKR rows with no broker id.
    Those are rows written before the id was captured, and nothing can
    recover it for them: they are invisible to this join, and a count is
    the only honest thing to say about them.
    """
    with db_cursor() as cur:
        rows = cur.execute(
            """
            SELECT st.slave_id, st.slave_symbol, st.broker_order_id,
                   st.broker_order_ref, st.slave_price, st.slave_lots
            FROM slave_trades st
            JOIN slaves s ON s.id = st.slave_id
            WHERE s.broker_type = 'ibkr'
            """
        ).fetchall()

    joinable: Dict[str, RegistryRow] = {}
    without = 0
    for r in rows:
        row = RegistryRow(
            slave_id=r["slave_id"], slave_symbol=r["slave_symbol"],
            price=r["slave_price"], size=r["slave_lots"],
        )
        keys = [r["broker_order_ref"], r["broker_order_id"]]
        keys = [str(k) for k in keys if k]
        if not keys:
            without += 1
            continue
        # Keyed under BOTH, because they answer different executions. The
        # reference is on every leg of the position, including protective
        # stops and targets this hub never placed a closing order for; the
        # broker's order id is on the entry alone. An execution matching
        # either is ours.
        for key in keys:
            joinable[key] = row
    return joinable, without


def _protective_order_ids() -> Dict[str, str]:
    """Stop and target order ids, so a stop fill is not read as drift.

    These execute at the broker without this hub placing a closing order,
    so they appear in the statement under order ids that are nowhere in
    `slave_trades`. Counted as untracked, every protected position would
    look like something the copier had lost track of.

    These ids are `trade.order.orderId` -- TWS's client-side number -- so
    they cannot match a statement's `ibOrderID` any more than the entry
    ids could. They are kept as a second line of defence for positions
    opened before the copier stamped its own reference on orders; since
    then the legs carry the entry's reference and resolve through that
    instead, which is what made `untracked` believable at all.
    """
    known: Dict[str, str] = {}
    with db_cursor() as cur:
        rows = cur.execute(
            "SELECT slave_id, slave_symbol, sl_order_id, tp_order_id "
            "FROM engine_positions"
        ).fetchall()
    for row in rows:
        for column, label in (("sl_order_id", "stop"), ("tp_order_id", "target")):
            if row[column] is not None:
                known[str(row[column])] = f"{label} on {row['slave_symbol']}"
    return known


def _mismatch(fill: OrderFill, row: RegistryRow) -> Optional[Dict[str, Any]]:
    """Where the broker's record and the hub's disagree about one order.

    Only an OPENING fill is checked. The registry row records what the
    entry executed at, and a protective stop or target fills wherever the
    market took it -- so comparing an exit against the price it was
    protecting would report a mismatch on every position that ever closed,
    which is the opposite of useful. Exits are recognised as ours through
    the reference and counted; what they filled at is the variance
    capture's business, not this one's.
    """
    differences = {}

    if fill.two_sided:
        # Reported whatever the fill opened or closed: an order carrying
        # both sides is odd on its own terms, and the hub's figure is not
        # the suspect one.
        differences["two_sided"] = {"filled": fill.filled, "net": fill.net}

    if fill.opening is not True:
        return {
            "ib_order_id": fill.ib_order_id,
            "slave_id": row.slave_id,
            "slave_symbol": row.slave_symbol,
            "executions": fill.executions,
            **differences,
        } if differences else None

    broker_price = fill.avg_price
    if row.price is not None and broker_price is not None:
        scale = max(abs(broker_price), 1e-9)
        if abs(broker_price - row.price) / scale > PRICE_TOLERANCE:
            differences["price"] = {"hub": row.price, "broker": broker_price}

    if row.size is not None and abs(fill.filled - abs(row.size)) > SIZE_TOLERANCE:
        differences["size"] = {"hub": abs(row.size), "broker": fill.filled}

    if not differences:
        return None
    return {
        "ib_order_id": fill.ib_order_id,
        "slave_id": row.slave_id,
        "slave_symbol": row.slave_symbol,
        "executions": fill.executions,
        **differences,
    }


@dataclass
class Summary:
    """What one reconciliation pass found."""

    checked: int = 0                 # executions in the statement
    orders: int = 0                  # distinct broker orders among them
    matched: int = 0                 # orders this hub has a registry row for
    unjoinable: int = 0              # executions with no ibOrderID
    stored: int = 0
    already_had: int = 0
    protective: int = 0              # orders that are a stop or a target
    mismatched: List[Dict[str, Any]] = field(default_factory=list)
    untracked: List[Dict[str, Any]] = field(default_factory=list)
    period: Optional[str] = None
    account: str = ""
    # Whether any registry row carries a broker order id to join on. False
    # means this pass compared nothing and is making no claim.
    join_available: bool = False
    # Whether `untracked` can be believed. False while the protective
    # stop/target orders are still recorded by their client-side ids, which
    # a statement never reports -- so every protective fill would appear in
    # the list. The orders are still reported, for the record; they just do
    # not raise anything.
    untracked_reliable: bool = False
    # IBKR rows with no broker order id -- written before it was captured,
    # unrecoverable, and invisible to the join.
    unjoinable_rows: int = 0

    @property
    def clean(self) -> bool:
        """Nothing worth raising. Read by the alerting, so it is
        deliberately conservative about what counts.

        A pass that could not join is not "clean" in the sense of "all
        well" -- it is silent -- but it must not alert. And `untracked` is
        only counted while it can be believed: see untracked_reliable.
        """
        if self.mismatched:
            return False
        return not (self.untracked and self.untracked_reliable)

    def as_detail(self) -> Dict[str, Any]:
        return {
            "account": self.account,
            "period": self.period,
            "checked": self.checked,
            "orders": self.orders,
            "matched": self.matched,
            "protective": self.protective,
            "unjoinable": self.unjoinable,
            "stored": self.stored,
            "already_had": self.already_had,
            "mismatched": self.mismatched,
            "untracked": self.untracked,
            "join_available": self.join_available,
            "unjoinable_rows": self.unjoinable_rows,
            "untracked_reliable": self.untracked_reliable,
            # Said in the payload, not only in this module's docstring: a
            # reader must not take a clean pass as "the broker executed
            # everything the registry expected".
            "absence_checked": False,
        }


def compare(statement: FlexStatement, stored: int = 0, already_had: int = 0) -> Summary:
    """Compare a statement against the registry. Touches nothing."""
    orders, unjoinable = _by_order(statement.trades)
    registry, unjoinable_rows = _registry_by_order_id()
    protective = _protective_order_ids()

    summary = Summary(
        checked=len(statement.trades), orders=len(orders), unjoinable=unjoinable,
        stored=stored, already_had=already_had,
        period=f"{statement.from_date} -> {statement.to_date}",
        account=statement.account,
        join_available=bool(registry),
        unjoinable_rows=unjoinable_rows,
        # Believable once the copier stamps its own reference on orders,
        # because the reference reaches the protective legs too. Judged per
        # statement rather than assumed: an execution carrying no reference
        # at all is either older than the stamping or genuinely not ours,
        # and only the first is a reason to stay quiet.
        untracked_reliable=any(t.order_ref for t in statement.trades),
    )

    if not registry:
        # Nothing to join against, so nothing can be said. Reporting every
        # order as untracked here would be an alarm that fires daily and is
        # wrong daily -- which is exactly what happened before the key was
        # fixed, measured at 109 of 109.
        logger.warning(
            "Flex reconciliation: no registry row carries a broker order id, "
            "so this statement cannot be joined and nothing is being claimed "
            "about it. %d IBKR row(s) predate the id being captured. New "
            "copies will carry it.", unjoinable_rows,
        )
        return summary

    for order_id, fill in sorted(orders.items()):
        # The reference first: it is on every leg, so a protective fill
        # resolves here where the order id would miss it.
        row = registry.get(fill.order_ref or "") or registry.get(order_id)
        if row is not None:
            summary.matched += 1
            difference = _mismatch(fill, row)
            if difference is not None:
                summary.mismatched.append(difference)
            continue
        if order_id in protective:
            summary.protective += 1
            continue
        summary.untracked.append({**fill.as_dict(), "note": "no registry row"})

    return summary


# ------------------------------------------------------------ the pass


async def run_once(
    events: Optional[EventBus] = None,
    client: Optional[FlexClient] = None,
    notifier: Optional[Notifier] = None,
) -> Optional[Summary]:
    """Fetch, store, compare and report. None when Flex is not configured.

    Raises FlexError, which the caller turns into a retry: a statement that
    cannot be fetched is an outage, not a reconciliation finding, and
    reporting it as a clean pass would be the worst of both.
    """
    client = client_from_settings() if client is None else client
    if client is None:
        return None

    statement = await client.fetch_async()
    stored, already_had = store(statement)
    summary = compare(statement, stored, already_had)

    if not summary.join_available:
        logger.info(
            "Flex reconciliation (%s): %d execution(s) stored, %d new. No "
            "comparison was made -- see the warning above.",
            summary.period, summary.checked, summary.stored,
        )
    elif summary.clean:
        logger.info(
            "Flex reconciliation (%s): %d execution(s) over %d order(s), "
            "%d matched, %d protective, %d new -- nothing disagrees",
            summary.period, summary.checked, summary.orders, summary.matched,
            summary.protective, summary.stored,
        )
    else:
        logger.warning(
            "Flex reconciliation (%s): %d mismatch(es) and %d untracked "
            "order(s) at the broker, over %d execution(s)",
            summary.period, len(summary.mismatched), len(summary.untracked),
            summary.checked,
        )
        for item in summary.mismatched:
            logger.warning("  order %s %s: %s", item["ib_order_id"],
                           item["slave_symbol"],
                           {k: v for k, v in item.items() if k in ("price", "size")})
        for item in summary.untracked:
            logger.warning("  order %s: the broker filled %s %s x%s and this "
                           "hub has no record of it",
                           item["ib_order_id"], item["direction"],
                           item["slave_symbol"], item["size"])

    if summary.untracked and not summary.untracked_reliable:
        logger.info(
            "Flex reconciliation: %d order(s) could not be tied to a registry "
            "row, which is expected for now -- protective stop and target "
            "orders are still recorded by their client-side ids, which a "
            "statement never reports. Not raising anything on it.",
            len(summary.untracked),
        )

    if summary.unjoinable:
        logger.warning(
            "Flex reconciliation: %d execution(s) carry no ibOrderID and "
            "could not be joined. Tick it in the query's Trades section in "
            "Client Portal.", summary.unjoinable,
        )

    if events is not None:
        await events.emit({
            "type": "flex_reconciliation",
            # Not a slave's event: a statement covers the whole account, and
            # which slaves it touched is what the join establishes.
            "slave_id": None,
            **summary.as_detail(),
        })

    if notifier is not None and not summary.clean:
        untracked = (len(summary.untracked) if summary.untracked_reliable
                     else 0)
        await notifier.notify(
            "flex_reconciliation",
            f"IBKR's own statement disagrees with the copier's record: "
            f"{len(summary.mismatched)} mismatch(es) and {untracked} order(s) "
            f"the broker filled that this hub has no record of, over "
            f"{summary.period}",
        )

    return summary


class FlexReconciler:
    """Runs the pass daily, in the background, and never on the signal path.

    Daily because that is as often as there is anything new: a statement
    reports from completed statement days, so a second fetch the same day
    returns the same file. The token is rate limited (error 1018) and
    expires, so this is a schedule and not a poll.
    """

    def __init__(
        self,
        events: Optional[EventBus] = None,
        notifier: Optional[Notifier] = None,
        client: Optional[FlexClient] = None,
        interval: Optional[float] = None,
        initial_delay: Optional[float] = None,
        retry_base: Optional[float] = None,
    ) -> None:
        self.events = events
        self.notifier = notifier
        self.client = client if client is not None else client_from_settings()
        self.interval = (settings.flex_poll_interval_seconds
                         if interval is None else interval)
        self.initial_delay = (settings.flex_initial_delay_seconds
                              if initial_delay is None else initial_delay)
        self.retry_base = (settings.flex_retry_base_seconds
                           if retry_base is None else retry_base)
        self._task: Optional[asyncio.Task] = None
        self.passes = 0
        self.failures = 0
        self.last_summary: Optional[Summary] = None
        self.last_error: Optional[str] = None
        self._told_someone: Optional[str] = None

    @property
    def configured(self) -> bool:
        return self.client is not None

    def start(self) -> None:
        if not self.configured:
            # Normal. The hub has always run fine without Flex, and
            # reconciliation against a statement nobody has configured is
            # not something to warn about every boot.
            logger.info(
                "Flex is not configured, so there is no independent record to "
                "reconcile against (set TC_FLEX_TOKEN and TC_FLEX_QUERY_ID)")
            return
        self._task = asyncio.create_task(self._loop(), name="flex-reconciler")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        # Startup is busy -- adapters connecting, positions restoring,
        # preflight resolving thirty-five contracts -- and a Flex fetch can
        # sit waiting on IBKR generating the report. It can wait a minute.
        await asyncio.sleep(self.initial_delay)
        backoff = 0.0
        while True:
            try:
                await run_once(self.events, self.client, self.notifier)
            except asyncio.CancelledError:
                raise
            except FlexError as exc:
                backoff = self._note_failure(exc, exc.code, backoff)
            except Exception as exc:
                # A network error, a malformed document, a locked database.
                # None of them is worth ending the task for: tomorrow's
                # statement is a fresh attempt.
                logger.exception("Flex reconciliation failed")
                backoff = self._note_failure(exc, None, backoff)
            else:
                self.passes += 1
                self.last_error = None
                self._told_someone = None
                backoff = 0.0

            await asyncio.sleep(backoff or self.interval)

    def _note_failure(self, exc: Exception, code: Optional[str],
                      backoff: float) -> float:
        self.failures += 1
        self.last_error = str(exc)

        if code in NEEDS_A_HUMAN:
            # No schedule fixes an expired token. Said once per distinct
            # cause, because this will now happen on every attempt until
            # somebody goes to Client Portal, and a daily alert about it
            # becomes noise within a week.
            logger.error("Flex: %s", exc)
            if self.notifier is not None and self._told_someone != code:
                self._told_someone = code
                asyncio.ensure_future(self.notifier.notify(
                    "flex_unavailable",
                    f"The copier cannot fetch IBKR's statement and is not "
                    f"reconciling against it: {exc}",
                ))
            return self.interval

        logger.warning("Flex: %s -- retrying later", exc)
        # Doubling from the retry base, capped at the normal interval: there
        # is no value in trying more often than there is a new statement,
        # and 1018 means the token is already being rate limited.
        return min(self.interval, backoff * 2 if backoff else self.retry_base)
