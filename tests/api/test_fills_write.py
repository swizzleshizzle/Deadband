"""POST /api/fills and DELETE /api/fills/{id} (spec section 3). All values invented."""

from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db


def _leg(symbol="ZZF", side="buy", qty="4", price="12.50"):
    return {
        "symbol": symbol, "side": side, "quantity": qty, "price": price,
        "fee": "0", "fee_currency": "USD", "executed_at": "2026-06-01T15:30:00Z",
    }


async def test_post_fills_creates_a_fill_and_regroups(client, conn):
    acc = await create_account(conn, name="ApiEntry", venue="manual", account_type="cash")
    r = await client.post("/api/fills", json={"account_id": str(acc), "fills": [_leg()]})
    assert r.status_code == 201
    body = r.json()
    assert_no_json_floats(body)
    assert len(body["fill_ids"]) == 1
    assert await conn.fetchval("SELECT count(*) FROM trade WHERE account_id = $1", acc) == 1


async def test_post_fills_writes_every_leg_in_one_transaction(client, conn):
    """Multi-leg is N fills in one request (spec E2/section 4). Four legs land
    together or not at all -- never two in and two rejected."""
    acc = await create_account(conn, name="ApiMultiLeg", venue="manual", account_type="cash")
    legs = [_leg(symbol=f"ZZL{i}") for i in range(4)]
    r = await client.post("/api/fills", json={"account_id": str(acc), "fills": legs})
    assert r.status_code == 201
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 4


async def test_post_fills_rolls_back_every_leg_when_one_is_invalid(client, conn):
    acc = await create_account(conn, name="ApiAtomic", venue="manual", account_type="cash")
    legs = [_leg(symbol="ZZG"), _leg(symbol="   ")]
    r = await client.post("/api/fills", json={"account_id": str(acc), "fills": legs})
    assert r.status_code == 422
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_post_fills_renders_quantities_as_strings(client, conn):
    acc = await create_account(conn, name="ApiStrings", venue="manual", account_type="cash")
    await client.post("/api/fills", json={"account_id": str(acc), "fills": [_leg(qty="0.00000001")]})
    got = await conn.fetchval("SELECT quantity FROM fill WHERE account_id = $1", acc)
    assert got == Decimal("0.00000001")


async def test_post_fills_404s_on_an_unknown_account(client):
    r = await client.post("/api/fills", json={"account_id": str(uuid4()), "fills": [_leg()]})
    assert r.status_code == 404


async def test_delete_fill_removes_a_manual_fill(client, conn):
    acc = await create_account(conn, name="ApiDelete", venue="manual", account_type="cash")
    posted = (await client.post("/api/fills", json={"account_id": str(acc), "fills": [_leg()]})).json()
    fill_id = posted["fill_ids"][0]
    assert (await client.delete(f"/api/fills/{fill_id}")).status_code == 204
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_delete_fill_409s_on_an_imported_fill(client, conn):
    from datetime import UTC, datetime
    from db.fills import insert_fills
    from db.instruments import upsert_instrument
    from ledger.types import AssetClass, Fill, FillSource, Instrument, Side

    acc = await create_account(conn, name="ApiImported", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZH", quote_currency="USD")
    )
    imported = Fill(
        id=uuid4(), account_id=acc, instrument_id=inst,
        executed_at=datetime(2026, 6, 1, tzinfo=UTC), side=Side.BUY,
        quantity=Decimal("1"), price=Decimal("1"), fee=Decimal("0"), fee_currency="USD",
        source=FillSource.CSV, venue_fill_id="v7", is_estimated=False,
    )
    await insert_fills(conn, [imported])
    assert (await client.delete(f"/api/fills/{imported.id}")).status_code == 409
