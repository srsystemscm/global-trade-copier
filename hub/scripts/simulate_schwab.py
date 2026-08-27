"""A fake Schwab REST server for development.

Implements just enough of Schwab's OAuth2 + trading + market-data API
surface (as used by app/adapters/schwab.py) to exercise the whole
autonomous-mode pipeline without real Schwab credentials: token exchange/
refresh, order place/replace/cancel (including reading back child OCO
bracket order IDs), account balances, price history, and quotes.

Point a slave's config at this instead of the real API:

    "schwab_auth_base": "http://127.0.0.1:8801/oauth",
    "schwab_api_base": "http://127.0.0.1:8801/trader/v1",
    "schwab_market_base": "http://127.0.0.1:8801/marketdata/v1"
"""

import argparse
import itertools
import math
import time
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, Request, Response

app = FastAPI(title="Fake Schwab")

_next_order_id = itertools.count(700001)
_orders: Dict[int, dict] = {}

ACCOUNTS = {
    "FUTURES-ACCT-1": {"balance": 50000.0, "equity": 50000.0},
    "BROKERAGE-ACCT-1": {"balance": 20000.0, "equity": 20000.0},
}

# test-only control surface: lets a test script declare "the broker currently
# shows this position open" without simulating real order fills
POSITIONS: Dict[str, list] = {"FUTURES-ACCT-1": [], "BROKERAGE-ACCT-1": []}

BASE_PRICES = {
    "/MGC": 2385.0,
    "GLD": 238.5,
}


def _base_price(symbol: str) -> float:
    return BASE_PRICES.get(symbol, 100.0)


def _assign_ids(node: Dict[str, Any]) -> None:
    for child in node.get("childOrderStrategies", []):
        _assign_ids(child)
    if "orderId" not in node:
        node["orderId"] = next(_next_order_id)


@app.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")
    if grant_type not in ("authorization_code", "refresh_token"):
        return Response(status_code=400, content='{"error":"unsupported_grant_type"}')
    return {
        "access_token": f"fake-access-{int(time.time())}",
        "refresh_token": "fake-refresh-token",
        "expires_in": 1800,
    }


@app.post("/trader/v1/accounts/{account_id}/orders")
async def place_order(account_id: str, request: Request):
    body = await request.json()
    _assign_ids(body)
    order_id = body["orderId"]
    body["status"] = "WORKING"
    body["account_id"] = account_id
    _orders[order_id] = body
    print(f"ORDER  account={account_id} id={order_id} -> {body}")
    return Response(
        status_code=201,
        headers={"Location": f"/trader/v1/accounts/{account_id}/orders/{order_id}"},
    )


@app.get("/trader/v1/accounts/{account_id}/orders/{order_id}")
async def get_order(account_id: str, order_id: int):
    return _orders.get(order_id, {})


@app.delete("/trader/v1/accounts/{account_id}/orders/{order_id}")
async def cancel_order(account_id: str, order_id: int):
    order = _orders.get(order_id)
    if order is not None:
        order["status"] = "CANCELED"
    print(f"CANCEL account={account_id} id={order_id}")
    return Response(status_code=200)


@app.get("/trader/v1/accounts/{account_id}")
async def get_account(account_id: str):
    acct = ACCOUNTS.get(account_id, {"balance": 10000.0, "equity": 10000.0})
    return {
        "securitiesAccount": {
            "currentBalances": {
                "cashBalance": acct["balance"],
                "liquidationValue": acct["equity"],
            },
            "positions": POSITIONS.get(account_id, []),
        }
    }


@app.post("/test/positions/{account_id}")
async def set_positions(account_id: str, request: Request):
    """Test-only: declare what the broker currently shows as open, for
    exercising startup reconciliation without simulating real fills.
    """
    POSITIONS[account_id] = await request.json()
    return {"status": "ok", "positions": POSITIONS[account_id]}


@app.get("/marketdata/v1/pricehistory")
async def price_history(symbol: str, periodType: str = "month", period: int = 1,
                         frequencyType: str = "daily", frequency: int = 1):
    base = _base_price(symbol)
    candles = []
    for i in range(30):
        close = base + math.sin(i / 3.0) * base * 0.01
        high = close + base * 0.003
        low = close - base * 0.003
        candles.append({"open": close, "high": high, "low": low, "close": close, "volume": 1000, "datetime": i})
    return {"symbol": symbol, "candles": candles}


@app.get("/marketdata/v1/quotes")
async def quotes(symbols: str):
    price = _base_price(symbols)
    return {symbols: {"quote": {"lastPrice": price}}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8801)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
