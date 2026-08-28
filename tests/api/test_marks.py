"""GET /api/marks (spec section 4). All symbols and values invented."""

from datetime import UTC, datetime
from decimal import Decimal

from db.accounts import create_account
from db.instruments import upsert_instrument
from db.marks import set_mark
from db.trades import regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db


async def _held(conn, account_id, symbol, quantity="10", price="100"):
    """Give `account_id` an open position in `symbol` and return its
    instrument id. Goes through insert_fills + regroup_account rather than
    writing a position row directly, because open_positions derives
    positions from grouped trades -- a hand-written row would not appear."""
    from uuid import uuid4

    from db.fills import insert_fills

    instrument_id = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(), account_id=account_id, instrument_id=instrument_id,
                executed_at=datetime(2026, 6, 1, 15, 30, tzinfo=UTC), side=Side.BUY,
                quantity=Decimal(quantity), price=Decimal(price), fee=Decimal(0),
                fee_currency="USD", source=FillSource.MANUAL, venue_fill_id=None,
                is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, account_id)
    return instrument_id


async def test_get_marks_lists_a_held_instrument_with_no_mark(client, conn):
    acc = await create_account(conn, name="MarksA", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZM1")

    r = await client.get("/api/marks")
    assert r.status_code == 200
    body = r.json()
    assert_no_json_floats(body)

    row = next(m for m in body["marks"] if m["instrument_id"] == str(instrument_id))
    assert row["symbol"] == "ZZM1"
    assert row["natural_key"]
    assert row["quantity"] == "10"
    assert row["last_mark"] is None
    assert [a["name"] for a in row["accounts"]] == ["MarksA"]


async def test_get_marks_reports_an_existing_mark_with_its_age(client, conn):
    acc = await create_account(conn, name="MarksB", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZM2")
    marked_at = datetime(2026, 6, 15, 20, 0, tzinfo=UTC)
    await set_mark(conn, instrument_id, Decimal("241.50"), marked_at)

    body = (await client.get("/api/marks")).json()
    row = next(m for m in body["marks"] if m["instrument_id"] == str(instrument_id))
    assert row["last_mark"]["price"] == "241.50"
    assert row["last_mark"]["as_of"].startswith("2026-06-15")


async def test_get_marks_returns_one_row_for_an_instrument_held_in_two_accounts(client, conn):
    """A mark is keyed on instrument_id alone (mark's PRIMARY KEY), so one
    instrument must be ONE row here however many accounts hold it. Two rows
    would invite two conflicting prices for a single database row."""
    a = await create_account(conn, name="MarksC1", venue="manual", account_type="cash")
    b = await create_account(conn, name="MarksC2", venue="manual", account_type="cash")
    instrument_id = await _held(conn, a, "ZZM3", quantity="10")
    await _held(conn, b, "ZZM3", quantity="7")

    body = (await client.get("/api/marks")).json()
    rows = [m for m in body["marks"] if m["instrument_id"] == str(instrument_id)]
    assert len(rows) == 1
    assert rows[0]["quantity"] == "17"
    assert sorted(a["name"] for a in rows[0]["accounts"]) == ["MarksC1", "MarksC2"]
