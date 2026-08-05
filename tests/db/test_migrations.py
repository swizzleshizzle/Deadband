import pytest

from db.migrate import apply
from tests.conftest import requires_db

pytestmark = requires_db


async def test_schema_creates_expected_tables(conn):
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    names = {r["tablename"] for r in rows}
    assert {
        "account",
        "instrument",
        "fill",
        "trade",
        "trade_fill",
        "cash_movement",
        "mark",
        "corporate_action",
        "account_snapshot",
        "funded_account_rule",
    } <= names


async def test_apply_is_idempotent(pool):
    async with pool.acquire() as c:
        assert await apply(c) == []  # already applied by the fixture


async def test_fill_rejects_non_positive_quantity(conn):
    acc = await conn.fetchval(
        "INSERT INTO account (name, venue, account_type) "
        "VALUES ('t', 'manual', 'cash') RETURNING id"
    )
    inst = await conn.fetchval(
        "INSERT INTO instrument (natural_key, asset_class, symbol) "
        "VALUES ('equity:T:USD', 'equity', 'T') RETURNING id"
    )
    with pytest.raises(Exception, match="quantity"):
        await conn.execute(
            "INSERT INTO fill (account_id, instrument_id, executed_at, side, "
            "quantity, price, source) VALUES ($1, $2, now(), 'buy', 0, 1, 'manual')",
            acc,
            inst,
        )
