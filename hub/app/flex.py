"""IBKR Flex Web Service client: the broker's own record of what happened.

This is deliberately NOT a source of live state -- the Gateway API already
gives us fills, positions and commissions as they happen. An Activity Flex
statement reports from completed statement days, so today's trades do not
appear until tomorrow's statement exists. Verified on the paper account:
a statement generated at 23:03 on 2026-11-11 covered 2026-10-12 to
2026-11-10.

What it is instead is the independent end-of-day record that the hub's own
registry gets reconciled against. Two systems that agree are worth something
only if they were written down separately.

Protocol, which is two steps and not one:

    SendRequest(token, query_id)  -> ReferenceCode + Url
    GetStatement(token, ref)      -> the statement, once IBKR has built it

Between them IBKR generates the report, so GetStatement has to tolerate
"statement generation in progress" and come back.
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"

# IBKR's own codes. Worth distinguishing because the operator response
# differs: a new token, a new query, or simply waiting.
ERROR_MEANINGS = {
    "1003": "statement could not be generated for that account",
    "1012": "the Flex token has expired -- generate a new one in Client Portal",
    "1014": "no such query id, or the query was deleted",
    "1015": "the Flex token is invalid -- it may have been regenerated",
    "1018": "too many requests; the service is rate limiting this token",
    "1019": "statement generation in progress",
    "1021": "statement generation failed",
}

IN_PROGRESS = "1019"


class FlexError(RuntimeError):
    """A Flex request that cannot be retried into success."""

    def __init__(self, code: Optional[str], message: str) -> None:
        self.code = code
        meaning = ERROR_MEANINGS.get(code or "", "")
        super().__init__(
            f"Flex error {code}: {message}" + (f" ({meaning})" if meaning else "")
        )


@dataclass
class FlexTrade:
    """One execution, as the broker recorded it."""

    account: str
    symbol: str                  # the contract, e.g. MGCZ6
    underlying: Optional[str]    # e.g. MGC
    conid: Optional[str]
    asset_category: Optional[str]
    expiry: Optional[str]
    multiplier: Optional[float]
    direction: str               # BUY | SELL
    quantity: float              # signed: negative is a sale
    price: float
    proceeds: Optional[float]    # tradeMoney
    commission: Optional[float]  # negative, as IBKR reports it
    net_cash: Optional[float]
    open_close: Optional[str]    # O | C | (blank)
    trade_id: Optional[str]
    ib_order_id: Optional[str]   # the join key back to the hub's registry
    exec_id: Optional[str]
    exchange: Optional[str]
    # Naive: IBKR reports these in the ACCOUNT'S configured report timezone,
    # which is not stated in the file and is not necessarily UTC. Anything
    # comparing these to the registry's epoch timestamps has to establish
    # that offset rather than assume it.
    trade_time: Optional[datetime]
    order_time: Optional[datetime]

    @property
    def abs_quantity(self) -> float:
        return abs(self.quantity)


@dataclass
class FlexStatement:
    account: str
    query_name: Optional[str]
    from_date: Optional[str]
    to_date: Optional[str]
    period: Optional[str]
    when_generated: Optional[str]
    trades: List[FlexTrade]


def _f(value: Optional[str]) -> Optional[float]:
    """IBKR writes an empty attribute where a number does not apply."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _dt(value: Optional[str]) -> Optional[datetime]:
    """Parse IBKR's "YYYYMMDD;HHMMSS", and its several near-misses."""
    if not value:
        return None
    for fmt in ("%Y%m%d;%H%M%S", "%Y%m%d;%H%M", "%Y%m%d %H%M%S", "%Y%m%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_statement(xml_text: str) -> FlexStatement:
    """Turn a Flex response into trades, or raise if it is an error document.

    Only levelOfDetail="EXECUTION" rows are kept. A statement also carries
    ASSET_SUMMARY and SYMBOL_SUMMARY roll-ups in the same <Trades> element
    and under the same tag names; counting those as fills would double or
    triple the position. They are dropped here rather than anywhere later,
    so no caller has to remember.
    """
    root = ET.fromstring(xml_text)

    code = root.findtext("ErrorCode")
    if code:
        raise FlexError(code, (root.findtext("ErrorMessage") or "").strip())

    stmt = root.find(".//FlexStatement")
    if stmt is None:
        raise FlexError(None, "no FlexStatement in the response")

    trades: List[FlexTrade] = []
    for el in stmt.iter():
        if el.get("levelOfDetail") != "EXECUTION":
            continue
        a = el.attrib
        qty = _f(a.get("quantity")) or 0.0
        # buySell is the broker's own statement of side; the sign of
        # quantity should agree, and where it does not, the explicit field
        # is the one to trust.
        direction = (a.get("buySell") or ("SELL" if qty < 0 else "BUY")).upper()
        trades.append(FlexTrade(
            account=a.get("accountId", ""),
            symbol=a.get("symbol", ""),
            underlying=a.get("underlyingSymbol") or None,
            conid=a.get("conid") or None,
            asset_category=a.get("assetCategory") or None,
            expiry=a.get("expiry") or None,
            multiplier=_f(a.get("multiplier")),
            direction=direction,
            quantity=qty,
            price=_f(a.get("tradePrice")) or 0.0,
            proceeds=_f(a.get("tradeMoney")),
            commission=_f(a.get("ibCommission")),
            net_cash=_f(a.get("netCash")),
            open_close=a.get("openCloseIndicator") or None,
            trade_id=a.get("tradeID") or None,
            # Only present when ticked in the query's Trades section. None
            # is reported honestly rather than filled in from tradeID, which
            # identifies an execution and not an order.
            ib_order_id=a.get("ibOrderID") or None,
            exec_id=a.get("ibExecID") or None,
            exchange=a.get("exchange") or None,
            trade_time=_dt(a.get("dateTime")),
            order_time=_dt(a.get("orderTime")),
        ))

    return FlexStatement(
        account=stmt.get("accountId", ""),
        query_name=root.get("queryName"),
        from_date=stmt.get("fromDate"),
        to_date=stmt.get("toDate"),
        period=stmt.get("period"),
        when_generated=stmt.get("whenGenerated"),
        trades=trades,
    )


class FlexClient:
    """Fetches one Activity Flex query.

    The token is never logged. It is a read-only credential -- it cannot
    trade or move money -- but it does expose the account's statements, and
    log files travel.
    """

    def __init__(
        self,
        token: str,
        query_id: str,
        base_url: str = DEFAULT_BASE,
        timeout: float = 30.0,
        poll_seconds: float = 5.0,
        max_wait_seconds: float = 120.0,
    ) -> None:
        if not token or not query_id:
            raise ValueError("Flex requires both a token and a query id")
        self.token = token
        self.query_id = query_id
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.poll_seconds = poll_seconds
        self.max_wait_seconds = max_wait_seconds

    # -- transport, isolated so tests never touch the network -------------
    def _get(self, url: str, params: dict) -> str:
        full = f"{url}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(
            full, headers={"User-Agent": "trade-copier-hub/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read().decode("utf-8", "replace")

    def fetch(self) -> FlexStatement:
        """SendRequest, then GetStatement once IBKR has built the report."""
        xml = self._get(f"{self.base_url}/SendRequest",
                        {"t": self.token, "q": self.query_id, "v": "3"})
        root = ET.fromstring(xml)
        code = root.findtext("ErrorCode")
        if code:
            raise FlexError(code, (root.findtext("ErrorMessage") or "").strip())

        ref = (root.findtext("ReferenceCode") or "").strip()
        if not ref:
            raise FlexError(None, "SendRequest returned no ReferenceCode")

        # Follow the URL the response gives rather than assuming the host.
        # Observed live: a request to ndcdyn came back pointing at gdcdyn.
        # Hardcoding the host works until they move a load balancer.
        url = (root.findtext("Url") or f"{self.base_url}/GetStatement").strip()

        logger.info("Flex query %s accepted, reference %s", self.query_id, ref)

        deadline = time.monotonic() + self.max_wait_seconds
        attempt = 0
        while True:
            attempt += 1
            body = self._get(url, {"t": self.token, "q": ref, "v": "3"})
            try:
                return parse_statement(body)
            except FlexError as exc:
                if exc.code != IN_PROGRESS:
                    raise
                if time.monotonic() >= deadline:
                    raise FlexError(
                        IN_PROGRESS,
                        f"still generating after {self.max_wait_seconds:.0f}s "
                        f"and {attempt} attempts",
                    ) from exc
                logger.info("Flex statement still generating, attempt %d", attempt)
                time.sleep(self.poll_seconds)

    async def fetch_async(self) -> FlexStatement:
        """fetch() off the event loop; it blocks and can wait on generation."""
        return await asyncio.to_thread(self.fetch)


def client_from_settings() -> Optional[FlexClient]:
    """A client, or None when Flex isn't configured -- a normal state."""
    from app.config import settings

    if not settings.flex_token or not settings.flex_query_id:
        return None
    return FlexClient(settings.flex_token, settings.flex_query_id)


def _main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    client = client_from_settings()
    if client is None:
        print("Flex is not configured: set TC_FLEX_TOKEN and TC_FLEX_QUERY_ID "
              "in hub/.env")
        return 2
    stmt = client.fetch()
    print(f"query     {stmt.query_name}")
    print(f"account   {stmt.account}")
    print(f"period    {stmt.from_date} -> {stmt.to_date} ({stmt.period})")
    print(f"generated {stmt.when_generated}")
    print(f"executions {len(stmt.trades)}")
    if stmt.trades:
        print()
        print("%-9s %-5s %6s %10s %9s %10s  %s" % (
            "symbol", "side", "qty", "price", "comm", "order id", "time"))
        for t in stmt.trades:
            print("%-9s %-5s %6g %10.4f %9s %10s  %s" % (
                t.symbol, t.direction, t.abs_quantity, t.price,
                "-" if t.commission is None else f"{t.commission:.2f}",
                t.ib_order_id or "-",
                t.trade_time.strftime("%Y-%m-%d %H:%M:%S") if t.trade_time else "-"))
    if stmt.trades and not any(t.ib_order_id for t in stmt.trades):
        print("\nNOTE: no ibOrderID on any execution. Tick it in the query's "
              "Trades section -- without it there is no exact join back to "
              "the hub's registry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
