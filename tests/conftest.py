import asyncio
import os
import secrets
from types import SimpleNamespace

import pytest
import pytest_asyncio

from db.migrate import apply
from db.pool import create_pool

TEST_DSN = os.environ.get("TEST_PG_DSN")

requires_db = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set — database tests are opt-in"
)


@pytest_asyncio.fixture
async def pool(migration_namespace):
    p = await create_pool(
        TEST_DSN, server_settings={"search_path": migration_namespace.schema}
    )
    yield p
    await p.close()


@pytest_asyncio.fixture
async def conn(pool):
    """A connection inside a transaction that is always rolled back, so tests
    never leave residue and can run in any order."""
    async with pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            yield c
        finally:
            await tx.rollback()


async def sweep_orphan_schemas(conn, keep: str) -> int:
    """Drop test_session_* schemas left behind by crashed runs, sparing `keep`.

    Deliberately drops EVERY other test_session_* schema: concurrent suite runs
    against one database are unsupported (they already raced on the shared
    public schema before issue #15's fix, and this box runs suites serially).
    """
    rows = await conn.fetch(
        "SELECT nspname FROM pg_namespace"
        " WHERE nspname LIKE 'test\\_session\\_%' AND nspname <> $1",
        keep,
    )
    for row in rows:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{row["nspname"]}" CASCADE')
    return len(rows)


@pytest.fixture(scope="session")
def migration_namespace():
    """The per-session disposable schema DB tests run in (issue #15).

    Migrations used to be applied to the shared public schema, where the first
    suite run after a branch added one recorded it PERMANENTLY -- every later
    apply() skipped it, and a test written to exercise that migration silently
    degraded to a no-op that kept passing (it hid a Critical on PR #14). Here
    apply() runs against a schema that did not exist a moment ago, so every
    migration executes on every suite run; `applied` carries the proof, and
    tests/db/test_session_namespace.py fails the suite if it is ever partial.

    Sync fixture with its own short-lived event loops on purpose: the pool
    fixture (and pytest-asyncio's default loop) is function-scoped, and a
    session-scoped asyncpg pool cannot be shared across per-test loops.
    """
    if not TEST_DSN:
        pytest.skip("TEST_PG_DSN not set")
    import asyncpg

    schema = f"test_session_{secrets.token_hex(4)}"

    async def _create() -> list[str]:
        conn = await asyncpg.connect(TEST_DSN)
        try:
            await sweep_orphan_schemas(conn, keep=schema)
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(f'SET search_path TO "{schema}"')
            return await apply(conn)
        finally:
            await conn.close()

    applied = asyncio.run(_create())
    yield SimpleNamespace(schema=schema, applied=applied)

    async def _drop() -> None:
        conn = await asyncpg.connect(TEST_DSN)
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await conn.close()

    asyncio.run(_drop())
