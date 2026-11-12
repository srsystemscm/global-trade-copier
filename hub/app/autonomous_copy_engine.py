import asyncio
import logging
from dataclasses import dataclass
import time
from typing import Any, Dict, Optional, Tuple

from app import trade_registry
from app.adapters.base import (
    AccountSummary,
    AdapterError, BrokerAdapter, EntryNeverFilled, OrderRequest,
)
from app.atr import compute_atr
from app.contract_specs import get_contract_spec
from app.events import EventBus
from app.models import Signal
from app.notifier import Notifier
from app.risk_controls import is_within_trading_hours
from app.session_gate import SessionGateMixin
from app.sizing import (
    resolve_balance_adjustment,
    capital_base_amount, compute_size, merge_sizing, resolve_risk_scale,
    resolve_sizing_config,
)
from app.contract_roll import roll_sequence, volume_crossover
from app import position_store
from app.routing import (
    Route, configured_targets, describe_refusal, resolve_routes,
)

logger = logging.getLogger(__name__)


@dataclass
class Refusal:
    """Why a candidate was turned away, and whether the next one may be tried.

    `overflowable` separates a capacity limit from a rule. Running out of
    netting legs or of margin on one instrument is exactly what the next
    candidate in the list is for; an opposing-direction rule or a
    correlated-risk budget is not something a sibling product escapes, and
    treating it as such would subvert the limit while appearing to honour it.
    """

    reason: str
    overflowable: bool = False


class AutonomousCopyEngine(SessionGateMixin):
    """Autonomous-mode copy engine: one instance per slave.

    Unlike mirror mode, this does not copy the master's absolute SL/TP.
    Instead, for each OPEN it:

    1. Back-calculates the ATR multiple the master's own SL/TP represent,
       using `signal.atr` (the master's own iATR() value, since fxDreema
       can't yet publish its internal ATR multiple directly -- see
       bridge_ea/TradeCopierBridge.mq4).
    2. Fans out across this slave's symbol_map (one master symbol can open
       several slave instruments, e.g. XAUUSD -> MGC and GLD).
    3. For each target, pulls that instrument's own ATR + current quote
       from the adapter and reapplies the same ATR multiple to compute a
       slave-native SL/TP.
    4. Sizes the order via the sizing engine (fixed/lot-multiplier/%risk/
       $notional) using the adapter's own account balance/equity.

    It follows MODIFY the only way it can: by re-deriving the ATR multiple
    the master's new levels represent and reapplying it to this instrument,
    the same translation OPEN uses. Copying the master's absolute price
    would put a gold CFD level on a GC futures contract.

    A background monitor loop also manages breakeven + trailing per
    position, so the two can disagree: a master that widens its stop while
    this leg has already moved to breakeven will see the monitor pull the
    stop back in on its next pass, because the monitor only ever moves a
    stop favourably. Last writer wins, as in mirror mode.

    It still follows CLOSE: when the master closes, all slave legs close.
    """

    def __init__(
        self,
        slave_id: str,
        adapter: BrokerAdapter,
        queue: "asyncio.Queue[Signal]",
        slave_config: Dict[str, Any],
        paused: bool = False,
        events: Optional[EventBus] = None,
        notifier: Optional[Notifier] = None,
    ) -> None:
        self.slave_id = slave_id
        self.adapter = adapter
        self.queue = queue
        self.paused = paused
        self.events = events
        self.notifier = notifier
        self.slave_config = slave_config
        self.atr_period = slave_config.get("atr_period", 14)
        self.risk_config = slave_config.get("risk_management", {})
        self.roll_config = slave_config.get("contract_roll", {})
        # Follow the master's MODIFY by re-deriving this leg's stops from the
        # ATR multiple the master's new levels represent. Off means the leg
        # is managed solely by the trailing monitor below, which is how this
        # engine behaved before.
        self.follow_master_modify = slave_config.get("follow_master_modify", True)
        # An open the broker cannot accept yet is held and retried rather
        # than dropped. CME halts its whole complex 17:00-18:00 ET, and an
        # MT4 server on GMT+2/+3 opens its daily bar right on that boundary
        # -- so a D1 strategy's entry lands inside the halt roughly once a
        # day. Dropped, it fills on the CFD legs and silently does not exist
        # on the futures one, with no registry row for reconciliation to
        # catch.
        defer = slave_config.get("defer_unavailable", {})
        self.defer_enabled = defer.get("enabled", True)
        self.defer_retry_seconds = defer.get("retry_seconds", 30)
        # 90 minutes covers the 60-minute halt with room for a late reopen.
        self.defer_max_seconds = defer.get("max_seconds", 5400)
        self.defer_poll_seconds = defer.get("poll_seconds", 5)
        # keyed (master_account, master_ticket, target, month_slot)
        self._deferred: Dict[tuple, dict] = {}
        # background fill collectors; held so they aren't garbage collected
        # mid-flight and so stop() can cancel them
        self._fill_tasks: set = set()
        # An exit the broker cannot take yet is held and retried, not
        # dropped. The equity session is shut for most of the day, so a CFD
        # stop hit in the evening has to be executed when the market next
        # opens -- which is what a person would do by hand.
        #
        # Separate knobs from the OPEN deferral on purpose: an exit has to
        # happen eventually, so it waits far longer. The default covers a
        # weekend, because a Friday evening close legitimately waits for
        # Monday.
        dc = slave_config.get("defer_close", {})
        self.defer_close_enabled = dc.get("enabled", True)
        self.defer_close_retry_seconds = dc.get("retry_seconds", 60)
        self.defer_close_max_seconds = dc.get("max_seconds", 259200)   # 72h
        self.defer_close_poll_seconds = dc.get("poll_seconds", 10)
        # keyed (master_account, master_ticket, slave_symbol)
        self._deferred_closes: Dict[tuple, dict] = {}
        # How much deliberate netting to permit. The broker nets, so several
        # same-direction legs become one position -- which is wanted here
        # (weekly trend, then pullback, then breakout) but has to be bounded.
        # allow_opposing defaults to false because the opposite side does not
        # open a position on a netting account, it reduces the one held.
        self.netting_policy = slave_config.get("netting", {}) or {}
        # Targets preflight found to resolve to the wrong contract size, as
        # {symbol: reason}. Treated as a per-candidate refusal, so a tier
        # steps past a blocked one and a single-target route says why.
        self.blocked_targets: Dict[str, str] = {}
        self._preflight_done = asyncio.Event()
        self._preflight_task: Optional[asyncio.Task] = None
        self.preflight_wait_seconds = float(
            slave_config.get("preflight_wait_seconds", 30.0))
        self._last_roll_check = 0.0

        # Keyed (master_account, master_ticket, slave_symbol): ticket
        # numbers are per master account, so the account has to be in the key
        # or two masters' positions collide in here as well as in the tables.
        self._positions: Dict[Tuple[str, int, str], dict] = {}
        self._consume_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._deferred_task: Optional[asyncio.Task] = None

    async def restore(self) -> None:
        """Reload positions that were open when the hub last stopped.

        Called before the engine starts consuming signals, so a restart is
        invisible to a position's lifecycle: the trailing monitor picks it
        back up and a later CLOSE still finds it.
        """
        restored = 0
        for key, pos in position_store.load_all(self.slave_id):
            _master_account, master_ticket, slave_symbol = key
            try:
                await self.adapter.restore_position(
                    pos["slave_ticket"], {**pos, "slave_symbol": slave_symbol}
                )
            except Exception as exc:
                # Keep the engine's own record either way -- losing it would
                # strand the position entirely. A failed adapter restore just
                # means the broker-side handle has to be rebuilt later.
                logger.error(
                    "slave=%s could not restore adapter state for %s slave_ticket=%s: %s",
                    self.slave_id, slave_symbol, pos["slave_ticket"], exc,
                )
            self._positions[key] = pos
            restored += 1
        if restored:
            logger.info("slave=%s restored %d open position(s) across the restart",
                        self.slave_id, restored)

    def start(self) -> None:
        self._consume_task = asyncio.create_task(self._run(), name=f"autonomous-engine-{self.slave_id}")
        self._monitor_task = asyncio.create_task(self._monitor_loop(), name=f"autonomous-monitor-{self.slave_id}")
        self._deferred_task = asyncio.create_task(
            self._deferred_loop(), name=f"autonomous-deferred-{self.slave_id}")
        self._deferred_close_task = asyncio.create_task(
            self._deferred_close_loop(),
            name=f"autonomous-deferred-close-{self.slave_id}")
        # Backgrounded so the hub starts at once; _await_preflight is what
        # keeps a signal from overtaking it.
        self._preflight_task = asyncio.create_task(
            self._preflight_routes(), name=f"autonomous-preflight-{self.slave_id}")

    async def stop(self) -> None:
        for task in (self._consume_task, self._monitor_task, self._deferred_task,
                     self._deferred_close_task, self._preflight_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for task in list(self._fill_tasks):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _emit(self, event_type: str, **fields) -> None:
        if self.events:
            await self.events.emit({"type": event_type, "slave_id": self.slave_id, **fields})

    async def _run(self) -> None:
        while True:
            signal = await self.queue.get()
            if self.paused:
                logger.debug("slave=%s paused, dropping %s ticket=%s", self.slave_id, signal.action, signal.ticket)
                continue
            if await self.refuse_while_down(signal):
                continue
            try:
                await self._handle(signal)
            except AdapterError as exc:
                logger.error(
                    "slave=%s failed to handle %s ticket=%s: %s",
                    self.slave_id, signal.action, signal.ticket, exc,
                )
                await self._emit(
                    "slave_error", master_ticket=signal.ticket, action=signal.action, message=str(exc)
                )

    async def _handle(self, signal: Signal) -> None:
        if signal.action == "MODIFY":
            await self._handle_modify(signal)
            return
        if signal.action == "OPEN":
            await self._handle_open(signal)
            return
        if signal.action == "CLOSE":
            await self._handle_close(signal)
            return

    async def _handle_open(self, signal: Signal) -> None:
        if not is_within_trading_hours():
            logger.info(
                "slave=%s skipping OPEN ticket=%s: outside configured trading hours",
                self.slave_id, signal.ticket,
            )
            await self._emit("trading_hours_blocked", master_ticket=signal.ticket)
            return

        routes = resolve_routes(self.slave_config, signal)
        if not routes:
            logger.warning("slave=%s refusing signal ticket=%s: %s",
                           self.slave_id, signal.ticket, describe_refusal(self.slave_config, signal))
            await self._emit("signal_unrouted", master_ticket=signal.ticket,
                             symbol=signal.symbol, magic=signal.magic)
            return

        if not signal.atr or signal.atr <= 0:
            logger.error(
                "slave=%s skipping OPEN ticket=%s: no usable master ATR on signal (fxDreema/EA not sending it?)",
                self.slave_id, signal.ticket,
            )
            return
        if signal.sl is None and signal.tp is None:
            logger.error(
                "slave=%s skipping OPEN ticket=%s: master signal has neither SL nor TP to back-calculate from",
                self.slave_id, signal.ticket,
            )
            return

        sl_mult = abs(signal.price - signal.sl) / signal.atr if signal.sl is not None else None
        tp_mult = abs(signal.tp - signal.price) / signal.atr if signal.tp is not None else None

        for route in routes:
            try:
                await self._open_one(signal, route, sl_mult, tp_mult)
            except AdapterError as exc:
                # Deliberately not classified by error text. IBKR's wording
                # for a closed market is not something to pin behaviour on,
                # and the alternative -- dropping anything unrecognised --
                # is the failure this exists to prevent. A genuinely bad
                # order retries until the ceiling and then says so, which is
                # noisier than dropping it and far easier to notice.
                await self._defer_open(signal, route, sl_mult, tp_mult, exc)

    def _risk_account(self, account):
        """The account figures RISK is measured against.

        Returns (summary, reason): a summary to size against, or None and
        the reason this account cannot be sized against at all.

        Margin deliberately does not come through here. What the broker
        holds is held against real equity, so a margin cap measured against
        an inflated figure would believe it had room it does not have --
        which is a margin call rather than an over-sized trade. The
        watchdog's drawdown alert stays on the real figure too: an alert
        about money that is not there is not an alert.
        """
        adjustment = resolve_balance_adjustment(self.slave_config)
        if not adjustment:
            return account, None
        balance = (account.balance or 0.0) + adjustment
        equity = (account.equity or 0.0) + adjustment
        if balance <= 0 or equity <= 0:
            return None, (
                f"balance_adjustment of {adjustment:+,.0f} would put the "
                f"account at {balance:,.0f} balance / {equity:,.0f} equity, "
                f"which is not something risk can be measured against"
            )
        return AccountSummary(balance=balance, equity=equity), None

    async def _preflight_routes(self) -> None:
        """Resolve every configured target once, before any signal needs it.

        Reports anything that will not resolve, and BLOCKS anything that
        resolves to a contract whose multiplier disagrees with its spec.
        The asymmetry is the point: an unresolvable target fails loudly at
        signal time anyway, while a wrong contract size is silent and would
        mis-size every position on that instrument.

        Nothing is quoted. Most ETF routes are shut overnight, so quote
        failures here would mean nothing, and holding 35 market-data lines
        at startup is how MES came back empty twice.
        """
        try:
            targets = configured_targets(self.slave_config)
            if not targets:
                return
            blocked, unresolved = {}, []
            for symbol, slot in targets:
                try:
                    info = await self.adapter.resolve_contract(symbol, slot)
                except NotImplementedError:
                    # This adapter has no contract resolution at all, so
                    # there is nothing here to check.
                    return
                except Exception as exc:
                    unresolved.append((symbol, slot, str(exc)[:160]))
                    continue

                spec = get_contract_spec(symbol)
                if spec is None or info.multiplier is None:
                    continue
                if abs(info.multiplier - spec.point_value) > 1e-6:
                    blocked[symbol] = (
                        f"{symbol} resolves to {info.local_symbol} with a "
                        f"multiplier of {info.multiplier:g}, but its spec says "
                        f"{spec.point_value:g} -- every position would be sized "
                        f"by the wrong contract value, so this target is blocked "
                        f"until contract_specs.py and the broker agree"
                    )

            self.blocked_targets = blocked
            for symbol, slot, exc in unresolved:
                logger.error(
                    "slave=%s route target %s (slot %s) will not resolve at the "
                    "broker: %s -- signals to it will be refused; every other "
                    "route still works",
                    self.slave_id, symbol, slot, exc,
                )
                await self._emit("route_unresolved", slave_symbol=symbol,
                                 month_slot=slot, reason=exc)
            for symbol, reason in blocked.items():
                logger.error("slave=%s %s", self.slave_id, reason)
                await self._emit("route_blocked", slave_symbol=symbol,
                                 reason=reason)
            if not unresolved and not blocked:
                logger.info(
                    "slave=%s preflight: all %d route targets resolve, and "
                    "every multiplier matches its spec",
                    self.slave_id, len(targets),
                )
        finally:
            # Set even on failure. A preflight that died must not be the
            # thing that stops the copier from trading.
            self._preflight_done.set()

    async def _await_preflight(self) -> None:
        """Hold a signal until preflight has had its say, but not for long.

        Without this, a signal arriving in the first seconds after startup
        would be sized before the multiplier check had run -- which is the
        one case the check exists for.
        """
        if self._preflight_done.is_set():
            return
        if self._preflight_task is None:
            # No preflight was ever started, so there is nothing to wait
            # for. Without this, any caller that drives the engine without
            # start() -- a test, or a one-shot replay -- sits out the whole
            # timeout on every single signal.
            return
        try:
            await asyncio.wait_for(self._preflight_done.wait(),
                                   timeout=self.preflight_wait_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "slave=%s preflight has not finished after %.0fs; sizing "
                "anyway rather than holding the signal any longer",
                self.slave_id, self.preflight_wait_seconds,
            )

    async def _size_candidate(
        self, signal: Signal, route: Route, target_symbol: str,
        sl_mult: Optional[float], tp_mult: Optional[float],
    ):
        """Price, measure and size one candidate contract.

        Returns (sized, reason): a dict when this contract can be traded, or
        None plus the reason it cannot. Everything here is per-instrument --
        a back month's own volatility is what its stops have to survive, and
        a smaller contract's lower risk per unit is the entire point of
        offering it as an alternative.
        """
        blocked = self.blocked_targets.get(target_symbol)
        if blocked:
            return None, blocked

        atr_period = route.atr_period or self.atr_period
        bar_size = route.bar_size

        try:
            bars = await self.adapter.get_price_history(
                target_symbol, atr_period, month_slot=route.month_slot,
                bar_size=bar_size)
            slave_atr = compute_atr(bars, atr_period)
        except ValueError as exc:
            return None, f"no usable ATR ({exc})"
        except AdapterError as exc:
            return None, f"no price history ({exc})"

        try:
            quote = await self.adapter.get_quote(
                target_symbol, month_slot=route.month_slot)
        except AdapterError as exc:
            return None, f"no quote ({exc})"

        traded_direction = route.target_direction or signal.direction
        sign = 1 if traded_direction == "BUY" else -1
        slave_sl = quote - sign * sl_mult * slave_atr if sl_mult is not None else None
        slave_tp = quote + sign * tp_mult * slave_atr if tp_mult is not None else None

        spec = get_contract_spec(target_symbol)
        account_type = "futures" if spec else "brokerage"
        real_account = await self.adapter.get_account_summary(account_type)
        account, reason = self._risk_account(real_account)
        if account is None:
            return None, reason

        sizing_config = merge_sizing(
            resolve_sizing_config(self.slave_config, target_symbol), route.sizing)
        risk_scale = resolve_risk_scale(self.slave_config, route.risk_scale)
        sizing_result = compute_size(
            sizing_config=sizing_config,
            master_lots=signal.lots or 0.0,
            entry_price=quote,
            stop_price=slave_sl,
            account_balance=account.balance,
            account_equity=account.equity,
            contract_spec=spec,
            risk_scale=risk_scale,
        )
        if sizing_result.skipped:
            return None, sizing_result.reason

        return {
            "symbol": target_symbol, "spec": spec, "atr": slave_atr,
            "atr_period": atr_period, "bar_size": bar_size, "quote": quote,
            "sl": slave_sl, "tp": slave_tp, "direction": traded_direction,
            "sizing_result": sizing_result, "sizing_config": sizing_config,
            # `account` carries the balance_adjustment and is what risk is
            # measured against; `real_account` is the broker's own figure,
            # for margin, which cannot be told a bigger number.
            "account": account, "real_account": real_account,
        }, None

    async def _margin_refusal(self, signal, route, chosen, account) -> Optional["Refusal"]:
        """Would this order push total initial margin past the cap?

        Only asked when a cap is configured, because whatIfOrder is a round
        trip to the broker and most signals do not need one.

        The broker's own initMarginAfter is compared directly, rather than
        adding a change to a figure tracked here: on a netting account what
        is held against a combined position is not the sum of what each leg
        would cost alone, and a cached total would be stale exactly while
        positions were moving.
        """
        policy = route.netting or self.netting_policy
        cap_pct = policy.get("max_margin_pct")
        if cap_pct is None:
            return None

        try:
            preview = await self.adapter.preview_margin(OrderRequest(
                symbol=chosen["symbol"],
                direction=chosen["direction"],
                size=chosen["sizing_result"].size,
                sl=None, tp=None,
                master_ticket=signal.ticket,
                month_slot=route.month_slot,
            ))
        except NotImplementedError:
            # The adapter cannot do this for any candidate, so trying the
            # rest of the list would be N round trips to the same answer.
            return Refusal(overflowable=False, reason=(
                f"netting.max_margin_pct is set but {type(self.adapter).__name__} "
                f"cannot preview margin, so the cap cannot be enforced"
            ))
        except AdapterError as exc:
            return Refusal(
                overflowable=True,
                reason=f"could not price the margin for {chosen['symbol']}: {exc}")

        after = preview.initial_after
        if after is None:
            return Refusal(overflowable=True, reason=(
                f"the broker gave no post-order margin figure for "
                f"{chosen['symbol']}, so netting.max_margin_pct cannot be enforced"
            ))

        equity = account.equity or account.balance
        if not equity:
            return Refusal(
                overflowable=False,
                reason="no account equity to measure margin against")

        ceiling = equity * float(cap_pct)
        if after > ceiling:
            change = preview.initial_change
            # Capacity at the broker: a cheaper instrument is the point of
            # having a list.
            return Refusal(overflowable=True, reason=(
                f"{chosen['symbol']} x{chosen['sizing_result'].size:g} would take "
                f"initial margin to {after:.0f}"
                + (f" (adding {change:.0f})" if change is not None else "")
                + f", over the {float(cap_pct):.1%} ceiling of {ceiling:.0f}. "
                f"Margin binds before risk does on this instrument."
            ))
        logger.debug(
            "slave=%s %s margin after = %.0f of %.0f allowed",
            self.slave_id, chosen["symbol"], after, ceiling,
        )
        return None

    def _leg_risk(self, pos) -> Optional[float]:
        """What one tracked leg stands to lose if its stop fills.

        None when it cannot be measured -- a leg with no stop has no bounded
        loss, which the caller has to treat as unknown rather than as zero.
        """
        sl, entry, size = pos.get("sl"), pos.get("entry_price"), pos.get("size")
        if sl is None or entry is None or not size:
            return None
        distance = abs(entry - sl)
        spec = get_contract_spec(pos.get("symbol") or "")
        if spec:
            return (distance / spec.tick_size) * spec.tick_value * size
        return distance * size

    def _netting_check(self, signal, route, chosen, account) -> Optional["Refusal"]:
        """Is this leg allowed to stack onto what is already held?

        Returns a refusal reason, or None to proceed. Everything here exists
        because the broker nets: these legs become one position, so the
        limits have to be applied across them rather than one at a time.
        """
        symbol = chosen["symbol"]
        existing = [
            (key, pos) for key, pos in self._positions.items()
            if key[2] == symbol and key[0] == signal.account
        ]
        if not existing:
            return None

        policy = route.netting or self.netting_policy

        # --- opposing legs -------------------------------------------------
        if not policy.get("allow_opposing", False):
            held = {pos.get("direction") for _, pos in existing}
            if chosen["direction"] not in held and held:
                # Never overflowable: the next product would simply be where
                # the opposing position got opened instead.
                return Refusal(overflowable=False, reason=(
                    f"already holding {', '.join(sorted(h or '?' for h in held))} "
                    f"on {symbol} and this signal is {chosen['direction']}. On a "
                    f"netting account the opposite side does not open a position, "
                    f"it REDUCES the one held -- while this leg would be tracked "
                    f"as {chosen['direction']} with its stop on the wrong side. "
                    f"Set netting.allow_opposing to override."
                ))

        # --- how many may stack -------------------------------------------
        max_legs = policy.get("max_legs")
        same_side = [p for _, p in existing if p.get("direction") == chosen["direction"]]
        if max_legs is not None and len(same_side) >= int(max_legs):
            # Capacity, so the next candidate is exactly the answer: this is
            # the case a second product is listed for.
            return Refusal(overflowable=True, reason=(
                f"already holding {len(same_side)} {chosen['direction']} leg(s) on "
                f"{symbol}, at the netting.max_legs limit of {max_legs}"
            ))

        # --- how much they may risk together ------------------------------
        cap_pct = policy.get("max_aggregate_risk_pct")
        if cap_pct is not None:
            new_risk = self._leg_risk({
                "sl": chosen["sl"], "entry_price": chosen["quote"],
                "size": chosen["sizing_result"].size, "symbol": symbol,
            })
            held_risks = [self._leg_risk(p) for _, p in existing]
            if any(r is None for r in held_risks) or new_risk is None:
                return Refusal(overflowable=False, reason=(
                    f"cannot measure the combined risk on {symbol} -- a leg has no "
                    f"stop, so netting.max_aggregate_risk_pct cannot be enforced"
                ))
            total = sum(held_risks) + new_risk
            capital = capital_base_amount(
                (route.sizing or {}).get("capital_base")
                or self.slave_config.get("sizing", {}).get("capital_base", "balance"),
                account.balance, account.equity, 0.0, 0.0,
            )
            ceiling = capital * float(cap_pct)
            if total > ceiling:
                # Not overflowable. The cap exists because several systems on
                # one symbol are a single correlated bet, and a sibling
                # product on the same underlying is the same bet -- escaping
                # the cap by changing ticker would defeat it while looking
                # like compliance.
                return Refusal(overflowable=False, reason=(
                    f"{len(existing)} leg(s) already risk {sum(held_risks):.2f} on "
                    f"{symbol}; adding {new_risk:.2f} would make {total:.2f}, over "
                    f"the {float(cap_pct):.2%} aggregate ceiling of {ceiling:.2f}. "
                    f"These net into ONE position, so the limit is across them."
                ))
        return None

    async def _open_one(
        self, signal: Signal, route: Route, sl_mult: Optional[float], tp_mult: Optional[float]
    ) -> None:
        # Candidates in declared order of preference, largest first by
        # convention. Commission is per contract, so one NG is far cheaper
        # than ten MNG for the same exposure -- but only if the account can
        # carry a whole NG, which depends on the account and therefore
        # cannot be settled when the route is written.
        await self._await_preflight()

        candidates = route.candidates or [route.target]
        rejected = []
        refused = []
        chosen = None
        for candidate in candidates:
            sized, reason = await self._size_candidate(
                signal, route, candidate, sl_mult, tp_mult)
            if sized is None:
                rejected.append((candidate, reason))
                continue

            # Inside the loop, not after it. A netting or margin refusal is
            # precisely what the next candidate is for: a book already full
            # on BITU is when the overflow product earns its place, and
            # refusing the signal there would throw away the flexibility the
            # list was written to provide.
            refusal = self._netting_check(signal, route, sized, sized["account"])
            if refusal is None:
                refusal = await self._margin_refusal(
                    signal, route, sized, sized["real_account"])
            if refusal is not None:
                rejected.append((candidate, refusal.reason))
                refused.append(refusal.reason)
                if not refusal.overflowable:
                    # A rule rather than a capacity limit. The rest of the
                    # list is not an answer to it, so stop here instead of
                    # finding a different instrument to break it on.
                    break
                continue

            chosen = sized
            break

        if chosen is None:
            logger.warning(
                "slave=%s skipping OPEN ticket=%s: nothing tradeable among "
                "%s -- %s",
                self.slave_id, signal.ticket, ", ".join(candidates),
                "; ".join("%s: %s" % r for r in rejected),
            )
            if refused:
                # Still reported as a refusal: the signal was turned away by
                # policy, not by arithmetic, and that is the thing worth
                # seeing in the ledger.
                await self._emit(
                    "netting_refused", master_ticket=signal.ticket,
                    slave_symbol=candidates[0], reason="; ".join(refused))
            return

        if rejected:
            # "Why did it take MNG today" has to be answerable afterwards.
            logger.info(
                "slave=%s chose %s over %s -- %s",
                self.slave_id, chosen["symbol"],
                ", ".join(c for c, _ in rejected),
                "; ".join("%s: %s" % r for r in rejected),
            )

        target_symbol = chosen["symbol"]
        spec = chosen["spec"]
        slave_atr = chosen["atr"]
        atr_period = chosen["atr_period"]
        bar_size = chosen["bar_size"]
        quote = chosen["quote"]
        slave_sl, slave_tp = chosen["sl"], chosen["tp"]
        traded_direction = chosen["direction"]
        sizing_result = chosen["sizing_result"]

        # Per target rather than per signal: one master signal can fan out
        # to several instruments on this slave, and a repeat must re-open
        # none of the ones that already exist without blocking any that
        # genuinely did not open. See the note in CopyEngine for why a
        # repeat reaches the engines at all.
        already = [
            m for m in trade_registry.get_open_mappings(
                self.slave_id, signal.account, signal.ticket)
            if m.slave_symbol == target_symbol
        ]
        if already:
            logger.warning(
                "slave=%s already holding %s for master_ticket=%s as slave_ticket=%s, "
                "ignoring a repeat OPEN",
                self.slave_id, target_symbol, signal.ticket, already[0].slave_ticket,
            )
            await self._emit("duplicate_open_ignored", master_ticket=signal.ticket,
                             slave_symbol=target_symbol, slave_ticket=already[0].slave_ticket)
            return

        limit_price = None
        if route.order_type == "limit":
            # Price through the quote in the direction that gets filled: up
            # for a buy, down for a sell. The offset is deliberately applied
            # to the live quote rather than to the master's price -- the two
            # instruments are unrelated, and in an evening book the quote is
            # the only thing that says what is actually tradeable.
            through = 1 + route.limit_offset_pct if traded_direction == "BUY" \
                else 1 - route.limit_offset_pct
            limit_price = quote * through

        order = OrderRequest(
            symbol=target_symbol,
            direction=traded_direction,
            size=sizing_result.size,
            sl=slave_sl,
            tp=slave_tp,
            master_ticket=signal.ticket,
            month_slot=route.month_slot,
            order_type=route.order_type,
            limit_price=limit_price,
            outside_rth=route.outside_rth,
            limit_offset_pct=route.limit_offset_pct,
            broker_brackets=route.broker_brackets,
        )
        requested_at = time.time()
        result = await self.adapter.open(order)
        filled_at = time.time()
        # master_price is the master's own entry on its own instrument, so
        # for this engine it is context rather than a slippage reference:
        # the slave is trading a different contract at a different price
        # scale. The comparable figure is slave_price against `quote`, the
        # level the stops were sized from, which is stored on the position.
        trade_registry.record_open(
            self.slave_id, signal.account, signal.ticket, target_symbol, result.slave_ticket,
            master_price=signal.price,
            slave_price=result.price,
            slave_lots=sizing_result.size,
            requested_at=requested_at,
            filled_at=filled_at,
        )
        if result.price is None:
            # The broker hasn't reported a fill yet. Collect it in the
            # background: this is the leg variance capture exists to
            # measure, so a NULL here is the measurement being lost.
            self._capture_fill("entry", signal.account, signal.ticket,
                               target_symbol, result.slave_ticket)

        # Pin the concrete contract. Slot numbers shift under you when the
        # front month rolls; an expiry doesn't, and the roll needs to know
        # exactly what it's moving from.
        try:
            info = await self.adapter.resolve_contract(target_symbol, route.month_slot)
            contract_id, contract_local, contract_expiry = (
                info.contract_id, info.local_symbol, info.expiry)
        except NotImplementedError:
            contract_id = contract_local = contract_expiry = None

        key = (signal.account, signal.ticket, target_symbol)
        self._positions[key] = {
            "slave_ticket": result.slave_ticket,
            # Carried so aggregate risk can be measured across netted legs
            # without having to reconstruct which symbol a key belongs to.
            "symbol": target_symbol,
            "entry_price": quote,
            "direction": traded_direction,
            "atr": slave_atr,
            "sl": slave_sl,
            "tp": slave_tp,
            "breakeven_done": False,
            "size": sizing_result.size,
            "month_slot": route.month_slot,
            # per-route overrides, falling back to the slave's defaults
            "risk_config": route.risk_management or self.risk_config,
            "contract_id": contract_id,
            "contract_local": contract_local,
            "contract_expiry": contract_expiry,
            # kept so a roll can re-derive stops on the new contract's own ATR
            # rather than carrying absolute prices across a calendar spread
            "sl_mult": sl_mult,
            "tp_mult": tp_mult,
            "atr_period": atr_period,
            "bar_size": bar_size,
            "direction": traded_direction,
            "sl_order_id": result.sl_order_id,
            "tp_order_id": result.tp_order_id,
        }
        position_store.save(
            self.slave_id, signal.account, signal.ticket, target_symbol, self._positions[key]
        )
        logger.info(
            "slave=%s opened %s master_ticket=%s -> slave_ticket=%s size=%s sl=%s tp=%s",
            self.slave_id, target_symbol, signal.ticket, result.slave_ticket, sizing_result.size, slave_sl, slave_tp,
        )
        if route.month_slot:
            logger.info("slave=%s   (magic=%s -> month_slot=%s)", self.slave_id, signal.magic, route.month_slot)
        await self._emit(
            "slave_open",
            master_ticket=signal.ticket,
            slave_symbol=target_symbol,
            slave_ticket=result.slave_ticket,
            size=sizing_result.size,
            sl=slave_sl,
            tp=slave_tp,
        )

    async def _handle_modify(self, signal: Signal) -> None:
        """Re-derive this leg's stops from the master's new levels.

        Mirror mode copies the master's absolute SL/TP. This engine cannot:
        the slave is a different instrument at a different price scale, so
        a gold CFD stop of 3937 is meaningless on a GC contract. What does
        carry across is the *risk distance* measured in ATR multiples --
        exactly the translation _handle_open() performs -- so a master
        widening its stop from 1.5 to 3.0 ATR widens this leg by the same
        factor against this instrument's own ATR.

        The multiples are recomputed from the master's own entry price and
        ATR, both of which ride on every signal, rather than from the ones
        captured at open. That way a master whose ATR has moved since entry
        still describes its *current* intent.
        """
        if not self.follow_master_modify:
            logger.debug(
                "slave=%s ignoring MODIFY for ticket=%s (follow_master_modify is off)",
                self.slave_id, signal.ticket,
            )
            return

        if not signal.atr or signal.atr <= 0 or signal.price is None:
            # Without the master's ATR there is no multiple to carry across,
            # and guessing one would move a real stop on a real position.
            logger.warning(
                "slave=%s cannot follow MODIFY for ticket=%s: master sent atr=%s price=%s",
                self.slave_id, signal.ticket, signal.atr, signal.price,
            )
            return

        # SIGNED, unlike _handle_open(). At open a stop is always on the
        # losing side so its absolute distance is unambiguous, but a MODIFY
        # is usually the master moving its stop toward -- and then past --
        # breakeven. Taking abs() there would read a stop 15 points ABOVE a
        # long's entry as one 15 points below it, and push this leg's stop
        # the wrong way at the exact moment the master was protecting profit.
        m = 1 if signal.direction == "BUY" else -1
        sl_mult = m * (signal.price - signal.sl) / signal.atr if signal.sl is not None else None
        tp_mult = m * (signal.tp - signal.price) / signal.atr if signal.tp is not None else None

        matched = [
            (key, pos) for key, pos in self._positions.items()
            if key[0] == signal.account and key[1] == signal.ticket
        ]
        if not matched:
            logger.warning(
                "slave=%s MODIFY for unknown master_ticket=%s (never opened here?)",
                self.slave_id, signal.ticket,
            )
            return

        for key, pos in matched:
            target_symbol = key[2]
            sign = 1 if pos["direction"] == "BUY" else -1
            entry, slave_atr = pos["entry_price"], pos["atr"]

            slave_sl = entry - sign * sl_mult * slave_atr if sl_mult is not None else None
            slave_tp = entry + sign * tp_mult * slave_atr if tp_mult is not None else None

            try:
                await self.adapter.modify(pos["slave_ticket"], slave_sl, slave_tp)
            except AdapterError as exc:
                logger.error(
                    "slave=%s could not follow MODIFY on %s slave_ticket=%s: %s",
                    self.slave_id, target_symbol, pos["slave_ticket"], exc,
                )
                continue

            pos.update({"sl": slave_sl, "tp": slave_tp,
                        "sl_mult": sl_mult, "tp_mult": tp_mult})
            position_store.save(
                self.slave_id, signal.account, signal.ticket, target_symbol, pos
            )

            logger.info(
                "slave=%s followed MODIFY on %s slave_ticket=%s -> sl=%s tp=%s "
                "(%.4f/%.4f ATR of its own instrument)",
                self.slave_id, target_symbol, pos["slave_ticket"], slave_sl, slave_tp,
                sl_mult or 0.0, tp_mult or 0.0,
            )
            await self._emit(
                "slave_modify",
                master_ticket=signal.ticket,
                slave_symbol=target_symbol,
                slave_ticket=pos["slave_ticket"],
                sl=slave_sl,
                tp=slave_tp,
            )

    async def _defer_open(self, signal, route, sl_mult, tp_mult, exc) -> None:
        """Hold an open the broker refused, to try again shortly."""
        if not self.defer_enabled:
            logger.error(
                "slave=%s could not open %s for master_ticket=%s: %s",
                self.slave_id, route.target, signal.ticket, exc,
            )
            await self._emit("slave_error", master_ticket=signal.ticket,
                             action="OPEN", message=str(exc))
            return

        key = (signal.account, signal.ticket, route.target, route.month_slot)
        if key in self._deferred:
            return

        now = time.time()
        self._deferred[key] = {
            "signal": signal, "route": route, "sl_mult": sl_mult, "tp_mult": tp_mult,
            "first_at": now, "next_at": now + self.defer_retry_seconds,
            "attempts": 1, "last_error": str(exc),
        }
        logger.warning(
            "slave=%s deferring %s for master_ticket=%s, retrying every %ss for up "
            "to %ss: %s",
            self.slave_id, route.target, signal.ticket,
            self.defer_retry_seconds, self.defer_max_seconds, exc,
        )
        await self._emit("open_deferred", master_ticket=signal.ticket,
                         slave_symbol=route.target, reason=str(exc))

    async def _deferred_loop(self) -> None:
        """Retry held opens until the broker takes them, or time runs out.

        Each retry re-runs the whole of _open_one(), so the contract is
        re-priced and re-sized against live data at the moment it actually
        fills -- not against whatever could be read during the halt. What
        carries over from the original signal is the master's intent: the
        ATR multiple its stop represented.

        These live in memory. A hub restart mid-halt loses them, which is
        the honest limit of this: it narrows a daily certainty to a rare
        coincidence rather than removing the failure outright.
        """
        while True:
            await asyncio.sleep(self.defer_poll_seconds)
            if not self._deferred:
                continue
            if not self.session_up:
                # Nothing to gain from retrying into a dead session; the
                # watchdog is rebuilding it and the ceiling still applies.
                continue

            now = time.time()
            for key, item in list(self._deferred.items()):
                if now < item["next_at"]:
                    continue

                if now - item["first_at"] > self.defer_max_seconds:
                    del self._deferred[key]
                    await self._abandon_deferred(key, item)
                    continue

                item["attempts"] += 1
                try:
                    await self._open_one(item["signal"], item["route"],
                                         item["sl_mult"], item["tp_mult"])
                except AdapterError as exc:
                    item["last_error"] = str(exc)
                    item["next_at"] = now + self.defer_retry_seconds
                    continue

                del self._deferred[key]
                waited = now - item["first_at"]
                logger.info(
                    "slave=%s opened deferred %s for master_ticket=%s after %.0fs "
                    "and %d attempt(s)",
                    self.slave_id, key[2], key[1], waited, item["attempts"],
                )
                await self._emit("deferred_open_filled", master_ticket=key[1],
                                 slave_symbol=key[2], waited_seconds=round(waited))

    async def _abandon_deferred(self, key, item) -> None:
        """Give up, loudly. The master is in this trade and this leg is not."""
        msg = (
            f"Slave {self.slave_id} never opened {key[2]} for master_ticket={key[1]} "
            f"after {self.defer_max_seconds}s and {item['attempts']} attempts. The "
            f"master IS in this trade and this leg is NOT -- check it by hand. "
            f"Last error: {item['last_error']}"
        )
        logger.error(msg)
        await self._emit("deferred_open_abandoned", master_ticket=key[1],
                         slave_symbol=key[2], attempts=item["attempts"],
                         message=item["last_error"])
        if self.notifier:
            await self.notifier.notify(
                "deferred_open_abandoned", msg, slave_id=self.slave_id,
                master_ticket=key[1], slave_symbol=key[2],
            )

    def _cancel_deferred(self, account: str, master_ticket: int) -> int:
        """Drop held opens for a ticket the master has already closed.

        Without this a deferred entry outlives the trade it belongs to and
        opens a position the master is no longer in -- the copier putting on
        risk entirely by itself, which is worse than the missed fill it was
        trying to avoid.
        """
        stale = [k for k in self._deferred if k[0] == account and k[1] == master_ticket]
        for k in stale:
            del self._deferred[k]
        return len(stale)

    async def _abandon_unfilled_entry(self, account: str, master_ticket: int,
                                      slave_symbol: str, slave_ticket: int) -> None:
        """An entry that never filled leaves nothing behind.

        Every copy of the position has to go: the broker's own order and its
        protective legs, this engine's in-memory position, the position_store
        row that would restore it across a restart, and the registry's OPEN
        status. Miss any one and a later MODIFY or CLOSE acts on a position
        that does not exist -- or a stranded GTC stop opens one.
        """
        abandon = getattr(self.adapter, "abandon_unfilled_entry", None)
        if abandon is None:
            return
        try:
            price, size = await abandon(slave_ticket)
        except Exception:
            logger.exception(
                "slave=%s failed cancelling the unfilled entry for slave_ticket=%s; "
                "CHECK THE BROKER BY HAND -- a stranded protective leg can open a "
                "position on its own", self.slave_id, slave_ticket,
            )
            return

        if price is not None:
            # It filled in the gap. The position stands; just record it.
            trade_registry.record_fill(
                self.slave_id, account, master_ticket, slave_symbol,
                slave_price=price, slave_lots=size, filled_at=time.time(),
            )
            return

        key = (account, master_ticket, slave_symbol)
        self._positions.pop(key, None)
        position_store.delete(self.slave_id, account, master_ticket, slave_symbol)
        trade_registry.record_entry_cancelled(
            self.slave_id, account, master_ticket, slave_symbol)

        msg = (
            f"Slave {self.slave_id} never filled {slave_symbol} for "
            f"master_ticket={master_ticket}; the order and its protective legs "
            f"have been cancelled. The master IS in this trade and this leg is "
            f"NOT."
        )
        logger.error(msg)
        await self._emit("entry_unfilled", master_ticket=master_ticket,
                         slave_symbol=slave_symbol, slave_ticket=slave_ticket)
        if self.notifier:
            await self.notifier.notify(
                "entry_unfilled", msg, slave_id=self.slave_id,
                master_ticket=master_ticket, slave_symbol=slave_symbol,
            )

    def _capture_fill(self, which: str, account: str, master_ticket: int,
                      slave_symbol: str, slave_ticket: int) -> None:
        """Collect a fill in the background and write it into the registry.

        Runs off the signal path on purpose. The alternative -- waiting for
        the fill inside open()/close() -- delays every later signal for this
        slave behind the broker, including the MODIFY and CLOSE of the very
        ticket being opened.

        A fill that never arrives simply leaves the column NULL, which the
        registry already treats as "not captured".
        """
        awaiter = getattr(self.adapter, f"await_{which}_fill", None)
        if awaiter is None:
            return  # adapter reports fills synchronously; nothing to collect

        async def run() -> None:
            try:
                price, size = await awaiter(slave_ticket)
            except Exception:
                logger.exception(
                    "slave=%s failed collecting the %s fill for slave_ticket=%s",
                    self.slave_id, which, slave_ticket,
                )
                return
            if price is None:
                if which == "entry":
                    # Not just "no price captured": there is no position, and
                    # a live order plus two GTC legs are still sitting at the
                    # broker.
                    await self._abandon_unfilled_entry(
                        account, master_ticket, slave_symbol, slave_ticket)
                return
            if which == "entry":
                trade_registry.record_fill(
                    self.slave_id, account, master_ticket, slave_symbol,
                    slave_price=price, slave_lots=size, filled_at=time.time(),
                )
            else:
                trade_registry.record_close(
                    self.slave_id, account, master_ticket, slave_symbol,
                    slave_close_price=price,
                )
            logger.info(
                "slave=%s %s fill for %s slave_ticket=%s: %s @ %s",
                self.slave_id, which, slave_symbol, slave_ticket, size, price,
            )

        task = asyncio.create_task(
            run(), name=f"fill-{which}-{self.slave_id}-{slave_ticket}")
        self._fill_tasks.add(task)
        task.add_done_callback(self._fill_tasks.discard)

    async def _defer_close(self, signal, mapping, exc) -> None:
        """Hold an exit the broker refused, to execute at the next open.

        Deliberately does NOT touch the position or the registry row. Until
        the close succeeds this slave really is still in the trade, and
        anything that says otherwise would hide a live position from
        reconciliation.
        """
        if not self.defer_close_enabled:
            logger.error(
                "slave=%s could not close %s slave_ticket=%s: %s",
                self.slave_id, mapping.slave_symbol, mapping.slave_ticket, exc,
            )
            await self._emit("slave_error", master_ticket=signal.ticket,
                             action="CLOSE", message=str(exc))
            return

        key = (signal.account, signal.ticket, mapping.slave_symbol)
        if key in self._deferred_closes:
            return

        now = time.time()
        self._deferred_closes[key] = {
            "signal": signal, "mapping": mapping, "first_at": now,
            "next_at": now + self.defer_close_retry_seconds,
            "attempts": 1, "last_error": str(exc),
        }
        logger.warning(
            "slave=%s deferring the close of %s slave_ticket=%s -- STILL HOLDING "
            "a position the master has exited. Retrying every %ss for up to %ss: %s",
            self.slave_id, mapping.slave_symbol, mapping.slave_ticket,
            self.defer_close_retry_seconds, self.defer_close_max_seconds, exc,
        )
        await self._emit("close_deferred", master_ticket=signal.ticket,
                         slave_symbol=mapping.slave_symbol,
                         slave_ticket=mapping.slave_ticket, reason=str(exc))

    async def _deferred_close_loop(self) -> None:
        """Retry held exits until the broker takes them.

        Each attempt goes back through adapter.close(), which re-prices a
        limit exit from a live quote -- so a close that waits for the open is
        priced at the open, not at a quote from hours earlier.

        These live in memory. A hub restart mid-wait loses them, and the
        position is then found by reconciliation against the broker rather
        than by this loop, which is the honest limit of it.
        """
        while True:
            await asyncio.sleep(self.defer_close_poll_seconds)
            if not self._deferred_closes:
                continue
            if not self.session_up:
                continue

            now = time.time()
            for key, item in list(self._deferred_closes.items()):
                if now < item["next_at"]:
                    continue

                signal, mapping = item["signal"], item["mapping"]
                if now - item["first_at"] > self.defer_close_max_seconds:
                    del self._deferred_closes[key]
                    await self._abandon_deferred_close(key, item)
                    continue

                item["attempts"] += 1
                try:
                    slave_close = await self.adapter.close(mapping.slave_ticket)
                except EntryNeverFilled as exc:
                    # The entry was cancelled in the meantime, so there is
                    # nothing to exit after all.
                    del self._deferred_closes[key]
                    logger.warning("slave=%s %s", self.slave_id, exc)
                    self._finish_close(signal, mapping, cancelled=True)
                    continue
                except AdapterError as exc:
                    item["last_error"] = str(exc)
                    item["next_at"] = now + self.defer_close_retry_seconds
                    logger.warning(
                        "slave=%s still cannot close %s slave_ticket=%s "
                        "(attempt %d, holding for %.0fs): %s",
                        self.slave_id, mapping.slave_symbol, mapping.slave_ticket,
                        item["attempts"], now - item["first_at"], exc,
                    )
                    continue

                del self._deferred_closes[key]
                waited = now - item["first_at"]
                logger.info(
                    "slave=%s closed deferred %s slave_ticket=%s after %.0fs and "
                    "%d attempt(s)",
                    self.slave_id, mapping.slave_symbol, mapping.slave_ticket,
                    waited, item["attempts"],
                )
                self._finish_close(signal, mapping, slave_close=slave_close)
                await self._emit("deferred_close_filled",
                                 master_ticket=signal.ticket,
                                 slave_symbol=mapping.slave_symbol,
                                 waited_seconds=round(waited))
                if slave_close is None:
                    self._capture_fill("exit", signal.account, signal.ticket,
                                       mapping.slave_symbol, mapping.slave_ticket)

    async def _abandon_deferred_close(self, key, item) -> None:
        """Give up, loudly, and leave every record in place.

        The position is still open as far as this hub knows, and that is the
        truth: nothing has closed it. Tidying the records away here would
        turn a position someone has to deal with into one nobody can see.
        """
        msg = (
            f"Slave {self.slave_id} NEVER CLOSED {key[2]} for master_ticket={key[1]} "
            f"after {self.defer_close_max_seconds}s and {item['attempts']} attempts. "
            f"The master is OUT of this trade and this slave is STILL IN IT -- "
            f"close it by hand. Last error: {item['last_error']}"
        )
        logger.error(msg)
        await self._emit("deferred_close_abandoned", master_ticket=key[1],
                         slave_symbol=key[2], attempts=item["attempts"],
                         message=item["last_error"])
        if self.notifier:
            await self.notifier.notify(
                "deferred_close_abandoned", msg, slave_id=self.slave_id,
                master_ticket=key[1], slave_symbol=key[2],
            )

    def _finish_close(self, signal, mapping, slave_close=None, cancelled=False) -> None:
        """Write down a close that actually happened, and forget the position."""
        if cancelled:
            trade_registry.record_entry_cancelled(
                self.slave_id, signal.account, signal.ticket, mapping.slave_symbol)
        else:
            trade_registry.record_close(
                self.slave_id, signal.account, signal.ticket, mapping.slave_symbol,
                master_close_price=signal.price or None,
                slave_close_price=slave_close,
            )
        self._positions.pop(
            (signal.account, signal.ticket, mapping.slave_symbol), None)
        position_store.delete(
            self.slave_id, signal.account, signal.ticket, mapping.slave_symbol)

    async def _handle_close(self, signal: Signal) -> None:
        dropped = self._cancel_deferred(signal.account, signal.ticket)
        if dropped:
            logger.warning(
                "slave=%s master closed ticket=%s before %d deferred open(s) could "
                "fill; dropping them rather than opening into a closed trade",
                self.slave_id, signal.ticket, dropped,
            )
            await self._emit("deferred_open_cancelled", master_ticket=signal.ticket,
                             dropped=dropped)

        mappings = trade_registry.get_open_mappings(self.slave_id, signal.account, signal.ticket)
        if not mappings:
            logger.warning(
                "slave=%s CLOSE for unknown master_ticket=%s (never opened here?)", self.slave_id, signal.ticket
            )
            return
        for mapping in mappings:
            try:
                slave_close = await self.adapter.close(mapping.slave_ticket)
            except EntryNeverFilled as exc:
                # The adapter has already cancelled the order and its legs.
                # Recorded as CANCELLED, not CLOSED: "never existed" and
                # "closed without a captured price" have to stay apart, or
                # reconciliation cannot tell a missed fill from a missing
                # measurement.
                logger.warning("slave=%s %s", self.slave_id, exc)
                trade_registry.record_entry_cancelled(
                    self.slave_id, signal.account, signal.ticket, mapping.slave_symbol)
                self._positions.pop(
                    (signal.account, signal.ticket, mapping.slave_symbol), None)
                position_store.delete(
                    self.slave_id, signal.account, signal.ticket, mapping.slave_symbol)
                await self._emit("entry_unfilled", master_ticket=signal.ticket,
                                 slave_symbol=mapping.slave_symbol,
                                 slave_ticket=mapping.slave_ticket)
                continue
            except AdapterError as exc:
                # Held, not dropped -- and nothing about the position or its
                # row is changed, because until this succeeds the slave
                # really is still in the trade.
                await self._defer_close(signal, mapping, exc)
                continue
            trade_registry.record_close(
                self.slave_id, signal.account, signal.ticket, mapping.slave_symbol,
                # 0 means the EA could not determine the exit; recorded as
                # "not captured" rather than as a price of zero.
                master_close_price=signal.price or None,
                slave_close_price=slave_close,
            )
            if slave_close is None:
                self._capture_fill("exit", signal.account, signal.ticket,
                                   mapping.slave_symbol, mapping.slave_ticket)
            self._positions.pop((signal.account, signal.ticket, mapping.slave_symbol), None)
            position_store.delete(
                self.slave_id, signal.account, signal.ticket, mapping.slave_symbol
            )
            logger.info(
                "slave=%s closed %s slave_ticket=%s (master closed)",
                self.slave_id, mapping.slave_symbol, mapping.slave_ticket,
            )
            await self._emit(
                "slave_close",
                master_ticket=signal.ticket,
                slave_symbol=mapping.slave_symbol,
                slave_ticket=mapping.slave_ticket,
            )
            if self.notifier:
                await self.notifier.notify(
                    "trade_close",
                    f"Slave {self.slave_id} closed {mapping.slave_symbol} (slave_ticket={mapping.slave_ticket})",
                    slave_id=self.slave_id,
                    master_ticket=signal.ticket,
                    slave_symbol=mapping.slave_symbol,
                    slave_ticket=mapping.slave_ticket,
                )

    async def _monitor_loop(self) -> None:
        interval = self.risk_config.get("poll_interval_seconds", 15)
        while True:
            await asyncio.sleep(interval)

            # Every check below needs the broker, so there is nothing to do
            # with a dead session but wait for the watchdog to rebuild it.
            # Skipping is also what keeps the task alive: a risk check
            # against a disconnected adapter raises AssertionError, which
            # isn't an AdapterError, so it would escape the handlers below
            # and end this task for the life of the process -- taking every
            # position's trailing stop with it, silently, and not coming
            # back when the session does.
            if not self.session_up:
                logger.debug("slave=%s broker session down, skipping monitor pass", self.slave_id)
                continue

            for key, pos in list(self._positions.items()):
                try:
                    await self._check_risk(key, pos)
                except AdapterError as exc:
                    logger.error("slave=%s risk check failed for %s: %s", self.slave_id, key, exc)
                except Exception:
                    # Belt and braces for the same failure mode: one bad
                    # position must not cost every other position its stop.
                    logger.exception("slave=%s risk check raised for %s", self.slave_id, key)
            try:
                await self._check_rolls()
            except AdapterError as exc:
                logger.error("slave=%s roll check failed: %s", self.slave_id, exc)
            except Exception:
                logger.exception("slave=%s roll check raised", self.slave_id)

    async def _check_rolls(self) -> None:
        """Coordinated per-symbol roll sweep, outermost expiry first.

        The trigger is evaluated once per symbol on the FRONT month, not per
        position. When the front is dying every position for that symbol has
        to shift together -- rolling only the front one would land it on the
        month the next strategy still holds, and on a netting account they'd
        merge.
        """
        if not self.roll_config.get("enabled", False) or not self._positions:
            return

        interval = float(self.roll_config.get("check_interval_seconds", 3600))
        now = time.time()
        if now - self._last_roll_check < interval:
            return
        self._last_roll_check = now

        by_symbol: Dict[str, list] = {}
        for key, pos in self._positions.items():
            if pos.get("contract_id") is not None:
                by_symbol.setdefault(key[2], []).append((key, pos))

        min_ratio = float(self.roll_config.get("min_volume_ratio", 1.0))
        for symbol, items in by_symbol.items():
            try:
                front = await self.adapter.get_contract_volume(symbol, month_slot=0)
                nxt = await self.adapter.get_contract_volume(symbol, month_slot=1)
            except NotImplementedError:
                continue

            decision = volume_crossover(front, nxt, min_ratio)
            logger.info("slave=%s roll check %s: %s", self.slave_id, symbol, decision.reason)
            if not decision.should_roll:
                continue

            for key, pos in roll_sequence(items):
                try:
                    await self._roll_one(key, pos, decision.reason)
                except AdapterError as exc:
                    # Stop the sweep: a half-rolled symbol is recoverable, but
                    # continuing would move the next position into the contract
                    # this one failed to vacate.
                    logger.error(
                        "slave=%s roll failed for %s (%s) -- halting this symbol's sweep: %s",
                        self.slave_id, symbol, pos.get("contract_local"), exc,
                    )
                    break

    async def _roll_one(self, key: Tuple[str, int, str], pos: dict, reason: str) -> None:
        master_account, master_ticket, symbol = key
        nxt = await self.adapter.next_contract_after(symbol, pos["contract_id"])
        if nxt is None:
            logger.error(
                "slave=%s cannot roll %s %s: no liquid contract beyond it",
                self.slave_id, symbol, pos.get("contract_local"),
            )
            return

        old_local = pos.get("contract_local")
        old_ticket = pos["slave_ticket"]

        # Stops are re-derived on the new contract's own ATR rather than
        # carried across. The calendar spread is real money (GCZ6 4051.6 vs
        # GCJ7 4118.5), so an absolute level transplanted between contracts is
        # simply wrong, and shift errors would compound over repeated rolls.
        # Whatever period measured this position at entry, not whatever
        # the slave's default happens to be now.
        roll_atr_period = pos.get("atr_period") or self.atr_period
        bars = await self.adapter.get_price_history(
            symbol, roll_atr_period, contract_id=nxt.contract_id,
            bar_size=pos.get("bar_size"),
        )
        try:
            new_atr = compute_atr(bars, roll_atr_period)
        except ValueError as exc:
            logger.error("slave=%s not rolling %s: %s", self.slave_id, symbol, exc)
            return
        quote = await self.adapter.get_quote(symbol, contract_id=nxt.contract_id)

        sign = 1 if pos["direction"] == "BUY" else -1
        sl_mult, tp_mult = pos.get("sl_mult"), pos.get("tp_mult")
        new_sl = quote - sign * sl_mult * new_atr if sl_mult is not None else None
        new_tp = quote + sign * tp_mult * new_atr if tp_mult is not None else None
        # A position already moved to breakeven keeps that protection: on the
        # new contract breakeven is the new entry, not the old price.
        if pos.get("breakeven_done") and (new_sl is None or sign * (quote - new_sl) > 0):
            new_sl = quote

        await self.adapter.close(old_ticket)
        order = OrderRequest(
            symbol=symbol,
            direction=pos["direction"],
            size=pos["size"],
            sl=new_sl,
            tp=new_tp,
            master_ticket=master_ticket,
            contract_id=nxt.contract_id,
        )
        result = await self.adapter.open(order)

        trade_registry.record_open(
            self.slave_id, master_account, master_ticket, symbol, result.slave_ticket
        )
        pos.update({
            "slave_ticket": result.slave_ticket,
            "entry_price": quote,
            "atr": new_atr,
            "sl": new_sl,
            "tp": new_tp,
            "contract_id": nxt.contract_id,
            "contract_local": nxt.local_symbol,
            "contract_expiry": nxt.expiry,
            "sl_order_id": result.sl_order_id,
            "tp_order_id": result.tp_order_id,
        })
        position_store.save(self.slave_id, master_account, master_ticket, symbol, pos)

        logger.info(
            "slave=%s rolled %s %s -> %s: slave_ticket %s -> %s, sl=%s tp=%s (%s)",
            self.slave_id, symbol, old_local, nxt.local_symbol,
            old_ticket, result.slave_ticket, new_sl, new_tp, reason,
        )
        await self._emit(
            "contract_roll",
            master_ticket=master_ticket,
            slave_symbol=symbol,
            from_contract=old_local,
            to_contract=nxt.local_symbol,
            slave_ticket=result.slave_ticket,
            sl=new_sl,
            tp=new_tp,
        )
        if self.notifier:
            await self.notifier.notify(
                "contract_roll",
                f"Slave {self.slave_id} rolled {symbol} {old_local} -> {nxt.local_symbol}",
                slave_id=self.slave_id,
                master_ticket=master_ticket,
                from_contract=old_local,
                to_contract=nxt.local_symbol,
            )

    async def _check_risk(self, key: Tuple[str, int, str], pos: dict) -> None:
        _master_account, _master_ticket, target_symbol = key
        quote = await self.adapter.get_quote(target_symbol, month_slot=pos.get("month_slot", 0))
        sign = 1 if pos["direction"] == "BUY" else -1
        favorable_move = sign * (quote - pos["entry_price"])
        atr = pos["atr"]

        risk_config = pos.get("risk_config") or self.risk_config
        be_trigger_atr = risk_config.get("breakeven_trigger_atr")
        trailing_atr = risk_config.get("trailing_atr")

        new_sl = pos["sl"]

        if be_trigger_atr and not pos["breakeven_done"] and favorable_move >= be_trigger_atr * atr:
            new_sl = pos["entry_price"]
            pos["breakeven_done"] = True

        if trailing_atr and pos["breakeven_done"]:
            candidate_sl = quote - sign * trailing_atr * atr
            if new_sl is None or sign * (candidate_sl - new_sl) > 0:
                new_sl = candidate_sl

        if new_sl is not None and new_sl != pos["sl"]:
            await self.adapter.modify(pos["slave_ticket"], new_sl, pos["tp"])
            pos["sl"] = new_sl
            position_store.save(self.slave_id, key[0], key[1], target_symbol, pos)
            logger.info(
                "slave=%s trailing %s slave_ticket=%s -> sl=%s",
                self.slave_id, target_symbol, pos["slave_ticket"], new_sl,
            )
            await self._emit(
                "slave_modify",
                master_ticket=key[1],
                slave_symbol=target_symbol,
                slave_ticket=pos["slave_ticket"],
                sl=new_sl,
                tp=pos["tp"],
            )
