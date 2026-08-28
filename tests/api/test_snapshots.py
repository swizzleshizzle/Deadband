"""Statement snapshot routes (spec section 4). All figures invented."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.snapshots import add_snapshot
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db


async def test_get_snapshot_returns_null_when_none_exists(client, conn):
    acc = await create_account(conn, name="SnapNone", venue="manual", account_type="cash")
    r = await client.get(f"/api/accounts/{acc}/snapshot", params={"as_of": "2026-07-31"})
    assert r.status_code == 200
    assert r.json()["snapshot"] is None


async def test_get_snapshot_returns_an_exact_date_match(client, conn):
    acc = await create_account(conn, name="SnapHit", venue="manual", account_type="cash")
    await add_snapshot(
        conn, acc, datetime(2026, 7, 31, tzinfo=UTC),
        cash_balance=Decimal("1180.00"), total_equity=Decimal("30000.00"), note="July",
    )
    r = await client.get(f"/api/accounts/{acc}/snapshot", params={"as_of": "2026-07-31"})
    body = r.json()
    assert_no_json_floats(body)
    assert body["snapshot"]["cash_balance"] == "1180.00"
    assert body["snapshot"]["note"] == "July"


async def test_get_snapshot_does_not_fall_back_to_an_earlier_one(client, conn):
    """latest_snapshot is `on or before` and is the WRONG function here. This
    route answers "will saving replace something?", and add_snapshot's
    ON CONFLICT fires only on an exact (account_id, as_of) match. Falling back
    would warn about replacing a July statement while entering an August one
    that replaces nothing."""
    acc = await create_account(conn, name="SnapNoFallback", venue="manual", account_type="cash")
    await add_snapshot(
        conn, acc, datetime(2026, 7, 31, tzinfo=UTC),
        cash_balance=Decimal("1180.00"), total_equity=Decimal("30000.00"),
    )
    r = await client.get(f"/api/accounts/{acc}/snapshot", params={"as_of": "2026-08-31"})
    assert r.json()["snapshot"] is None


async def test_get_snapshot_404s_on_an_unknown_account(client):
    r = await client.get(f"/api/accounts/{uuid4()}/snapshot", params={"as_of": "2026-07-31"})
    assert r.status_code == 404


async def test_get_snapshot_422s_on_an_unparseable_as_of(client, conn):
    acc = await create_account(conn, name="SnapBadDate", venue="manual", account_type="cash")
    r = await client.get(f"/api/accounts/{acc}/snapshot", params={"as_of": "not-a-date"})
    assert r.status_code == 422
