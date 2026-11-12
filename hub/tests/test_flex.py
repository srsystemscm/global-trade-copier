"""Parsing the broker's own record of what happened.

The fixtures here are real responses from the paper account on 2026-11-11,
trimmed. Two properties matter most and both bit during setup:

  - a <Trades> element carries ASSET_SUMMARY and SYMBOL_SUMMARY roll-ups
    under the same tag names as the executions, so counting every row
    double- or triple-counts the position; and
  - a field that was not ticked in the query is simply absent, which has to
    read as "not captured" rather than as a value.
"""
import pytest

from app.flex import FlexClient, FlexError, parse_statement

# Real shape, including the summary rows and the missing ibOrderID.
REAL = """<FlexQueryResponse queryName="CFD-FUTS TEST" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="DUR314653" fromDate="20261012" toDate="20261110" period="Last30CalendarDays" whenGenerated="20261111;230354">
<CashReport><CashReportCurrency accountId="DUR314653" /></CashReport>
<OpenPositions>
</OpenPositions><Trades>
<AssetSummary accountId="DUR314653" assetCategory="FUT" symbol="" quantity="4" ibCommission="-10.04" levelOfDetail="ASSET_SUMMARY" />
<SymbolSummary accountId="DUR314653" assetCategory="FUT" symbol="GCZ6" quantity="2" tradePrice="4040.6" ibCommission="-5.02" buySell="BUY" levelOfDetail="SYMBOL_SUMMARY" />
<SymbolSummary accountId="DUR314653" assetCategory="FUT" symbol="GCZ6" quantity="-2" tradePrice="4039.85" ibCommission="-5.02" buySell="SELL" levelOfDetail="SYMBOL_SUMMARY" />
<Trade accountId="DUR314653" currency="USD" assetCategory="FUT" symbol="GCZ6" conid="462941472" underlyingSymbol="GC" tradeID="330723926" multiplier="100" expiry="20261229" dateTime="20261029;023253" tradeDate="20261029" exchange="COMEX" quantity="1" tradePrice="4040.7" tradeMoney="404070" ibCommission="-2.51" netCash="-932.51" openCloseIndicator="O" buySell="BUY" ibExecID="0000e1a7.6ae348a4.01.01" orderTime="20261029;023253" levelOfDetail="EXECUTION" />
<Trade accountId="DUR314653" currency="USD" assetCategory="FUT" symbol="GCZ6" conid="462941472" underlyingSymbol="GC" tradeID="330724022" multiplier="100" expiry="20261229" dateTime="20261029;023713" tradeDate="20261029" exchange="COMEX" quantity="-1" tradePrice="4037.2" tradeMoney="-403720" ibCommission="-2.51" netCash="577.49" openCloseIndicator="C" buySell="SELL" ibExecID="0000e1a7.6ae3494c.01.01" orderTime="20261029;023713" levelOfDetail="EXECUTION" />
</Trades>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""

WITH_ORDER_ID = REAL.replace(
    'tradeID="330723926"', 'tradeID="330723926" ibOrderID="166000617"')

EMPTY = """<FlexQueryResponse queryName="CFD-FUTS TEST" type="AF">
<FlexStatements count="1">
<FlexStatement accountId="DUR314653" fromDate="20261110" toDate="20261110" period="LastBusinessDay" whenGenerated="20261111;224106">
<CashReport><CashReportCurrency accountId="DUR314653" /></CashReport>
<OpenPositions>
</OpenPositions><Trades>
</Trades>
</FlexStatement>
</FlexStatements>
</FlexQueryResponse>"""

ERROR = """<FlexStatementResponse timestamp="11 November, 2026 11:03 PM EST">
<Status>Fail</Status><ErrorCode>1012</ErrorCode>
<ErrorMessage>Token has expired.</ErrorMessage>
</FlexStatementResponse>"""

IN_PROGRESS_XML = """<FlexStatementResponse timestamp="x">
<Status>Warn</Status><ErrorCode>1019</ErrorCode>
<ErrorMessage>Statement generation in progress. Please try again shortly.</ErrorMessage>
</FlexStatementResponse>"""

SEND_OK = """<FlexStatementResponse timestamp="x">
<Status>Success</Status><ReferenceCode>5359300738</ReferenceCode>
<Url>https://gdcdyn.interactivebrokers.com/AccountManagement/FlexWebService/GetStatement</Url>
</FlexStatementResponse>"""


# ---------------- parsing ------------------------------------------------

def test_only_executions_are_counted():
    """The roll-ups share the <Trades> element and the same attributes. Two
    executions plus three summary rows must read as two fills, not five."""
    stmt = parse_statement(REAL)
    assert len(stmt.trades) == 2
    assert [t.direction for t in stmt.trades] == ["BUY", "SELL"]


def test_the_statement_header_is_read():
    stmt = parse_statement(REAL)
    assert stmt.account == "DUR314653"
    assert stmt.query_name == "CFD-FUTS TEST"
    assert (stmt.from_date, stmt.to_date) == ("20261012", "20261110")
    assert stmt.period == "Last30CalendarDays"
    assert stmt.when_generated == "20261111;230354"


def test_an_execution_is_fully_parsed():
    t = parse_statement(REAL).trades[0]
    assert t.symbol == "GCZ6"
    assert t.underlying == "GC"
    assert t.conid == "462941472"
    assert t.asset_category == "FUT"
    assert t.expiry == "20261229"
    assert t.multiplier == 100
    assert t.direction == "BUY"
    assert t.quantity == 1
    assert t.price == 4040.7
    assert t.commission == -2.51
    assert t.net_cash == -932.51
    assert t.open_close == "O"
    assert t.trade_id == "330723926"
    assert t.trade_time.year == 2026 and t.trade_time.hour == 2


def test_a_sale_keeps_its_sign_and_states_its_side():
    t = parse_statement(REAL).trades[1]
    assert t.quantity == -1          # signed, as IBKR reports it
    assert t.abs_quantity == 1       # and absolute where size is wanted
    assert t.direction == "SELL"


def test_a_field_not_ticked_reads_as_not_captured():
    """ibOrderID is absent until it is selected in the query. It must not be
    filled in from tradeID, which identifies an execution, not an order."""
    assert parse_statement(REAL).trades[0].ib_order_id is None
    assert parse_statement(WITH_ORDER_ID).trades[0].ib_order_id == "166000617"


def test_an_empty_statement_is_valid_not_an_error():
    """A quiet period is a legitimate answer, and must not look like a
    failure -- otherwise every weekend pull raises."""
    stmt = parse_statement(EMPTY)
    assert stmt.trades == []
    assert stmt.account == "DUR314653"


def test_an_error_document_raises_with_the_code_and_its_meaning():
    with pytest.raises(FlexError) as e:
        parse_statement(ERROR)
    assert e.value.code == "1012"
    assert "expired" in str(e.value).lower()
    # the operator needs to know what to DO about it
    assert "Client Portal" in str(e.value)


def test_a_response_with_no_statement_raises():
    with pytest.raises(FlexError):
        parse_statement("<FlexQueryResponse></FlexQueryResponse>")


# ---------------- the two-step fetch ------------------------------------

class _Transport:
    """Records the calls and replays scripted bodies."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, params))
        return self.bodies.pop(0)


# Distinctive on purpose: a short value like "tok" is a substring of the
# word "token" in the error text, so the leak test would fail on its own
# wording rather than on a real leak.
FAKE_TOKEN = "999888777666555444333222"


def _client(*bodies, **kw):
    kw.setdefault("poll_seconds", 0)
    c = FlexClient(FAKE_TOKEN, "1705360", **kw)
    c._get = _Transport(*bodies)
    return c


def test_fetch_follows_the_url_the_response_gives():
    """Observed live: a request to ndcdyn came back pointing at gdcdyn.
    Hardcoding the host works until they move a load balancer."""
    c = _client(SEND_OK, REAL)
    stmt = c.fetch()
    assert len(stmt.trades) == 2
    send_url, send_params = c._get.calls[0]
    get_url, get_params = c._get.calls[1]
    assert send_url.endswith("/SendRequest")
    assert send_params["q"] == "1705360"
    assert get_url.startswith("https://gdcdyn.interactivebrokers.com")
    assert get_params["q"] == "5359300738"     # the reference, not the query


def test_fetch_waits_out_statement_generation():
    c = _client(SEND_OK, IN_PROGRESS_XML, IN_PROGRESS_XML, REAL)
    assert len(c.fetch().trades) == 2
    assert len(c._get.calls) == 4


def test_fetch_gives_up_on_generation_eventually():
    c = _client(SEND_OK, IN_PROGRESS_XML, IN_PROGRESS_XML,
                max_wait_seconds=0)
    with pytest.raises(FlexError) as e:
        c.fetch()
    assert e.value.code == "1019"


def test_a_send_error_is_not_retried():
    """An expired token will not fix itself; retrying just burns the rate
    limit and delays the real message."""
    c = _client(ERROR)
    with pytest.raises(FlexError) as e:
        c.fetch()
    assert e.value.code == "1012"
    assert len(c._get.calls) == 1


def test_a_send_without_a_reference_code_raises():
    c = _client("<FlexStatementResponse><Status>Success</Status></FlexStatementResponse>")
    with pytest.raises(FlexError):
        c.fetch()


def test_a_client_needs_both_credentials():
    for token, query in (("", "1"), ("t", ""), ("", "")):
        with pytest.raises(ValueError):
            FlexClient(token, query)


def test_the_token_is_never_in_the_repr_or_errors():
    """Log files travel; the token is read-only but it exposes statements."""
    c = _client(ERROR)
    with pytest.raises(FlexError) as e:
        c.fetch()
    assert FAKE_TOKEN not in str(e.value)
    assert FAKE_TOKEN not in repr(e.value)


# ---------------- configuration -----------------------------------------

def test_no_client_when_flex_is_unconfigured(monkeypatch):
    """Running without Flex is a normal state, not a half-finished one."""
    from app import flex
    from app.config import settings

    monkeypatch.setattr(settings, "flex_token", "", raising=False)
    monkeypatch.setattr(settings, "flex_query_id", "", raising=False)
    assert flex.client_from_settings() is None

    monkeypatch.setattr(settings, "flex_token", "t", raising=False)
    monkeypatch.setattr(settings, "flex_query_id", "q", raising=False)
    assert flex.client_from_settings() is not None
