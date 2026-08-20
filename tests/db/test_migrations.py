import pathlib
from datetime import date
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest

from db.accounts import create_account
from db.corporate import add_action
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.migrate import apply
from ledger.corporate import ActionType, CorporateAction
from ledger.types import AssetClass, Instrument, Side
from tests.conftest import requires_db
from tests.db.conftest import _fill

pytestmark = requires_db

# Used by test_migration_003_survives_a_populated_trade_fill to build an
# isolated pre-003 namespace -- the same baseline test_schema_equivalence.py
# uses for its eq_migrated side.
DB_DIR = pathlib.Path(__file__).resolve().parents[2] / "db"
BASELINE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "schema_baseline_a1.sql"
MIGRATION_003 = DB_DIR / "migrations" / "003_derived_fills.sql"


async def test_schema_creates_expected_tables(conn):
    rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
    names = {r["tablename"] for r in rows}
    assert {
        "account",
        "asset_transfer",
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
        assert await apply(c) == []  # migration_namespace already applied them


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


async def test_migration_003_survives_a_populated_trade_fill(pool):
    """The trade_fill PK rework is the only destructive step in 003, and the
    session namespace cannot exercise it against data: migration_namespace
    (issue #15's fix) re-runs every migration each session, but into a schema
    whose tables are seconds old and EMPTY -- and migrate.apply() skips any
    migration already recorded in schema_migrations, so calling apply(conn)
    again after seeding rows is a no-op: it neither re-runs 003 nor proves
    anything about surviving populated rows.

    Instead, build an isolated namespace at the PRE-003 state (baseline + 001
    + 002 -- the old composite PRIMARY KEY (trade_id, fill_id), fill_id NOT
    NULL shape), insert trade_fill rows directly with raw SQL (not via
    regroup_account, which only ever produces the post-003 shape on whatever
    schema is live), apply 003 alone against that populated namespace, and
    check the rows survived with the new PK and constraints in place. Drops
    the namespace afterwards, including on failure, the same technique the
    mutation gate's namespace-isolation test used."""
    ns = "mig3_pre003"
    async with pool.acquire() as conn:
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{ns}" CASCADE')
            await conn.execute(f'CREATE SCHEMA "{ns}"')
            await conn.execute(f'SET search_path TO "{ns}"')
            await conn.execute(BASELINE.read_text())
            await conn.execute((DB_DIR / "migrations" / "001_a2_ledger_completion.sql").read_text())
            await conn.execute(
                (DB_DIR / "migrations" / "002_reject_non_finite_numerics.sql").read_text()
            )

            acc = await conn.fetchval(
                "INSERT INTO account (name, venue, account_type) "
                "VALUES ('Mig3', 'manual', 'cash') RETURNING id"
            )
            inst = await conn.fetchval(
                "INSERT INTO instrument (natural_key, asset_class, symbol) "
                "VALUES ('equity:ZXCO:USD', 'equity', 'ZXCO') RETURNING id"
            )
            fill_id = await conn.fetchval(
                "INSERT INTO fill (account_id, instrument_id, executed_at, side, "
                "quantity, price, source) VALUES ($1, $2, now(), 'buy', 10, 1, 'manual') "
                "RETURNING id",
                acc,
                inst,
            )
            trade_id = await conn.fetchval(
                "INSERT INTO trade (account_id, direction, status, opening_fill_id, opened_at) "
                "VALUES ($1, 'long', 'open', $2, now()) RETURNING id",
                acc,
                fill_id,
            )
            # Old shape: no `id` column, composite PRIMARY KEY (trade_id, fill_id).
            await conn.execute(
                "INSERT INTO trade_fill (trade_id, fill_id, account_id, quantity) "
                "VALUES ($1, $2, $3, 10)",
                trade_id,
                fill_id,
                acc,
            )

            before = await conn.fetchval(
                "SELECT count(*) FROM trade_fill WHERE account_id = $1", acc
            )
            assert before > 0

            # The one destructive step, applied against these already-populated rows.
            await conn.execute(MIGRATION_003.read_text())

            after = await conn.fetch(
                "SELECT id, fill_id, derived_fill_id FROM trade_fill WHERE account_id = $1", acc
            )
            assert len(after) == before
            assert all(r["id"] is not None for r in after)
            assert all(r["fill_id"] is not None and r["derived_fill_id"] is None for r in after)

            pk = await conn.fetchrow(
                "SELECT conname, pg_get_constraintdef(oid) AS def FROM pg_constraint "
                "WHERE conrelid = 'trade_fill'::regclass AND contype = 'p'"
            )
            assert pk is not None
            assert pk["conname"] == "trade_fill_pkey"
            assert pk["def"] == "PRIMARY KEY (id)"

            one_source_chk = await conn.fetchval(
                "SELECT 1 FROM pg_constraint WHERE conrelid = 'trade_fill'::regclass "
                "AND conname = 'trade_fill_one_source_chk'"
            )
            assert one_source_chk == 1
        finally:
            await conn.execute("RESET search_path")
            await conn.execute(f'DROP SCHEMA IF EXISTS "{ns}" CASCADE')


async def test_derived_fill_rejects_a_cross_account_trade_reference(conn):
    """derived_fill carries UNIQUE (id, account_id) so composite FKs get the
    same cross-account guard fill_id_account_uniq gives. Without it a trade in
    account B could anchor on a derived fill from account A.

    Rewritten from an introspection-only check (does some unique constraint
    exist on derived_fill?) to a behavioural one: insert a derived_fill row
    under account A directly (no code writes them until Task 3), then try to
    anchor a trade in account B on it, and assert the composite FK
    (trade_opening_derived_fill_fk, which needs derived_fill_id_account_uniq
    to exist at all) rejects it."""
    acc_a = await create_account(conn, name="DerivA", venue="manual", account_type="cash")
    acc_b = await create_account(conn, name="DerivB", venue="manual", account_type="cash")
    parent_inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    child_inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCB", quote_currency="USD"),
    )
    parent_fill = _fill(
        acc_a, parent_inst, side=Side.BUY, quantity="10", price="1.00", ref="deriv-parent"
    )
    await insert_fills(conn, [parent_fill])

    action_id = await add_action(
        conn,
        CorporateAction(
            instrument_id=parent_inst,
            action_type=ActionType.SPINOFF,
            ex_date=date(2026, 3, 2),
            ratio_numerator=Decimal("1"),
            ratio_denominator=Decimal("1"),
            resulting_instrument_id=child_inst,
            basis_allocation=Decimal("0.1"),
        ),
    )

    derived_id = uuid4()
    await conn.execute(
        """
        INSERT INTO derived_fill
            (id, account_id, instrument_id, executed_at, side, quantity, price,
             derived_from_fill_id, corporate_action_id)
        VALUES ($1, $2, $3, now(), 'buy', 1, 1, $4, $5)
        """,
        derived_id,
        acc_a,
        child_inst,
        parent_fill.id,
        action_id,
    )

    with pytest.raises(
        asyncpg.exceptions.ForeignKeyViolationError, match="trade_opening_derived_fill_fk"
    ):
        await conn.execute(
            "INSERT INTO trade (account_id, direction, status, opening_derived_fill_id, opened_at) "
            "VALUES ($1, 'long', 'open', $2, now())",
            acc_b,
            derived_id,
        )


async def test_migration_004_survives_populated_rows_and_widens_the_kind_check(pool):
    """Build a pre-004 namespace (baseline + 001..003), seed cash_movement rows
    under the OLD kind constraint, run 004 alone, and prove: existing rows
    survive, 'transfer_out' becomes insertable, a bogus kind still raises, and
    trade grew qty_transferred. Guards the named-constraint conversion: 004
    must drop the auto-named inline CHECK and install cash_movement_kind_chk."""
    ns = "mig004_populated"
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{ns}" CASCADE')
            await conn.execute(f'CREATE SCHEMA "{ns}"')
            await conn.execute(f'SET search_path TO "{ns}"')
            await conn.execute(BASELINE.read_text())
            for m in (
                "001_a2_ledger_completion.sql",
                "002_reject_non_finite_numerics.sql",
                "003_derived_fills.sql",
            ):
                await conn.execute((DB_DIR / "migrations" / m).read_text())

            acc = await conn.fetchval(
                "INSERT INTO account (name, venue, account_type)"
                " VALUES ('t', 'manual', 'cash') RETURNING id"
            )
            await conn.execute(
                "INSERT INTO cash_movement (account_id, occurred_at, kind, amount)"
                " VALUES ($1, now(), 'deposit', 10), ($1, now(), 'withdrawal', 5)",
                acc,
            )

            await conn.execute((DB_DIR / "migrations" / "004_asset_transfers.sql").read_text())

            assert await conn.fetchval("SELECT count(*) FROM cash_movement") == 2
            await conn.execute(
                "INSERT INTO cash_movement (account_id, occurred_at, kind, amount)"
                " VALUES ($1, now(), 'transfer_out', 1)",
                acc,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO cash_movement (account_id, occurred_at, kind, amount)"
                        " VALUES ($1, now(), 'bogus', 1)",
                        acc,
                    )
            assert (
                await conn.fetchval(
                    "SELECT count(*) FROM information_schema.columns"
                    " WHERE table_schema = $1 AND table_name = 'trade'"
                    " AND column_name = 'qty_transferred'",
                    ns,
                )
                == 1
            )
            inst = await conn.fetchval(
                "INSERT INTO instrument (natural_key, asset_class, symbol)"
                " VALUES ('equity:TSTX', 'equity', 'TSTX') RETURNING id"
            )
            await conn.execute(
                "INSERT INTO asset_transfer (account_id, instrument_id, occurred_at,"
                " direction, quantity, market_value) VALUES ($1,$2,now(),'out',40,259.2)",
                acc,
                inst,
            )
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO asset_transfer (account_id, instrument_id, occurred_at,"
                        " direction, quantity) VALUES ($1,$2,now(),'in',40)",
                        acc,
                        inst,
                    )
        finally:
            await tx.rollback()
