"""A fake broker for exercising engine paths that would otherwise only run
against a live account.

The roll and the autonomous open/close paths move real money and, until now,
had no coverage at all -- only the pure decision helpers were tested. This
stands in for an adapter so those paths can be driven deterministically.
"""
import asyncio
from typing import List, Optional

from app.adapters.base import AccountSummary, ContractInfo, OrderResult, Position
from app.atr import Bar


class FakeBroker:
    """Records what it was asked to do, in order."""

    def __init__(self, quote=4050.0, bars=None, balance=1_000_000.0, volumes=None,
                 fill_price=None, close_price=None):
        self.quote_price = quote
        self.bars = bars or [Bar(high=h, low=h - 20, close=h - 10)
                             for h in range(4100, 4130)]
        self.balance = balance
        self.volumes = volumes or {0: 30000.0, 1: 2000.0}
        # What the broker reports back as the actual execution. None is
        # the honest default: not every broker states a fill price
        # synchronously, and None must not become a price of zero.
        self.fill_price = fill_price
        self.close_price = close_price

        self.calls: List[tuple] = []
        self.opened: List[dict] = []
        self.closed: List[int] = []
        self._next_ticket = 100
        self.fail_open_on: Optional[int] = None

        # GC-like chain: each contract rolls to the next
        self.chain = [
            ContractInfo("GC", 462941472, "GCZ6", "20261229"),
            ContractInfo("GC", 765079322, "GCG7", "20270224"),
            ContractInfo("GC", 776689881, "GCJ7", "20270428"),
        ]

    async def connect(self): pass
    async def disconnect(self): pass
    async def get_status(self): return True

    async def get_account_summary(self, account_type=None):
        return AccountSummary(balance=self.balance, equity=self.balance)

    async def get_price_history(self, symbol, period, month_slot=0, contract_id=None):
        self.calls.append(("history", symbol, month_slot, contract_id))
        return self.bars

    async def get_quote(self, symbol, month_slot=0, contract_id=None):
        self.calls.append(("quote", symbol, month_slot, contract_id))
        return self.quote_price

    async def open(self, order):
        if self.fail_open_on is not None and len(self.opened) == self.fail_open_on:
            from app.adapters.base import AdapterRejectedError
            raise AdapterRejectedError("simulated rejection")
        self._next_ticket += 1
        self.opened.append({
            "symbol": order.symbol, "direction": order.direction, "size": order.size,
            "sl": order.sl, "tp": order.tp, "month_slot": order.month_slot,
            "contract_id": order.contract_id, "ticket": self._next_ticket,
        })
        self.calls.append(("open", order.symbol, order.contract_id))
        return OrderResult(slave_ticket=self._next_ticket, price=self.fill_price,
                           lots=order.size, sl_order_id=self._next_ticket + 500)

    async def close(self, slave_ticket):
        self.closed.append(slave_ticket)
        self.calls.append(("close", slave_ticket))
        return self.close_price

    async def modify(self, slave_ticket, sl, tp):
        self.calls.append(("modify", slave_ticket, sl, tp))

    async def get_open_positions(self, account_type=None) -> List[Position]:
        return []

    async def restore_position(self, slave_ticket, state): pass

    async def resolve_contract(self, symbol, month_slot=0) -> ContractInfo:
        return self.chain[month_slot]

    async def next_contract_after(self, symbol, contract_id) -> Optional[ContractInfo]:
        ids = [c.contract_id for c in self.chain]
        if contract_id not in ids:
            return self.chain[0]
        i = ids.index(contract_id) + 1
        return self.chain[i] if i < len(self.chain) else None

    async def get_contract_volume(self, symbol, month_slot=0, contract_id=None):
        return self.volumes.get(month_slot)


def run(coro):
    return asyncio.run(coro)
