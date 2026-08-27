import logging
from typing import Dict, List, Optional, Tuple

import httpx

from app.adapters.base import (
    AccountSummary,
    AdapterConnectionError,
    AdapterRejectedError,
    BrokerAdapter,
    OrderRequest,
    OrderResult,
    Position,
)
from app.atr import Bar
from app.config import settings
from app.contract_specs import get_contract_spec
from app.retry import retry_async
from app.schwab_auth import SchwabAuth

logger = logging.getLogger(__name__)


class SchwabAdapter(BrokerAdapter):
    """Talks to Schwab's REST trading + market-data APIs.

    Futures and brokerage (equities/ETFs) are separate Schwab account IDs
    even under one login, so every call routes to the right account based
    on whether the symbol has a futures contract spec (see
    app/contract_specs.py) -- anything not in that table is treated as an
    equity/ETF on the brokerage account.

    NOTE: the exact JSON shapes below follow Schwab's publicly documented
    (ex-TD Ameritrade) trading API conventions, but have not been exercised
    against the live API in this environment -- verify against a real
    Schwab sandbox response before trading real size. scripts/simulate_schwab.py
    mimics this shape closely enough to exercise the hub-side logic.
    """

    def __init__(
        self,
        slave_id: str,
        auth: SchwabAuth,
        futures_account_id: str,
        brokerage_account_id: str,
        api_base: str,
        market_base: str,
        max_retries: Optional[int] = None,
        retry_base_delay: Optional[float] = None,
    ) -> None:
        self.slave_id = slave_id
        self.auth = auth
        self.futures_account_id = futures_account_id
        self.brokerage_account_id = brokerage_account_id
        self.api_base = api_base
        self.market_base = market_base
        self.max_retries = max_retries if max_retries is not None else settings.adapter_max_retries
        self.retry_base_delay = retry_base_delay if retry_base_delay is not None else settings.adapter_retry_base_delay
        self._client: Optional[httpx.AsyncClient] = None
        # slave_ticket (our synthetic position id) -> bookkeeping needed to
        # modify/close a position later without a symbol in hand
        self._positions: Dict[int, dict] = {}

    def _account_id_for(self, symbol: str) -> str:
        return self.futures_account_id if get_contract_spec(symbol) else self.brokerage_account_id

    @staticmethod
    def _futures_symbol(symbol: str) -> str:
        # Schwab/thinkorswim futures symbols are prefixed with "/", e.g. "/MGC"
        return symbol if symbol.startswith("/") else f"/{symbol}"

    async def connect(self) -> None:
        self._client = httpx.AsyncClient(timeout=15.0)
        logger.info("SchwabAdapter[%s] connected", self.slave_id)

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request_once(self, method: str, url: str, **kwargs) -> httpx.Response:
        assert self._client is not None, "adapter not connected"
        token = await self.auth.get_access_token()
        headers = {**(kwargs.pop("headers", {}) or {}), "Authorization": f"Bearer {token}"}
        try:
            resp = await self._client.request(method, url, headers=headers, **kwargs)
        except httpx.RequestError as exc:
            raise AdapterConnectionError(f"Schwab request failed: {method} {url}: {exc}") from exc
        # 5xx is Schwab's own infrastructure having a bad moment -- worth a
        # retry. 4xx means the request itself was bad; retrying won't help.
        if resp.status_code >= 500:
            raise AdapterConnectionError(f"Schwab {method} {url} -> {resp.status_code}: {resp.text}")
        if resp.status_code >= 400:
            raise AdapterRejectedError(f"Schwab {method} {url} -> {resp.status_code}: {resp.text}")
        return resp

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        return await retry_async(
            lambda: self._request_once(method, url, **kwargs),
            retries=self.max_retries,
            base_delay=self.retry_base_delay,
            retryable=(AdapterConnectionError,),
        )

    def _closing_instruction(self, direction: str, is_future: bool) -> str:
        if direction == "BUY":
            return "SELL"
        return "BUY" if is_future else "BUY_TO_COVER"

    def _opening_instruction(self, direction: str, is_future: bool) -> str:
        if direction == "BUY":
            return "BUY"
        return "SELL" if is_future else "SELL_SHORT"

    async def open(self, order: OrderRequest) -> OrderResult:
        is_future = get_contract_spec(order.symbol) is not None
        account_id = self._account_id_for(order.symbol)
        schwab_symbol = self._futures_symbol(order.symbol) if is_future else order.symbol
        asset_type = "FUTURE" if is_future else "EQUITY"
        instruction = self._opening_instruction(order.direction, is_future)

        entry_leg = {
            "instruction": instruction,
            "quantity": order.size,
            "instrument": {"symbol": schwab_symbol, "assetType": asset_type},
        }

        payload: dict = {
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE" if order.sl is None and order.tp is None else "TRIGGER",
            "orderLegCollection": [entry_leg],
        }

        closing_instruction = self._closing_instruction(order.direction, is_future)
        closing_leg = {
            "instruction": closing_instruction,
            "quantity": order.size,
            "instrument": {"symbol": schwab_symbol, "assetType": asset_type},
        }

        if order.sl is not None or order.tp is not None:
            bracket_children = []
            if order.sl is not None:
                bracket_children.append(
                    {
                        "orderType": "STOP",
                        "session": "NORMAL",
                        "duration": "GOOD_TILL_CANCEL",
                        "stopPrice": order.sl,
                        "orderStrategyType": "SINGLE",
                        "orderLegCollection": [closing_leg],
                    }
                )
            if order.tp is not None:
                bracket_children.append(
                    {
                        "orderType": "LIMIT",
                        "session": "NORMAL",
                        "duration": "GOOD_TILL_CANCEL",
                        "price": order.tp,
                        "orderStrategyType": "SINGLE",
                        "orderLegCollection": [closing_leg],
                    }
                )
            if len(bracket_children) > 1:
                child_strategy = {"orderStrategyType": "OCO", "childOrderStrategies": bracket_children}
            else:
                child_strategy = bracket_children[0]
            payload["childOrderStrategies"] = [child_strategy]

        resp = await self._request("POST", f"{self.api_base}/accounts/{account_id}/orders", json=payload)
        entry_order_id = self._extract_order_id(resp)

        self._positions[entry_order_id] = {
            "account_id": account_id,
            "symbol": schwab_symbol,
            "asset_type": asset_type,
            "direction": order.direction,
            "size": order.size,
            "is_future": is_future,
            "sl": order.sl,
            "tp": order.tp,
            "stop_order_id": None,
            "limit_order_id": None,
        }

        if order.sl is not None or order.tp is not None:
            stop_id, limit_id = await self._fetch_child_order_ids(account_id, entry_order_id)
            self._positions[entry_order_id]["stop_order_id"] = stop_id
            self._positions[entry_order_id]["limit_order_id"] = limit_id

        return OrderResult(slave_ticket=entry_order_id)

    @staticmethod
    def _extract_order_id(resp: httpx.Response) -> int:
        location = resp.headers.get("Location", "")
        if location:
            return int(location.rstrip("/").rsplit("/", 1)[-1])
        raise AdapterRejectedError("Schwab order response had no Location header to read the order ID from")

    async def _fetch_child_order_ids(
        self, account_id: str, parent_order_id: int
    ) -> Tuple[Optional[int], Optional[int]]:
        """After placing a TRIGGER+OCO bracket, Schwab only gives us the
        parent order ID via Location -- the actual STOP/LIMIT order IDs
        (needed to cancel them individually later) have to be read back.
        """
        resp = await self._request("GET", f"{self.api_base}/accounts/{account_id}/orders/{parent_order_id}")
        data = resp.json()
        stop_id: Optional[int] = None
        limit_id: Optional[int] = None
        for child in data.get("childOrderStrategies", []):
            legs = child.get("childOrderStrategies", [child])  # OCO wrapper, or a single bracket leg
            for leg in legs:
                if leg.get("orderType") == "STOP":
                    stop_id = leg.get("orderId")
                elif leg.get("orderType") == "LIMIT":
                    limit_id = leg.get("orderId")
        return stop_id, limit_id

    async def modify(self, slave_ticket: int, sl: Optional[float], tp: Optional[float]) -> None:
        pos = self._positions.get(slave_ticket)
        if pos is None:
            raise AdapterRejectedError(f"no tracked Schwab position for slave_ticket={slave_ticket}")

        # cancel whatever protective orders are currently live, then place a
        # fresh standalone OCO bracket against the already-open position
        for existing_id in (pos.get("stop_order_id"), pos.get("limit_order_id")):
            if existing_id is not None:
                await self._request(
                    "DELETE", f"{self.api_base}/accounts/{pos['account_id']}/orders/{existing_id}"
                )

        closing_instruction = self._closing_instruction(pos["direction"], pos["is_future"])
        closing_leg = {
            "instruction": closing_instruction,
            "quantity": pos["size"],
            "instrument": {"symbol": pos["symbol"], "assetType": pos["asset_type"]},
        }
        children = []
        if sl is not None:
            children.append(
                {"orderType": "STOP", "session": "NORMAL", "duration": "GOOD_TILL_CANCEL",
                 "stopPrice": sl, "orderStrategyType": "SINGLE", "orderLegCollection": [closing_leg]}
            )
        if tp is not None:
            children.append(
                {"orderType": "LIMIT", "session": "NORMAL", "duration": "GOOD_TILL_CANCEL",
                 "price": tp, "orderStrategyType": "SINGLE", "orderLegCollection": [closing_leg]}
            )
        if not children:
            pos["sl"], pos["tp"] = None, None
            pos["stop_order_id"], pos["limit_order_id"] = None, None
            return

        payload = {"orderStrategyType": "OCO", "childOrderStrategies": children} if len(children) > 1 else children[0]
        resp = await self._request("POST", f"{self.api_base}/accounts/{pos['account_id']}/orders", json=payload)
        new_order_id = self._extract_order_id(resp)

        if len(children) > 1:
            stop_id, limit_id = await self._fetch_child_order_ids(pos["account_id"], new_order_id)
        else:
            # a single STOP or LIMIT was posted directly (not OCO-wrapped),
            # so its own id IS the stop/limit id -- no child lookup needed
            stop_id = new_order_id if sl is not None else None
            limit_id = new_order_id if tp is not None else None

        pos["stop_order_id"], pos["limit_order_id"] = stop_id, limit_id
        pos["sl"], pos["tp"] = sl, tp

    async def close(self, slave_ticket: int) -> None:
        pos = self._positions.get(slave_ticket)
        if pos is None:
            raise AdapterRejectedError(f"no tracked Schwab position for slave_ticket={slave_ticket}")

        for existing_id in (pos.get("stop_order_id"), pos.get("limit_order_id")):
            if existing_id is not None:
                await self._request(
                    "DELETE", f"{self.api_base}/accounts/{pos['account_id']}/orders/{existing_id}"
                )

        closing_instruction = self._closing_instruction(pos["direction"], pos["is_future"])
        payload = {
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderStrategyType": "SINGLE",
            "orderLegCollection": [
                {
                    "instruction": closing_instruction,
                    "quantity": pos["size"],
                    "instrument": {"symbol": pos["symbol"], "assetType": pos["asset_type"]},
                }
            ],
        }
        await self._request("POST", f"{self.api_base}/accounts/{pos['account_id']}/orders", json=payload)
        del self._positions[slave_ticket]

    async def get_status(self) -> bool:
        try:
            await self.auth.get_access_token()
            return True
        except Exception:
            return False

    async def get_account_summary(self, account_type: str) -> AccountSummary:
        account_id = self.futures_account_id if account_type == "futures" else self.brokerage_account_id
        resp = await self._request("GET", f"{self.api_base}/accounts/{account_id}")
        data = resp.json()

        # Schwab's account payload nests balances under securitiesAccount or
        # futuresAccount depending on account type; try both, falling back
        # to a flat shape (used by scripts/simulate_schwab.py) for testing.
        acct = data.get("securitiesAccount") or data.get("futuresAccount") or data
        balances = acct.get("currentBalances", acct)
        balance = balances.get("cashBalance", balances.get("balance"))
        equity = balances.get("liquidationValue", balances.get("equity", balance))
        if balance is None or equity is None:
            raise AdapterRejectedError(f"could not find balance/equity fields in Schwab account response: {data}")
        return AccountSummary(balance=float(balance), equity=float(equity))

    async def get_price_history(self, symbol: str, period: int) -> List[Bar]:
        is_future = get_contract_spec(symbol) is not None
        schwab_symbol = self._futures_symbol(symbol) if is_future else symbol
        resp = await self._request(
            "GET",
            f"{self.market_base}/pricehistory",
            params={
                "symbol": schwab_symbol,
                "periodType": "month",
                "period": 1,
                "frequencyType": "daily",
                "frequency": 1,
            },
        )
        data = resp.json()
        candles = data.get("candles", [])
        return [Bar(high=c["high"], low=c["low"], close=c["close"]) for c in candles]

    async def get_quote(self, symbol: str) -> float:
        is_future = get_contract_spec(symbol) is not None
        schwab_symbol = self._futures_symbol(symbol) if is_future else symbol
        resp = await self._request("GET", f"{self.market_base}/quotes", params={"symbols": schwab_symbol})
        data = resp.json()
        entry = data.get(schwab_symbol, {})
        quote = entry.get("quote", entry)
        price = quote.get("lastPrice", quote.get("mark", quote.get("closePrice")))
        if price is None:
            raise AdapterRejectedError(f"could not find a price in Schwab quote response: {data}")
        return float(price)

    async def get_open_positions(self, account_type: str) -> List[Position]:
        account_id = self.futures_account_id if account_type == "futures" else self.brokerage_account_id
        resp = await self._request(
            "GET", f"{self.api_base}/accounts/{account_id}", params={"fields": "positions"}
        )
        data = resp.json()
        acct = data.get("securitiesAccount") or data.get("futuresAccount") or data
        positions = acct.get("positions", [])

        results = []
        for p in positions:
            symbol = p.get("instrument", {}).get("symbol", "")
            long_qty = float(p.get("longQuantity", 0))
            short_qty = float(p.get("shortQuantity", 0))
            quantity = long_qty - short_qty
            if quantity == 0:
                continue
            results.append(
                Position(
                    symbol=symbol.lstrip("/"),
                    quantity=quantity,
                    average_price=float(p.get("averagePrice", 0)),
                    market_value=p.get("marketValue"),
                    unrealized_pnl=p.get("currentDayProfitLoss", p.get("unrealizedProfitLoss")),
                )
            )
        return results
