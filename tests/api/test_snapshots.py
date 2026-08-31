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


def _body(account_id, **over):
    body = {
        "account_id": str(account_id), "as_of": "2026-07-31",
        "cash_balance": "1204.11", "total_equity": "30184.22", "note": "July statement",
    }
    body.update(over)
    return body


async def test_post_snapshot_stores_the_figures_in_the_right_columns(client, conn):
    """The transposition guard, end to end. add_snapshot's cash/equity are
    keyword-only because swapping them positionally stores each as the other,
    raises nothing, and surfaces days later as drift on both lines at once."""
    acc = await create_account(conn, name="SnapPost", venue="manual", account_type="cash")
    r = await client.post("/api/snapshots", json=_body(acc))
    assert r.status_code == 201
    body = r.json()
    assert_no_json_floats(body)
    assert body["replaced"] is False

    row = await conn.fetchrow(
        "SELECT cash_balance, total_equity FROM account_snapshot WHERE account_id = $1", acc
    )
    assert row["cash_balance"] == Decimal("1204.11")
    assert row["total_equity"] == Decimal("30184.22")


async def test_post_snapshot_stores_a_bare_date_as_midnight_utc(client, conn):
    acc = await create_account(conn, name="SnapDate", venue="manual", account_type="cash")
    await client.post("/api/snapshots", json=_body(acc))
    stored = await conn.fetchval(
        "SELECT as_of FROM account_snapshot WHERE account_id = $1", acc
    )
    assert stored == datetime(2026, 7, 31, tzinfo=UTC)


async def test_post_snapshot_reports_a_replacement(client, conn):
    acc = await create_account(conn, name="SnapReplace", venue="manual", account_type="cash")
    await client.post("/api/snapshots", json=_body(acc, cash_balance="1180.00"))
    r = await client.post("/api/snapshots", json=_body(acc, cash_balance="1204.11"))
    assert r.status_code == 201
    assert r.json()["replaced"] is True
    assert await conn.fetchval(
        "SELECT count(*) FROM account_snapshot WHERE account_id = $1", acc
    ) == 1
    assert await conn.fetchval(
        "SELECT cash_balance FROM account_snapshot WHERE account_id = $1", acc
    ) == Decimal("1204.11")


async def test_post_snapshot_accepts_negative_cash(client, conn):
    """account_snapshot carries NO check constraints and a margin debit is a
    legitimate negative cash balance. Refusing it would make the form unable
    to record a real statement."""
    acc = await create_account(conn, name="SnapMargin", venue="manual", account_type="margin")
    r = await client.post("/api/snapshots", json=_body(acc, cash_balance="-2500.00"))
    assert r.status_code == 201
    assert await conn.fetchval(
        "SELECT cash_balance FROM account_snapshot WHERE account_id = $1", acc
    ) == Decimal("-2500.00")


async def test_post_snapshot_refuses_a_nan_figure(client, conn):
    """Postgres NUMERIC ACCEPTS 'NaN', and account_snapshot has no CHECK to
    stop it -- parse_decimal's is_finite() is the only thing that does."""
    acc = await create_account(conn, name="SnapNaN", venue="manual", account_type="cash")
    r = await client.post("/api/snapshots", json=_body(acc, total_equity="NaN"))
    assert r.status_code == 422
    assert await conn.fetchval(
        "SELECT count(*) FROM account_snapshot WHERE account_id = $1", acc
    ) == 0


async def test_post_snapshot_refuses_a_future_date(client, conn):
    acc = await create_account(conn, name="SnapFuture", venue="manual", account_type="cash")
    r = await client.post("/api/snapshots", json=_body(acc, as_of="2099-01-01"))
    assert r.status_code == 422
    assert "future" in r.json()["detail"]


async def test_post_snapshot_404s_on_an_unknown_account(client):
    r = await client.post("/api/snapshots", json=_body(uuid4()))
    assert r.status_code == 404
