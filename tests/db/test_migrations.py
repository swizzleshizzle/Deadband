import asyncpg
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


async def test_deleting_opening_fill_orphans_trade_but_preserves_it(conn):
    """A mis-imported fill can be deleted without destroying the trade's
    user-authored fields — opening_fill_id ON DELETE SET NULL, never CASCADE."""
    acc = await conn.fetchval(
        "INSERT INTO account (name, venue, account_type) "
        "VALUES ('t', 'manual', 'cash') RETURNING id"
    )
    inst = await conn.fetchval(
        "INSERT INTO instrument (natural_key, asset_class, symbol) "
        "VALUES ('equity:T:USD', 'equity', 'T') RETURNING id"
    )
    fill_id = await conn.fetchval(
        "INSERT INTO fill (account_id, instrument_id, executed_at, side, "
        "quantity, price, source) VALUES ($1, $2, now(), 'buy', 10, 1, 'manual') "
        "RETURNING id",
        acc,
        inst,
    )
    trade_id = await conn.fetchval(
        "INSERT INTO trade (account_id, direction, status, opening_fill_id, "
        "opened_at, notes, planned_risk, strategy_tag) "
        "VALUES ($1, 'long', 'open', $2, now(), 'do not lose this', 100, 'breakout') "
        "RETURNING id",
        acc,
        fill_id,
    )

    await conn.execute("DELETE FROM fill WHERE id = $1", fill_id)

    row = await conn.fetchrow(
        "SELECT opening_fill_id, notes, planned_risk, strategy_tag FROM trade WHERE id = $1",
        trade_id,
    )
    assert row is not None
    assert row["opening_fill_id"] is None
    assert row["notes"] == "do not lose this"
    assert row["planned_risk"] == 100
    assert row["strategy_tag"] == "breakout"


async def test_trade_cannot_anchor_on_a_different_accounts_fill(conn):
    """opening_fill_id is a composite FK on (opening_fill_id, account_id): a trade
    in account B cannot name a fill belonging to account A."""
    acc_a = await conn.fetchval(
        "INSERT INTO account (name, venue, account_type) "
        "VALUES ('a', 'manual', 'cash') RETURNING id"
    )
    acc_b = await conn.fetchval(
        "INSERT INTO account (name, venue, account_type) "
        "VALUES ('b', 'manual', 'cash') RETURNING id"
    )
    inst = await conn.fetchval(
        "INSERT INTO instrument (natural_key, asset_class, symbol) "
        "VALUES ('equity:T2:USD', 'equity', 'T2') RETURNING id"
    )
    fill_a = await conn.fetchval(
        "INSERT INTO fill (account_id, instrument_id, executed_at, side, "
        "quantity, price, source) VALUES ($1, $2, now(), 'buy', 10, 1, 'manual') "
        "RETURNING id",
        acc_a,
        inst,
    )

    # Fails on the composite FK (trade_opening_fill_fk), not on some unrelated
    # NOT NULL/check violation — pin the exception class and the constraint name.
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError, match="trade_opening_fill_fk"):
        await conn.execute(
            "INSERT INTO trade (account_id, direction, status, opening_fill_id, opened_at) "
            "VALUES ($1, 'long', 'open', $2, now())",
            acc_b,
            fill_a,
        )


async def test_trade_fill_cannot_join_different_accounts(conn):
    """trade_fill.account_id ties both composite FKs to the same account, so a
    fill from one account can never be allocated to a trade in another."""
    acc_a = await conn.fetchval(
        "INSERT INTO account (name, venue, account_type) "
        "VALUES ('a', 'manual', 'cash') RETURNING id"
    )
    acc_b = await conn.fetchval(
        "INSERT INTO account (name, venue, account_type) "
        "VALUES ('b', 'manual', 'cash') RETURNING id"
    )
    inst = await conn.fetchval(
        "INSERT INTO instrument (natural_key, asset_class, symbol) "
        "VALUES ('equity:T3:USD', 'equity', 'T3') RETURNING id"
    )
    trade_a = await conn.fetchval(
        "INSERT INTO trade (account_id, direction, status, opened_at) "
        "VALUES ($1, 'long', 'open', now()) RETURNING id",
        acc_a,
    )
    fill_b = await conn.fetchval(
        "INSERT INTO fill (account_id, instrument_id, executed_at, side, "
        "quantity, price, source) VALUES ($1, $2, now(), 'buy', 10, 1, 'manual') "
        "RETURNING id",
        acc_b,
        inst,
    )

    # trade_a belongs to acc_a, fill_b belongs to acc_b. Tagging the allocation
    # with acc_a satisfies trade_fill_trade_fk but must fail trade_fill_fill_fk,
    # since (fill_b, acc_a) does not exist in fill's (id, account_id) pairs.
    with pytest.raises(asyncpg.exceptions.ForeignKeyViolationError, match="trade_fill_fill_fk"):
        await conn.execute(
            "INSERT INTO trade_fill (trade_id, fill_id, account_id, quantity) "
            "VALUES ($1, $2, $3, 1)",
            trade_a,
            fill_b,
            acc_a,
        )


async def test_deleting_account_cascades_fills_trades_and_allocations(conn):
    """Existing cascade behaviour still holds after the composite-FK rework:
    deleting an account removes its fills, trades and allocations, no orphans."""
    acc = await conn.fetchval(
        "INSERT INTO account (name, venue, account_type) "
        "VALUES ('t', 'manual', 'cash') RETURNING id"
    )
    inst = await conn.fetchval(
        "INSERT INTO instrument (natural_key, asset_class, symbol) "
        "VALUES ('equity:T4:USD', 'equity', 'T4') RETURNING id"
    )
    fill_1 = await conn.fetchval(
        "INSERT INTO fill (account_id, instrument_id, executed_at, side, "
        "quantity, price, source) VALUES ($1, $2, now(), 'buy', 10, 1, 'manual') "
        "RETURNING id",
        acc,
        inst,
    )
    fill_2 = await conn.fetchval(
        "INSERT INTO fill (account_id, instrument_id, executed_at, side, "
        "quantity, price, source) VALUES ($1, $2, now(), 'sell', 10, 1, 'manual') "
        "RETURNING id",
        acc,
        inst,
    )
    trade_id = await conn.fetchval(
        "INSERT INTO trade (account_id, direction, status, opening_fill_id, opened_at) "
        "VALUES ($1, 'long', 'closed', $2, now()) RETURNING id",
        acc,
        fill_1,
    )
    await conn.execute(
        "INSERT INTO trade_fill (trade_id, fill_id, account_id, quantity) VALUES ($1, $2, $3, 10)",
        trade_id,
        fill_1,
        acc,
    )
    await conn.execute(
        "INSERT INTO trade_fill (trade_id, fill_id, account_id, quantity) VALUES ($1, $2, $3, 10)",
        trade_id,
        fill_2,
        acc,
    )

    await conn.execute("DELETE FROM account WHERE id = $1", acc)

    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0
    assert await conn.fetchval("SELECT count(*) FROM trade WHERE account_id = $1", acc) == 0
    assert await conn.fetchval("SELECT count(*) FROM trade_fill WHERE account_id = $1", acc) == 0
