"""The reporter: two background tasks, and nothing on the signal path.

Reporting must never slow or block copying. That shapes the whole design:

- The engines emit onto the event bus as they always did, into an unbounded
  asyncio queue. `emit` cannot block and cannot fail because of anything
  here.
- `_ingest` drains that queue in its own task, translates each event and
  writes it to the outbox. The database write happens here, not in the
  thing that produced the event.
- `_send` is the only part that touches the network. It has a short
  timeout, backs off on failure, and swallows everything -- a dead or slow
  hub costs a logged warning and not one copy.

Nothing in the signal path ever awaits either task.
"""
import asyncio
import contextlib
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from app.events import EventBus
from app.hub_reporter import catalogue, outbox as outbox_mod
from app.hub_reporter.config import SCHEMA_VERSION, HubReporterConfig
from app.hub_reporter.config import load as load_config
from app.hub_reporter.outbox import Outbox
from app.hub_reporter.snapshot import Counters, SlaveFacts
from app.hub_reporter.snapshot import build as build_snapshot

logger = logging.getLogger(__name__)

BACKOFF_CEILING_S = 60.0


class HubReporter:
    """Pushes the copier's state and history to the Operations hub.

    Inert unless configured: with no key, `start()` subscribes to nothing,
    starts no task and writes no row, so a copier without one behaves
    exactly as one built before this existed.
    """

    def __init__(
        self,
        events: EventBus,
        state: Any,
        config: Optional[HubReporterConfig] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.cfg = config if config is not None else load_config()
        self.events = events
        self.state = state
        self.session_id = outbox_mod.new_session_id()
        self.outbox = Outbox(self.cfg.copier_id, self.session_id, self.cfg.queue_max)
        self.facts = SlaveFacts()
        self.counters = Counters()
        self.seq = 0

        self._queue: Optional["asyncio.Queue[Dict[str, Any]]"] = None
        self._tasks: List[asyncio.Task] = []
        self._nudge = asyncio.Event()
        # Set before the tasks are cancelled, and checked by both loops.
        #
        # Cancellation alone is not enough to stop the sender, which was a
        # surprise worth writing down: httpx runs on anyio, and when a
        # request is cancelled inside anyio's own timeout scope the scope
        # absorbs the CancelledError and raises an httpx timeout in its
        # place. That is an ordinary Exception, so the sender's "a failed
        # report is still a report" handler caught it, treated it as the
        # hub being slow, and carried on -- with the task's one
        # cancellation already spent. Shutdown then waited forever.
        self._stopping = asyncio.Event()
        self._client = client
        self._owns_client = client is None
        self._backoff = 0.0
        # Observable without reaching into the network: the tests assert on
        # these, and so does anyone asking why the tab has gone quiet.
        self.reports_sent = 0
        self.consecutive_failures = 0
        self.last_ok_at: Optional[float] = None
        self.last_error: Optional[str] = None

    # ----------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return bool(self._tasks)

    async def start(self) -> None:
        if not self.cfg.active:
            # Said once, at INFO, with the reason. Silence here is how a
            # reporter comes to be switched off for a month without anyone
            # noticing the tab is stale.
            logger.info(
                "hub reporter: off (%s) -- the copier runs exactly as it did "
                "before the reporter existed",
                self._why_inactive(),
            )
            return

        self._stopping.clear()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.cfg.timeout_s)
        self._queue = self.events.subscribe()
        self._tasks = [
            asyncio.create_task(self._ingest(), name="hub-reporter-ingest"),
            asyncio.create_task(self._send_loop(), name="hub-reporter-send"),
        ]
        logger.info(
            "hub reporter: reporting as copier_id=%s session=%s to %s every %ss",
            self.cfg.copier_id, self.session_id, self.cfg.endpoint,
            self.cfg.snapshot_interval_s,
        )

    def _why_inactive(self) -> str:
        if not self.cfg.enabled:
            return "hub_reporter.enabled is false"
        if not self.cfg.url:
            return "no hub_reporter.url configured"
        return "no key configured"

    async def stop(self) -> None:
        """Stop reporting. Bounded, because shutting the copier down is not
        allowed to wait on the network.

        The flag goes up before the cancellation so neither loop can
        mistake a cancelled request for a slow hub and go round again. The
        wait is bounded anyway: a task that will not stop is reported and
        abandoned rather than holding up a shutdown that has engines and
        broker sessions behind it.
        """
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            done, pending = await asyncio.wait(self._tasks, timeout=5.0)
            for task in done:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    task.result()
            if pending:
                logger.warning(
                    "hub reporter: %d task(s) did not stop in time; abandoning them",
                    len(pending),
                )
        self._tasks = []
        if self._queue is not None:
            self.events.unsubscribe(self._queue)
            self._queue = None
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # -------------------------------------------------------------- ingest

    async def _ingest(self) -> None:
        """Drain the event bus into the outbox, forever.

        Wrapped so that nothing -- a malformed event, a locked database --
        can end this task. If it dies the copier keeps copying and stops
        reporting, which is the wrong half to lose silently.
        """
        assert self._queue is not None
        while not self._stopping.is_set():
            event = await self._queue.get()
            try:
                self.ingest_one(event)
            except Exception:
                logger.exception("hub reporter: could not queue an event")

    def ingest_one(self, event: Dict[str, Any]) -> Optional[str]:
        """Translate one bus event, queue it, and ask for a report.

        Synchronous and separate from the loop so a test can drive it
        directly, and so the ordering is obvious: the event is on disk
        before anything is told to send it.
        """
        bus_type = event.get("type")
        reported = catalogue.translate(event, self.facts)
        if reported is None:
            return None

        self.counters.record(catalogue.counter_for(bus_type))
        if catalogue.class_for(bus_type) == catalogue.ALERT:
            detail = reported.get("detail") or {}
            self.facts.record_error(
                reported.get("slave_id"),
                detail.get("message") or detail.get("reason") or str(bus_type),
                event.get("emitted_at") or time.time(),
            )

        event_id = self.outbox.queue(reported, queued_at=event.get("emitted_at"))
        if event_id is not None:
            # At once when something happens, debounced by the sender so
            # one signal's legs travel together.
            self._nudge.set()
        return event_id

    # ---------------------------------------------------------------- send

    async def _send_loop(self) -> None:
        """Report when something happened, and otherwise on the interval.

        The interval matters as much as the events: a snapshot every 15s is
        how the tab tells "quiet" from "dead", and events alone cannot say
        that because a quiet copier emits none.
        """
        while not self._stopping.is_set():
            try:
                await self._wait_for_turn()
                if self._stopping.is_set():
                    return
                await self._send_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never allowed to end the task. A report that fails is a
                # report; a sender that stopped is an outage nobody sees.
                logger.exception("hub reporter: send cycle failed")
                self._note_failure("unexpected error")

    async def _wait_for_turn(self) -> None:
        if self._backoff:
            # A nudge does not shorten a back-off: a hub that just refused a
            # report will refuse the next one, and a busy copier would turn
            # the back-off into a tight loop exactly when the hub is least
            # able to take it.
            await asyncio.sleep(self._backoff)
            return
        try:
            await asyncio.wait_for(self._nudge.wait(), timeout=self.cfg.snapshot_interval_s)
        except asyncio.TimeoutError:
            return
        # Something happened. Wait out the debounce so the rest of the
        # fan-out -- the other slaves' legs, a refusal, a margin note --
        # lands in the same report.
        self._nudge.clear()
        if self.cfg.debounce_s > 0:
            await asyncio.sleep(self.cfg.debounce_s)
        self._nudge.clear()

    def build_report(self) -> Dict[str, Any]:
        """One report: identity, the snapshot, and the oldest events.

        `seq` increments per report within the session, so a gap tells the
        hub a report was lost -- which costs a snapshot and no events,
        because the events are still queued until they are acked.
        """
        self.seq += 1
        events = self.outbox.oldest(self.cfg.max_events_per_report)
        return {
            "schema_version": SCHEMA_VERSION,
            "copier_id": self.cfg.copier_id,
            "session_id": self.session_id,
            "seq": self.seq,
            "sent_at": catalogue.iso(time.time()),
            "snapshot": build_snapshot(
                self.state, self.facts, self.counters, self.outbox.stats()
            ),
            "events": events,
        }

    async def _send_once(self) -> bool:
        """One POST. One. Never a retry inside a cycle.

        A retry here would multiply the cost of a hub that is slow rather
        than down -- three attempts at a 5s timeout is 15s in which the
        queue keeps growing -- and the next cycle is a retry already, with
        the backlog and a fresh snapshot.
        """
        report = self.build_report()
        sent_ids = [e.get("event_id") for e in report["events"]]
        assert self._client is not None

        try:
            response = await self._client.post(
                self.cfg.endpoint,
                json=report,
                headers={"Authorization": f"Bearer {self.cfg.key}",
                         "Content-Type": "application/json"},
                timeout=self.cfg.timeout_s,
            )
        except Exception as exc:
            # httpx raises a family of errors and a proxy or a DNS failure
            # can raise from below it. Any of them means the same thing
            # here: nothing was acked, try again later.
            #
            # Except one: a cancellation that anyio turned into a timeout
            # while this request was in flight. Treating that as a slow hub
            # would have the loop go round again with the task's
            # cancellation already spent, and nothing would ever stop.
            if self._stopping.is_set():
                raise asyncio.CancelledError from exc
            self._note_failure(f"{type(exc).__name__}: {exc}")
            return False

        if response.status_code != 200:
            self._note_failure(f"HTTP {response.status_code}")
            return False

        acked = self._acked_ids(response)
        if acked is None:
            # A 200 whose body cannot be read acks nothing. Treated as a
            # failure so the back-off applies: a hub answering 200 with
            # rubbish is broken, and hammering it will not help.
            self._note_failure("200 with an unreadable body")
            return False

        # Exactly the ids the hub returned. Not "everything we sent", which
        # would lose the events a partly-successful store did not keep.
        removed = self.outbox.ack(acked)
        self.reports_sent += 1
        self.consecutive_failures = 0
        self.last_ok_at = time.time()
        self.last_error = None
        self._backoff = 0.0

        unacked = len(sent_ids) - removed
        if unacked > 0:
            logger.info(
                "hub reporter: seq=%s sent %d event(s), the hub acked %d -- the "
                "rest stay queued",
                report["seq"], len(sent_ids), removed,
            )
        else:
            logger.debug("hub reporter: seq=%s ok, %d event(s) acked",
                         report["seq"], removed)
        # Still more behind this report than one carries: go again rather
        # than wait out the interval, or a backlog drains at 200 events per
        # 15 seconds.
        if removed and self.outbox.stats()["depth"]:
            self._nudge.set()
        return True

    @staticmethod
    def _acked_ids(response: httpx.Response) -> Optional[List[str]]:
        try:
            body = response.json()
        except ValueError:
            return None
        if not isinstance(body, dict):
            return None
        acked = body.get("acked_event_ids")
        if acked is None:
            # A 200 that says nothing about events acks none of them. Not an
            # error: a hub may legitimately answer a snapshot-only report
            # this way.
            return []
        if not isinstance(acked, list):
            return None
        return [str(i) for i in acked]

    def _note_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        self.last_error = reason
        previous = self._backoff
        self._backoff = min(BACKOFF_CEILING_S, previous * 2 if previous else 1.0)
        # At WARNING for the first few and then DEBUG: a hub that is down
        # for a day must not fill the copier's log with one line a minute,
        # and the state is in the snapshot's queue depth anyway.
        log = logger.warning if self.consecutive_failures <= 3 else logger.debug
        log(
            "hub reporter: report failed (%s); retrying in %.0fs. Copying is "
            "unaffected and the backlog is held.",
            reason, self._backoff,
        )
