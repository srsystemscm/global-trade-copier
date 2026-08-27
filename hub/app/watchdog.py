import asyncio
import logging
import time
from typing import Dict, List, Optional

from app.adapters.base import AdapterError, BrokerAdapter
from app.events import EventBus
from app.notifier import Notifier
from app.zmq_receiver import ZmqReceiver

logger = logging.getLogger(__name__)


class SlaveWatchdog:
    """Background per-slave monitor.

    The hub's own sockets/HTTP client don't need to "reconnect" themselves
    (MT4Adapter already rebuilds its REQ socket on timeout, Schwab's client
    just keeps making requests) -- what's missing is *noticing* a slave has
    gone dark and telling someone, then noticing it's back. That's this
    class: polls `adapter.get_status()` for connectivity, and where the
    adapter supports it, `adapter.get_account_summary()` for a simple
    peak-equity drawdown alert.
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
    ) -> None:
        self.slave_id = slave_id
        self.adapter = adapter
        self.events = events
        self.notifier = notifier
        self.account_types = account_types
        self.poll_interval = poll_interval
        self.drawdown_alert_pct = drawdown_alert_pct

        self.connected: Optional[bool] = None  # None = not checked yet
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
            self.connected = True
            logger.info("slave=%s reconnected", self.slave_id)
            await self.events.emit({"type": "slave_reconnected", "slave_id": self.slave_id})
            await self.notifier.notify(
                "slave_reconnected", f"Slave {self.slave_id} reconnected", slave_id=self.slave_id
            )
        elif not ok and self.connected:
            self.connected = False
            logger.warning("slave=%s disconnected", self.slave_id)
            await self.events.emit({"type": "slave_disconnected", "slave_id": self.slave_id})
            await self.notifier.notify(
                "slave_disconnected", f"Slave {self.slave_id} disconnected", slave_id=self.slave_id
            )

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
