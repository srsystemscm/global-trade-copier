import asyncio
import logging
import time
from typing import Awaitable, Callable, Dict, List, Optional

from app.adapters.base import AdapterError, BrokerAdapter
from app.events import EventBus
from app.notifier import Notifier
from app.zmq_receiver import ZmqReceiver

logger = logging.getLogger(__name__)


class SlaveWatchdog:
    """Background per-slave monitor.

    Two jobs. First, *noticing*: polls `adapter.get_status()` for
    connectivity and, where the adapter supports it,
    `adapter.get_account_summary()` for a simple peak-equity drawdown alert.

    Second, for adapters that hold a stateful broker session
    (`requires_session_recovery`), *rebuilding* it. Most adapters don't need
    that -- MT4Adapter rebuilds its REQ socket on timeout, and Schwab just
    keeps making HTTP requests -- but IBKR holds one TCP session that dies
    every time IB Gateway performs its forced daily restart. Without
    recovery the slave stays dark until someone restarts the hub by hand.

    Recovery is deliberately *not* a bare reconnect: the `recover` callback
    re-runs the slave's whole startup path (connect, reconcile against what
    the broker actually holds now, restore in-memory position state),
    because a position can be stopped out while the session is down and
    coming back with stale bookkeeping is worse than staying down.
    """

    def __init__(
        self,
        slave_id: str,
        adapter: BrokerAdapter,
        events: EventBus,
        notifier: Notifier,
        account_types: List[Optional[str]],
        poll_interval: float,
        drawdown_alert_pct: float,
        is_paused: Optional[Callable[[], bool]] = None,
        recover: Optional[Callable[[], Awaitable[None]]] = None,
        on_session_state: Optional[Callable[[bool], None]] = None,
        recovery_max_attempts: int = 8,
        recovery_base_delay: float = 5.0,
        recovery_max_delay: float = 60.0,
    ) -> None:
        self.slave_id = slave_id
        self.adapter = adapter
        self.events = events
        self.notifier = notifier
        self.account_types = account_types
        self.poll_interval = poll_interval
        self.drawdown_alert_pct = drawdown_alert_pct
        # A paused slave is deliberately idle, so health-checking it is noise:
        # it reports "disconnected" for a condition the operator created on
        # purpose, and on MT4 each failed PING costs three retries with
        # backoff. Left unchecked that buries real signals in the log.
        self.is_paused = is_paused or (lambda: False)
        self.recover = recover
        # Lets the engine refuse signals while the session is down rather
        # than firing them at a dead adapter.
        self.on_session_state = on_session_state or (lambda up: None)
        self.recovery_max_attempts = recovery_max_attempts
        self.recovery_base_delay = recovery_base_delay
        self.recovery_max_delay = recovery_max_delay

        self.connected: Optional[bool] = None  # None = not checked yet
        self._paused_logged = False
        self._account_summary_supported = True
        self._peak_equity: Dict[Optional[str], float] = {}
        self._drawdown_alerted: Dict[Optional[str], bool] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"watchdog-{self.slave_id}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            if self.is_paused():
                if not self._paused_logged:
                    logger.info("slave=%s paused, suspending health checks", self.slave_id)
                    self._paused_logged = True
                # Forget the last known state so the first poll after resume
                # re-baselines silently instead of firing a connect/disconnect
                # transition for time the slave spent deliberately idle.
                self.connected = None
                continue
            if self._paused_logged:
                logger.info("slave=%s resumed, health checks back on", self.slave_id)
                self._paused_logged = False
            await self._check_connectivity()
            if self._account_summary_supported:
                await self._check_drawdown()

    async def _check_connectivity(self) -> None:
        try:
            ok = await self.adapter.get_status()
        except Exception:
            ok = False

        if self.connected is None:
            self.connected = ok
            return

        if ok and not self.connected:
            # Came back on its own (MT4's REQ socket, a flaky network). No
            # recovery needed -- but still tell the engine it may trade.
            await self._mark_up("slave_reconnected", f"Slave {self.slave_id} reconnected")
        elif not ok and self.connected:
            self.connected = False
            self.on_session_state(False)
            logger.warning("slave=%s disconnected", self.slave_id)
            await self.events.emit({"type": "slave_disconnected", "slave_id": self.slave_id})
            await self.notifier.notify(
                "slave_disconnected", f"Slave {self.slave_id} disconnected", slave_id=self.slave_id
            )
            if self.recover is not None:
                await self._recover_session()

    async def _mark_up(self, event_type: str, message: str, **fields) -> None:
        self.connected = True
        self.on_session_state(True)
        logger.info("slave=%s %s", self.slave_id, event_type)
        await self.events.emit({"type": event_type, "slave_id": self.slave_id, **fields})
        await self.notifier.notify(event_type, message, slave_id=self.slave_id, **fields)

    async def _recover_session(self) -> None:
        """Rebuild a dead broker session, with backoff.

        Blocking the poll loop while this runs is deliberate: polling a
        session we already know is dead tells us nothing, and letting a
        second recovery start on top of the first would have two coroutines
        reconnecting the same adapter.

        The first attempt waits before trying. IB Gateway takes a minute or
        two to come back from its daily restart, so an immediate retry is
        guaranteed to fail and only burns an attempt.

        `recovery_max_attempts` is the point at which a human is told, NOT
        the point at which we stop. Stopping there would strand the hub for
        any outage longer than the schedule -- the weekly Sunday 2FA login,
        say, which can sit unacknowledged for hours. After escalating, this
        keeps retrying at the delay ceiling so a Gateway that returns an
        hour later is picked up without a hub restart.
        """
        assert self.recover is not None
        delay = self.recovery_base_delay
        attempt = 0
        escalated = False

        while True:
            attempt += 1
            await asyncio.sleep(delay)

            # An operator who paused the slave mid-recovery wants it idle,
            # not reconnected. Leave `connected` as None so the first poll
            # after resume re-baselines, same as the pause path in _loop().
            if self.is_paused():
                logger.info("slave=%s paused during recovery, standing down", self.slave_id)
                self.connected = None
                return

            try:
                await self.recover()
            except Exception as exc:
                logger.warning(
                    "slave=%s session recovery attempt %d failed: %s", self.slave_id, attempt, exc
                )
                delay = min(delay * 2, self.recovery_max_delay)
                if attempt >= self.recovery_max_attempts and not escalated:
                    escalated = True
                    await self._escalate(attempt)
                continue

            await self._mark_up(
                "slave_session_recovered",
                f"Slave {self.slave_id} session recovered after {attempt} attempt(s)",
                attempts=attempt,
            )
            return

    async def _escalate(self, attempts: int) -> None:
        """Alert once, when recovery has gone on long enough to mean trouble.

        Deliberately a different event from `slave_disconnected`: that one
        fires on every scheduled Gateway restart and is routine, so alerting
        on it would train you to ignore it. This one means the session is
        not coming back on its own schedule.
        """
        msg = (
            f"Slave {self.slave_id} session still down after {attempts} recovery attempts. "
            f"It is NOT trading. Still retrying every "
            f"{self.recovery_max_delay:.0f}s -- check IB Gateway (weekly 2FA login?)"
        )
        logger.error(msg)
        await self.events.emit(
            {"type": "slave_recovery_failed", "slave_id": self.slave_id, "attempts": attempts}
        )
        await self.notifier.notify("slave_recovery_failed", msg, slave_id=self.slave_id)

    async def _check_drawdown(self) -> None:
        for account_type in self.account_types:
            try:
                summary = await self.adapter.get_account_summary(account_type)
            except NotImplementedError:
                self._account_summary_supported = False
                return
            except AdapterError as exc:
                logger.debug("slave=%s drawdown check failed for %s: %s", self.slave_id, account_type, exc)
                continue

            peak = self._peak_equity.get(account_type)
            if peak is None or summary.equity > peak:
                self._peak_equity[account_type] = summary.equity
                self._drawdown_alerted[account_type] = False
                continue

            if peak <= 0:
                continue
            drawdown = (peak - summary.equity) / peak
            if drawdown >= self.drawdown_alert_pct and not self._drawdown_alerted.get(account_type):
                self._drawdown_alerted[account_type] = True
                label = account_type or "account"
                msg = (
                    f"Slave {self.slave_id} ({label}) drawdown {drawdown:.1%} from peak "
                    f"equity {peak:.2f} -> {summary.equity:.2f}"
                )
                logger.warning(msg)
                await self.events.emit(
                    {
                        "type": "drawdown_alert",
                        "slave_id": self.slave_id,
                        "account_type": account_type,
                        "drawdown_pct": drawdown,
                        "peak_equity": peak,
                        "equity": summary.equity,
                    }
                )
                await self.notifier.notify("drawdown_alert", msg, slave_id=self.slave_id)


class MasterWatchdog:
    """Watches ZmqReceiver.last_heartbeat freshness per master account.

    The hub's SUB/PULL sockets are bound (server-side), so there's no
    "reconnect" for the hub to do -- master EAs reconnect themselves via
    ZMQ's built-in client-side reconnect. This just detects when a given
    master account's heartbeat has gone stale and alerts, then detects
    when it resumes.
    """

    def __init__(
        self,
        receiver: ZmqReceiver,
        events: EventBus,
        notifier: Notifier,
        stale_after: float,
        poll_interval: float,
    ) -> None:
        self.receiver = receiver
        self.events = events
        self.notifier = notifier
        self.stale_after = stale_after
        self.poll_interval = poll_interval
        self._known_state: Dict[str, bool] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="watchdog-master")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.poll_interval)
            now = time.time()
            for account, last_seen in list(self.receiver.last_heartbeat.items()):
                fresh = (now - last_seen) < self.stale_after
                prev = self._known_state.get(account)

                if prev is None:
                    self._known_state[account] = fresh
                    continue

                if fresh and not prev:
                    self._known_state[account] = True
                    logger.info("master account=%s reconnected", account)
                    await self.events.emit({"type": "master_reconnected", "account": account})
                    await self.notifier.notify(
                        "master_reconnected", f"Master account {account} reconnected", account=account
                    )
                elif not fresh and prev:
                    self._known_state[account] = False
                    logger.warning("master account=%s heartbeat stale (disconnected)", account)
                    await self.events.emit({"type": "master_disconnected", "account": account})
                    await self.notifier.notify(
                        "master_disconnected", f"Master account {account} stopped sending heartbeats", account=account
                    )
