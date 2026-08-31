"""POST /api/fills and DELETE /api/fills/{id} (spec section 3). All values invented."""

from decimal import Decimal
from uuid import uuid4

import pytest

from db.accounts import create_account
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db


def _leg(symbol="ZZF", side="buy", qty="4", price="12.50", executed_at="2026-06-01T15:30:00Z"):
    return {
        "symbol": symbol, "side": side, "quantity": qty, "price": price,
        "fee": "0", "fee_currency": "USD", "executed_at": executed_at,
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


async def test_post_fills_quote_currency_is_always_usd_regardless_of_fee_currency(client, conn):
    """Review finding 1: quote_currency is part of the instrument NATURAL KEY
    (db/instruments.py) and is a different concept from a fill's fee_currency.
    The CLI's cmd_fills_add always mints with quote_currency="USD" no matter
    what --fee-currency is; posting the same symbol here with a non-USD
    fee_currency must NOT mint a second instrument row for it."""
    acc = await create_account(conn, name="ApiQuoteCcy", venue="manual", account_type="cash")
    r1 = await client.post(
        "/api/fills",
        json={"account_id": str(acc), "fills": [_leg(symbol="ZZQ", side="buy")]},
    )
    assert r1.status_code == 201
    leg2 = _leg(symbol="ZZQ", side="sell")
    leg2["fee_currency"] = "EUR"
    r2 = await client.post("/api/fills", json={"account_id": str(acc), "fills": [leg2]})
    assert r2.status_code == 201
    assert await conn.fetchval("SELECT count(*) FROM instrument WHERE symbol = 'ZZQ'") == 1


async def test_post_fills_rolls_back_when_regroup_fails_inside_the_transaction(
    client, conn, monkeypatch
):
    """Review finding 2: the earlier atomicity test only proved the
    pre-transaction validation loop short-circuits before any DB call -- it
    would still pass with `async with conn.transaction():` deleted from the
    multi-leg path. Force a failure INSIDE the transaction (after
    add_manual_fills has already run) and assert the whole batch rolls back."""
    import api.fills as fills_module

    async def _boom(_conn, _account_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(fills_module, "regroup_account", _boom)

    acc = await create_account(conn, name="ApiTxFailure", venue="manual", account_type="cash")
    legs = [_leg(symbol=f"ZZT{i}") for i in range(3)]
    with pytest.raises(RuntimeError):
        await client.post("/api/fills", json={"account_id": str(acc), "fills": legs})
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_post_fills_422s_on_an_invalid_side(client, conn):
    """Review finding 3: the API has no argparse `choices=` to lean on the way
    the CLI does, so an invalid side must be refused as a clean 422 rather
    than raising Side(...)'s ValueError uncaught into a 500."""
    acc = await create_account(conn, name="ApiBadSide", venue="manual", account_type="cash")
    r = await client.post(
        "/api/fills",
        json={"account_id": str(acc), "fills": [_leg(side="sideways")]},
    )
    assert r.status_code == 422
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_post_fills_422s_on_an_unparseable_executed_at(client, conn):
    """Gap #75. `datetime.fromisoformat` raises ValueError -- which, unlike the
    Side(...) case above, nothing was catching -- so a fat-fingered timestamp
    left the handler as an uncaught 500 for what is plainly a bad request."""
    acc = await create_account(conn, name="ApiBadWhen", venue="manual", account_type="cash")
    r = await client.post(
        "/api/fills",
        json={"account_id": str(acc), "fills": [_leg(executed_at="yesterday-ish")]},
    )
    assert r.status_code == 422
    assert "fills[0].executed_at" in r.json()["detail"]
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_post_fills_422s_on_an_executed_at_with_no_offset(client, conn):
    """The half of gap #75 that was silent rather than loud: a naive timestamp
    parsed fine and was written into a TIMESTAMPTZ column, which stamps it as
    though the wall-clock reading were UTC. Trades are grouped by executed_at,
    so a fill shifted by the sender's offset can reorder a trade -- and no
    error is raised at any layer. Refusing it is the only way it stays
    visible; web/src/datetime.ts documents the browser-side twin of this.

    Note this box runs UTC, so a naive timestamp happens to land correctly
    here -- that is exactly why the guard cannot be verified by observing
    stored values and has to be a refusal."""
    acc = await create_account(conn, name="ApiNaiveWhen", venue="manual", account_type="cash")
    r = await client.post(
        "/api/fills",
        json={"account_id": str(acc), "fills": [_leg(executed_at="2026-06-01T15:30:00")]},
    )
    assert r.status_code == 422
    assert "offset" in r.json()["detail"]
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_post_fills_still_accepts_the_offset_forms_the_frontend_sends(client, conn):
    """The tightening must not break the only two real callers. The frontend
    sends toInstant()'s output (a Z-suffixed instant) and the CLI-shaped
    tests send explicit offsets; both must still work."""
    acc = await create_account(conn, name="ApiGoodWhen", venue="manual", account_type="cash")
    for when in ("2026-06-01T15:30:00Z", "2026-06-01T15:30:00+00:00", "2026-06-01T11:30:00-04:00"):
        r = await client.post(
            "/api/fills",
            json={"account_id": str(acc), "fills": [_leg(symbol="ZZW", executed_at=when)]},
        )
        assert r.status_code == 201, f"{when} was refused: {r.json()}"
