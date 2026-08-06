from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.fills import fetch_fills, insert_fills
from db.instruments import upsert_instrument
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db


async def setup_account_and_instrument(conn):
    acc = await create_account(conn, name="Test", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"),
    )
    return acc, inst


def make_fill(
    acc, inst, *, venue_fill_id=None, content_hash=None, qty="1", funding_source="external"
) -> Fill:
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal("500"),
        fee=Decimal("1"),
        fee_currency="USD",
        source=FillSource.CSV,
        venue_fill_id=venue_fill_id,
        content_hash=content_hash,
        is_estimated=False,
        funding_source=funding_source,
    )


async def test_insert_and_fetch_round_trips_decimals(conn):
    acc, inst = await setup_account_and_instrument(conn)
    result = await insert_fills(conn, [make_fill(acc, inst, venue_fill_id="v1")])
    assert result.inserted == 1
    fetched = await fetch_fills(conn, acc)
    assert fetched[0].quantity == Decimal("1")
    assert fetched[0].price == Decimal("500")
    assert isinstance(fetched[0].price, Decimal)


async def test_reimporting_the_same_venue_fill_id_is_a_no_op(conn):
    acc, inst = await setup_account_and_instrument(conn)
    await insert_fills(conn, [make_fill(acc, inst, venue_fill_id="v1")])
    result = await insert_fills(conn, [make_fill(acc, inst, venue_fill_id="v1")])
    assert result.inserted == 0
    assert result.skipped == 1
    assert len(await fetch_fills(conn, acc)) == 1


async def test_reimporting_the_same_content_hash_is_a_no_op(conn):
    acc, inst = await setup_account_and_instrument(conn)
    await insert_fills(conn, [make_fill(acc, inst, content_hash="h1")])
    result = await insert_fills(conn, [make_fill(acc, inst, content_hash="h1")])
    assert result.inserted == 0
    assert result.skipped == 1


async def test_same_venue_fill_id_in_a_different_account_is_not_a_duplicate(conn):
    acc, inst = await setup_account_and_instrument(conn)
    other = await create_account(conn, name="Other", venue="manual", account_type="cash")
    await insert_fills(conn, [make_fill(acc, inst, venue_fill_id="v1")])
    result = await insert_fills(conn, [make_fill(other, inst, venue_fill_id="v1")])
    assert result.inserted == 1


async def test_fetch_fills_round_trips_funding_source(conn):
    """_to_fill's row-to-dataclass mapping is a separate code path from the raw
    SQL SELECT used in tests/db/test_importing.py's round-trip test, and
    regroup_account reads fills through fetch_fills -- so a dropped mapping
    here would quietly mislabel every fill's funding source in any downstream
    computation. Explicit non-default values on both fills (rather than
    relying on make_fill's own default) so this cannot pass for a mapping
    that hardcodes either 'external' or 'reinvestment'."""
    acc, inst = await setup_account_and_instrument(conn)
    reinvested = make_fill(
        acc, inst, venue_fill_id="v-reinvest", funding_source="reinvestment"
    )
    external = make_fill(acc, inst, venue_fill_id="v-external", funding_source="external")
    await insert_fills(conn, [reinvested, external])

    fetched = {f.venue_fill_id: f.funding_source for f in await fetch_fills(conn, acc)}
    assert fetched == {"v-reinvest": "reinvestment", "v-external": "external"}
