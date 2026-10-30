import logging

from app.models import Signal

logger = logging.getLogger(__name__)


class SessionGateMixin:
    """Refuses signals while the broker session is down.

    Without this, a signal arriving during IB Gateway's daily restart reaches
    an adapter whose `self.ib` is None and trips `assert ... "adapter not
    connected"`. An AssertionError is not an AdapterError, so the engines'
    `except AdapterError` doesn't catch it -- it escapes the consume loop and
    kills the engine task outright, which no amount of session recovery
    brings back.

    The watchdog owns the flag (see SlaveWatchdog.on_session_state); engines
    only read it.
    """

    # Engines start optimistic: start_engine_for_slave() connects the adapter
    # before constructing them, so a live session is the normal case and a
    # failed connect never gets this far.
    session_up: bool = True

    def set_session_up(self, up: bool) -> None:
        self.session_up = up

    async def refuse_while_down(self, signal: Signal) -> bool:
        """True when the signal was refused and the caller should skip it.

        Refused signals are dropped, not queued for replay. For OPEN and
        MODIFY that is the safe choice on its own terms: a trade signal is
        only valid near the moment it fired, and filling an OPEN five
        minutes late at a different price is worse than missing it. It is
        also what `paused` already does.

        CLOSE is the painful one -- dropping it leaves the slave holding a
        position the master has exited -- but it cannot be executed with a
        dead session either, and replaying it later is genuinely unsafe:
        on IBKR's netting account a close fired against a position someone
        already flattened by hand would *open a reverse position*. So it is
        dropped too, and escalated instead, because that drift needs a human
        who can see both accounts. Recovery's reconcile pass cannot find it:
        the registry still says OPEN and the broker agrees, since nothing
        recorded the master's exit.
        """
        if self.session_up:
            return False

        logger.warning(
            "slave=%s broker session down, refusing %s ticket=%s",
            self.slave_id, signal.action, signal.ticket,
        )
        await self._emit(
            "signal_refused",
            master_ticket=signal.ticket,
            action=signal.action,
            reason="session_down",
        )

        if signal.action == "CLOSE" and self.notifier:
            msg = (
                f"Slave {self.slave_id} could NOT close master_ticket={signal.ticket} "
                f"({signal.symbol}): broker session was down. The master is flat and this "
                f"slave may still be holding the position -- check it by hand"
            )
            logger.error(msg)
            await self.notifier.notify(
                "close_refused",
                msg,
                slave_id=self.slave_id,
                master_ticket=signal.ticket,
                slave_symbol=signal.symbol,
            )

        return True
