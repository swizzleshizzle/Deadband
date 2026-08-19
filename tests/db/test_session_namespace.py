"""The pool fixture must run against a per-session disposable namespace, so every
migration executes on every suite run and a migration test can never silently
degrade to a no-op (issue #15 — this mechanism hid a Critical on PR #14)."""

import time

from db.migrate import MIGRATIONS
from tests.conftest import (
    SESSION_SCHEMA_PREFIX,
    requires_db,
    sweep_orphan_schemas,
)

pytestmark = requires_db


async def test_pool_runs_in_a_disposable_namespace(conn):
    schema = await conn.fetchval("SELECT current_schema()")
    assert schema.startswith("test_session_"), schema


async def test_every_migration_executed_this_session(migration_namespace):
    expected = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert migration_namespace.applied == expected


async def test_sweep_drops_stale_schemas_and_spares_live_ones(conn, migration_namespace):
    """The sweep must drop schemas from crashed runs (stale timestamp, or a name
    it cannot parse an age from) while sparing both the schema it was told to
    keep and any schema young enough to belong to a CONCURRENT suite run in
    another worktree — dropping a live parallel run's schema mid-suite would
    fail its every remaining test. Runs on `conn` so the DDL rolls back."""
    now = int(time.time())
    stale = f"{SESSION_SCHEMA_PREFIX}1_aaaaaa"
    malformed = f"{SESSION_SCHEMA_PREFIX}orphan"
    recent = f"{SESSION_SCHEMA_PREFIX}{now}_bbbbbb"
    for ns in (stale, malformed, recent):
        await conn.execute(f'CREATE SCHEMA "{ns}"')

    await sweep_orphan_schemas(conn, keep=migration_namespace.schema)

    rows = await conn.fetch(
        "SELECT nspname FROM pg_namespace WHERE nspname = ANY($1::name[])",
        [stale, malformed, recent, migration_namespace.schema],
    )
    surviving = {r["nspname"] for r in rows}
    assert surviving == {recent, migration_namespace.schema}
