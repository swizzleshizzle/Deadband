from datetime import UTC, datetime
from decimal import Decimal

from db.instruments import get_multipliers, upsert_instrument
from ledger.types import AssetClass, Instrument, instrument_natural_key
from tests.conftest import requires_db

pytestmark = requires_db


def equity(symbol="SPY") -> Instrument:
    return Instrument(id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency="USD")


def option(strike="500") -> Instrument:
    return Instrument(
        id=None,
        asset_class=AssetClass.OPTION,
        symbol=f"SPY 26SEP19 {strike} C",
        quote_currency="USD",
        underlying="SPY",
        strike=Decimal(strike),
        expiry=datetime(2026, 9, 19, tzinfo=UTC).date(),
        option_right="call",
        contract_multiplier=Decimal("100"),
    )


async def test_upsert_returns_the_same_id_for_the_same_instrument(conn):
    first = await upsert_instrument(conn, equity())
    second = await upsert_instrument(conn, equity())
    assert first == second


async def test_differently_formatted_strikes_collapse_to_one_row(conn):
    # No count-based assertion (absolute or delta) can work here: the shared test
    # database is not guaranteed empty or free of this exact row (see
    # tests/db/test_importing.py header comment), and instrument has no FK back to
    # account, so committed rows from other tests/CLI runs persist independently of
    # what this test itself does. instrument.natural_key is NOT NULL UNIQUE
    # (db/schema.sql), so `a == b` alone already proves the collapse: if "500" and
    # "500.00" produced different keys they would upsert into two different rows
    # with two different ids, and the equality would fail regardless of how many
    # other instrument rows already exist. The second assertion pins the mechanism
    # directly by comparing the derived natural keys themselves, independent of the
    # database entirely.
    a = await upsert_instrument(conn, option("500"))
    b = await upsert_instrument(conn, option("500.00"))
    assert a == b
    assert instrument_natural_key(option("500")) == instrument_natural_key(option("500.00"))


async def test_different_instruments_get_different_ids(conn):
    a = await upsert_instrument(conn, equity("SPY"))
    b = await upsert_instrument(conn, equity("QQQ"))
    assert a != b


async def test_multipliers_are_fetched_for_pnl(conn):
    opt = await upsert_instrument(conn, option())
    eq = await upsert_instrument(conn, equity())
    mults = await get_multipliers(conn, [opt, eq])
    assert mults[opt] == Decimal("100")
    assert mults[eq] == Decimal("1")
