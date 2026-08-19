"""The pool fixture must run against a per-session disposable namespace, so every
migration executes on every suite run and a migration test can never silently
degrade to a no-op (issue #15 — this mechanism hid a Critical on PR #14)."""

from db.migrate import MIGRATIONS
from tests.conftest import requires_db, sweep_orphan_schemas

pytestmark = requires_db


async def test_pool_runs_in_a_disposable_namespace(conn):
    schema = await conn.fetchval("SELECT current_schema()")
    assert schema.startswith("test_session_"), schema


async def test_every_migration_executed_this_session(migration_namespace):
    expected = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert migration_namespace.applied == expected


async def test_sweep_drops_orphans_and_keeps_the_live_namespace(pool, migration_namespace):
    orphan = "test_session_orphan"
    async with pool.acquire() as c:
        await c.execute(f'CREATE SCHEMA IF NOT EXISTS "{orphan}"')
        try:
            await sweep_orphan_schemas(c, keep=migration_namespace.schema)
            remaining = await c.fetchval(
                "SELECT count(*) FROM pg_namespace WHERE nspname = $1", orphan
            )
            kept = await c.fetchval(
                "SELECT count(*) FROM pg_namespace WHERE nspname = $1",
                migration_namespace.schema,
            )
        finally:
            await c.execute(f'DROP SCHEMA IF EXISTS "{orphan}" CASCADE')
    assert remaining == 0
    assert kept == 1
