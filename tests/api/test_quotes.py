"""GET /api/quotes -- proposed prices for the instruments the ledger holds.

All symbols invented. No test here reaches a network: the app's quote source
is replaced with a stub, which is the same discipline
tests/test_marketdata_yahoo.py applies one layer down.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.trades import regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from marketdata.types import Quote, quantize_price
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db


class _StubSource:
    """Answers from a dict; records what it was asked for.

    Prices go through quantize_price, because every real QuoteSource does. A
    stub that skipped it would certify a payload shape no provider can
    actually produce -- and this repo has already shipped a fixture that
    matched zero real rows once.
    """

    name = "teststub"

    def __init__(self, prices: dict[str, str]):
        self._prices = prices
        self.asked: list[str] = []

    def quote(self, symbols):
        self.asked = list(symbols)
        return {
            s: Quote(
                symbol=s, price=quantize_price(self._prices[s]), currency="USD",
                quoted_at=None, source=self.name,
            )
            for s in symbols
            if s in self._prices
        }


async def _held(conn, account_id, instrument, quantity="10", price="100", ref=None):
    instrument_id = await upsert_instrument(conn, instrument)
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(), account_id=account_id, instrument_id=instrument_id,
                executed_at=datetime(2026, 6, 1, 15, 30, tzinfo=UTC), side=Side.BUY,
                quantity=Decimal(quantity), price=Decimal(price), fee=Decimal("0"),
                fee_currency="USD", source=FillSource.MANUAL,
                venue_fill_id=ref or f"q-{uuid4()}", is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, account_id)
    return instrument_id


def _equity(symbol):
    return Instrument(id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency="USD")


async def test_a_held_equity_comes_back_with_a_proposed_price(conn, api_app, client):
    acc = await create_account(conn, name="Q", venue="manual", account_type="cash")
    inst = await _held(conn, acc, _equity("ZQAA"))
    api_app.state.quote_source = _StubSource({"ZQAA": "12.34"})

    body = (await client.get("/api/quotes")).json()

    assert [q["instrument_id"] for q in body["quotes"]] == [str(inst)]
    assert body["quotes"][0]["price"] == "12.3400"
    assert body["quotes"][0]["source"] == "teststub"
    assert body["unquoted"] == []


async def test_prices_cross_the_wire_as_strings(conn, api_app, client):
    """Same rule as every other money value in this API: a JSON float would
    quietly lose the precision the NUMERIC column exists to keep."""
    acc = await create_account(conn, name="Q", venue="manual", account_type="cash")
    await _held(conn, acc, _equity("ZQAB"))
    api_app.state.quote_source = _StubSource({"ZQAB": "0.0795"})

    body = (await client.get("/api/quotes")).json()

    assert_no_json_floats(body)
    assert body["quotes"][0]["price"] == "0.0795"


async def test_an_unmappable_instrument_is_reported_with_its_reason(conn, api_app, client):
    """The blank-symbol instrument (known-gap #77) is live and appears on the
    marks screen. It must read as 'type this one yourself', not as a row that
    silently went missing."""
    acc = await create_account(conn, name="Q", venue="manual", account_type="cash")
    await conn.execute("ALTER TABLE instrument DROP CONSTRAINT instrument_symbol_not_blank")
    inst = await _held(conn, acc, _equity(""))
    api_app.state.quote_source = _StubSource({})

    body = (await client.get("/api/quotes")).json()

    assert body["quotes"] == []
    assert [u["instrument_id"] for u in body["unquoted"]] == [str(inst)]
    assert "no symbol" in body["unquoted"][0]["reason"]


async def test_a_symbol_the_provider_omits_is_reported_not_dropped(conn, api_app, client):
    """GMEWS, live: a warrant Yahoo has no listing for. Silently returning
    fewer rows than were asked about would leave the user unable to tell a
    missing quote from a position that stopped existing."""
    acc = await create_account(conn, name="Q", venue="manual", account_type="cash")
    inst = await _held(conn, acc, _equity("ZQWS"))
    api_app.state.quote_source = _StubSource({})

    body = (await client.get("/api/quotes")).json()

    assert body["quotes"] == []
    assert [u["instrument_id"] for u in body["unquoted"]] == [str(inst)]
    assert "no quote" in body["unquoted"][0]["reason"].lower()


async def test_an_option_is_asked_for_by_its_occ_symbol(conn, api_app, client):
    """Proves the mapper is actually consulted rather than the ledger symbol
    being passed through: the ledger spells this -ZQOP280121C100."""
    acc = await create_account(conn, name="Q", venue="manual", account_type="cash")
    await _held(
        conn, acc,
        Instrument(
            id=None, asset_class=AssetClass.OPTION, symbol="-ZQOP280121C100",
            quote_currency="USD", underlying="ZQOP", strike=Decimal("100"),
            expiry=date(2028, 1, 21), option_right="call",
            contract_multiplier=Decimal("100"),
        ),
        quantity="2", price="1",
    )
    stub = _StubSource({"ZQOP280121C00100000": "0.53"})
    api_app.state.quote_source = stub

    body = (await client.get("/api/quotes")).json()

    assert stub.asked == ["ZQOP280121C00100000"]
    assert body["quotes"][0]["provider_symbol"] == "ZQOP280121C00100000"
    assert body["quotes"][0]["price"] == "0.5300"


async def test_an_unvaluable_position_is_not_quoted(conn, api_app, client):
    """The set must match GET /api/marks exactly -- proposing a price for a
    row that table does not show is an action the user cannot complete."""
    acc = await create_account(conn, name="Q", venue="manual", account_type="cash")
    await _held(conn, acc, _equity("ZQAC"))
    api_app.state.quote_source = _StubSource({"ZQAC": "5"})

    quotes = (await client.get("/api/quotes")).json()
    marks = (await client.get("/api/marks")).json()

    quoted = {q["instrument_id"] for q in quotes["quotes"]}
    unquoted = {u["instrument_id"] for u in quotes["unquoted"]}
    assert quoted | unquoted == {m["instrument_id"] for m in marks["marks"]}


async def test_one_instrument_is_asked_about_once_however_many_accounts_hold_it(
    conn, api_app, client
):
    """mark's primary key is (instrument_id, as_of), so one instrument is one
    markable thing. Asking twice would spend two provider calls to write one
    row."""
    a = await create_account(conn, name="QA", venue="manual", account_type="cash")
    b = await create_account(conn, name="QB", venue="manual", account_type="cash")
    inst = Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZQAD", quote_currency="USD")
    await _held(conn, a, inst, ref="qa-1")
    await _held(conn, b, inst, ref="qb-1")
    stub = _StubSource({"ZQAD": "7"})
    api_app.state.quote_source = stub

    body = (await client.get("/api/quotes")).json()

    assert stub.asked == ["ZQAD"]
    assert len(body["quotes"]) == 1


async def test_nothing_held_means_the_provider_is_not_called(conn, api_app, client):
    stub = _StubSource({})
    api_app.state.quote_source = stub

    body = (await client.get("/api/quotes")).json()

    assert body["quotes"] == [] and body["unquoted"] == []
    assert stub.asked == []


async def test_quotes_is_reachable_without_an_identity_header(conn, api_app, anonymous_client):
    """It is a READ. Requiring identity here would be the only read route in
    the API that does, and it writes nothing -- the read pool it draws from is
    `default_transaction_read_only = on` at the Postgres level."""
    assert (await anonymous_client.get("/api/quotes")).status_code == 200


async def test_a_provider_that_raises_does_not_500_the_endpoint(conn, api_app, client):
    """yfinance scrapes an undocumented endpoint; it WILL fail. When it does,
    the screen must still render the rows so they can be typed by hand."""

    class _Broken:
        name = "broken"

        def quote(self, symbols):
            raise RuntimeError("provider exploded")

    acc = await create_account(conn, name="Q", venue="manual", account_type="cash")
    inst = await _held(conn, acc, _equity("ZQAE"))
    api_app.state.quote_source = _Broken()

    r = await client.get("/api/quotes")

    assert r.status_code == 200
    body = r.json()
    assert body["quotes"] == []
    assert [u["instrument_id"] for u in body["unquoted"]] == [str(inst)]
    assert "provider" in body["unquoted"][0]["reason"].lower()
