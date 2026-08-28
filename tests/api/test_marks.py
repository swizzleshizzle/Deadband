"""GET /api/marks (spec section 4). All symbols and values invented."""

from datetime import UTC, datetime
from decimal import Decimal

from db.accounts import create_account
from db.instruments import upsert_instrument
from db.marks import set_mark
from db.trades import list_trades, regroup_account
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


async def _orphaned(conn, account_id, symbol, ref):
    """Give `account_id` an open, but UNVALUABLE, trade: an ordinary open
    position whose opening fill is then deleted while the trade carries user
    content (`notes`), so db/trades.py's protection step preserves it as a
    manual, orphaned row instead of reaping it outright. That leaves
    open_quantity/open_cost_basis NULL, which is exactly what
    ledger.positions.aggregate_positions reads as "open quantity unknown on
    at least one trade" and sets unvaluable_reason for.

    Same shape as tests/db/test_positions.py's `_make_orphaned_trade` --
    reused here rather than reinvented, since it is the established,
    already-exercised way to reach this state through real production paths
    (insert_fills, regroup_account, a fill delete) rather than by writing a
    position or trade row by hand. Returns the trade's id: once orphaned, the
    trade's OWN id stands in for instrument_id in open_positions' output (see
    db/positions.py), so that id -- not the original instrument id, which is
    no longer reachable -- is what a caller must check is absent.
    """
    from uuid import uuid4

    from db.fills import insert_fills

    instrument_id = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency="USD"),
    )
    fill = Fill(
        id=uuid4(), account_id=account_id, instrument_id=instrument_id,
        executed_at=datetime(2026, 6, 1, 15, 30, tzinfo=UTC), side=Side.BUY,
        quantity=Decimal("5"), price=Decimal("10"), fee=Decimal(0),
        fee_currency="USD", source=FillSource.MANUAL, venue_fill_id=ref,
        is_estimated=False,
    )
    await insert_fills(conn, [fill])
    await regroup_account(conn, account_id)
    trade = next(t for t in await list_trades(conn, account_id) if t["opening_fill_id"] == fill.id)
    assert trade["status"] == "open"

    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", trade["id"])
    await conn.execute("DELETE FROM fill WHERE id = $1", fill.id)
    await regroup_account(conn, account_id)

    protected = next(t for t in await list_trades(conn, account_id) if t["id"] == trade["id"])
    assert protected["opening_fill_id"] is None
    assert protected["status"] == "open"
    return trade["id"]


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


async def test_get_marks_excludes_a_position_that_cannot_be_valued(client, conn):
    """A mark on an unvaluable position changes nothing: api/dashboard.py
    never prices such a position against a mark in the first place (it is
    excluded from that route's own latest_marks call), so offering a mark
    input for one here would be an action that accomplishes nothing. This
    orphaned trade has unvaluable_reason == "open quantity unknown on at
    least one trade" (ledger/positions.py), and its trade id -- which stands
    in for instrument_id once its real instrument is unreachable -- must not
    appear in the payload."""
    acc = await create_account(conn, name="MarksD", venue="manual", account_type="cash")
    trade_id = await _orphaned(conn, acc, "ZZM4", ref="zzm4-1")

    body = (await client.get("/api/marks")).json()
    ids = {m["instrument_id"] for m in body["marks"]}
    assert str(trade_id) not in ids


async def test_post_marks_writes_every_row_in_one_call(client, conn):
    acc = await create_account(conn, name="MarksPost", venue="manual", account_type="cash")
    one = await _held(conn, acc, "ZZM4")
    two = await _held(conn, acc, "ZZM5")

    r = await client.post(
        "/api/marks",
        json={
            "as_of": "2026-08-01T20:00:00Z",
            "marks": [
                {"instrument_id": str(one), "price": "238.90"},
                {"instrument_id": str(two), "price": "12.05"},
            ],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert_no_json_floats(body)
    assert body["marks_set"] == 2
    assert await conn.fetchval("SELECT count(*) FROM mark") >= 2
    assert await conn.fetchval(
        "SELECT price FROM mark WHERE instrument_id = $1", one
    ) == Decimal("238.90")


async def test_post_marks_accepts_a_genuine_zero(client, conn):
    """mark_price_chk is `price >= 0`, so 0 is a legal mark -- an expired
    option is worth zero, and that is not the same as having no mark. The
    frontend's blank-means-skip rule depends on this being writable."""
    acc = await create_account(conn, name="MarksZero", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZM6")

    r = await client.post(
        "/api/marks",
        json={"as_of": "2026-08-01T20:00:00Z",
              "marks": [{"instrument_id": str(instrument_id), "price": "0"}]},
    )
    assert r.status_code == 201
    assert await conn.fetchval(
        "SELECT price FROM mark WHERE instrument_id = $1", instrument_id
    ) == Decimal("0")


async def test_post_marks_refuses_a_negative_price(client, conn):
    """mark_price_chk would refuse this in the database as an uncaught
    CheckViolationError -- a 500. It must be a 422 named to the row."""
    acc = await create_account(conn, name="MarksNeg", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZM7")

    r = await client.post(
        "/api/marks",
        json={"as_of": "2026-08-01T20:00:00Z",
              "marks": [{"instrument_id": str(instrument_id), "price": "-1"}]},
    )
    assert r.status_code == 422
    assert "marks[0].price" in r.json()["detail"]
    assert await conn.fetchval(
        "SELECT count(*) FROM mark WHERE instrument_id = $1", instrument_id
    ) == 0


async def test_post_marks_rolls_back_every_row_when_one_is_invalid(client, conn):
    acc = await create_account(conn, name="MarksAtomic", venue="manual", account_type="cash")
    good = await _held(conn, acc, "ZZM8")
    bad = await _held(conn, acc, "ZZM9")

    r = await client.post(
        "/api/marks",
        json={
            "as_of": "2026-08-01T20:00:00Z",
            "marks": [
                {"instrument_id": str(good), "price": "10"},
                {"instrument_id": str(bad), "price": "not-a-number"},
            ],
        },
    )
    assert r.status_code == 422
    assert await conn.fetchval("SELECT count(*) FROM mark WHERE instrument_id = $1", good) == 0


async def test_post_marks_refuses_an_empty_list(client):
    r = await client.post("/api/marks", json={"as_of": "2026-08-01T20:00:00Z", "marks": []})
    assert r.status_code == 422


async def test_post_marks_refuses_a_duplicate_instrument(client, conn):
    """Two rows for one instrument at one as_of would ON CONFLICT DO UPDATE
    each other inside the transaction -- last one wins, silently. Refuse."""
    acc = await create_account(conn, name="MarksDup", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZMA")

    r = await client.post(
        "/api/marks",
        json={
            "as_of": "2026-08-01T20:00:00Z",
            "marks": [
                {"instrument_id": str(instrument_id), "price": "10"},
                {"instrument_id": str(instrument_id), "price": "20"},
            ],
        },
    )
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"].lower()


async def test_post_marks_refuses_a_future_as_of(client, conn):
    acc = await create_account(conn, name="MarksFuture", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZMB")

    r = await client.post(
        "/api/marks",
        json={"as_of": "2099-01-01T00:00:00Z",
              "marks": [{"instrument_id": str(instrument_id), "price": "10"}]},
    )
    assert r.status_code == 422
    assert "future" in r.json()["detail"]


async def test_post_marks_refuses_an_as_of_without_an_offset(client, conn):
    acc = await create_account(conn, name="MarksNaive", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZMC")

    r = await client.post(
        "/api/marks",
        json={"as_of": "2026-08-01T20:00:00",
              "marks": [{"instrument_id": str(instrument_id), "price": "10"}]},
    )
    assert r.status_code == 422
    assert "offset" in r.json()["detail"]


async def test_post_marks_404s_on_an_unknown_instrument(client):
    from uuid import uuid4

    r = await client.post(
        "/api/marks",
        json={"as_of": "2026-08-01T20:00:00Z",
              "marks": [{"instrument_id": str(uuid4()), "price": "10"}]},
    )
    assert r.status_code == 404
