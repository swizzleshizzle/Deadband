import asyncio
import os
import secrets
import time
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio

from db.migrate import apply
from db.pool import create_pool

TEST_DSN = os.environ.get("TEST_PG_DSN")

# Single source for the disposable-schema prefix: the name generator, the
# sweep's LIKE pattern, and the tests all derive from this one constant, so a
# rename cannot silently strand the sweep on the old spelling.
SESSION_SCHEMA_PREFIX = "test_session_"

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


# A schema younger than this is presumed to belong to a LIVE suite run in
# another worktree sharing TEST_PG_DSN and is spared; suites finish in minutes,
# so two hours is a generous margin before a crashed run's leftover is swept.
STALE_SESSION_SECONDS = 2 * 60 * 60


async def sweep_orphan_schemas(conn, keep: str) -> int:
    """Drop disposable schemas left behind by crashed runs.

    Spared: `keep`, and any schema whose embedded creation timestamp is recent
    enough to belong to a concurrent suite run in another worktree — dropping a
    live run's schema mid-suite would fail its every remaining test. A name the
    prefix matches but no timestamp parses from is an orphan from an older
    naming scheme: dropped.
    """
    pattern = SESSION_SCHEMA_PREFIX.replace("_", "\\_") + "%"
    rows = await conn.fetch(
        "SELECT nspname FROM pg_namespace WHERE nspname LIKE $1 AND nspname <> $2",
        pattern,
        keep,
    )
    cutoff = time.time() - STALE_SESSION_SECONDS
    dropped = 0
    for row in rows:
        stamp = row["nspname"][len(SESSION_SCHEMA_PREFIX) :].split("_", 1)[0]
        if stamp.isdigit() and int(stamp) > cutoff:
            continue
        await conn.execute(f'DROP SCHEMA IF EXISTS "{row["nspname"]}" CASCADE')
        dropped += 1
    return dropped


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

    schema = f"{SESSION_SCHEMA_PREFIX}{int(time.time())}_{secrets.token_hex(3)}"

    async def _admin(fn):
        # Through db.pool.create_pool, never a raw connect: db/pool.py is "the
        # only place that opens database connections", and the schema must be
        # built under the same connection settings it is later queried under —
        # a codec or timeout added there must reach this connection too. The
        # search_path may name a schema that does not exist yet; Postgres
        # accepts that, and CREATE SCHEMA below is name-qualified by itself.
        pool = await create_pool(TEST_DSN, server_settings={"search_path": schema})
        try:
            async with pool.acquire() as admin_conn:
                # A crashed run can leave a backend holding locks in an orphan
                # schema; without a lock_timeout the sweep's DROP would wait on
                # it forever with no diagnostic pointing here.
                await admin_conn.execute("SET lock_timeout = '10s'")
                return await fn(admin_conn)
        finally:
            await pool.close()

    async def _create(admin_conn) -> list[str]:
        await sweep_orphan_schemas(admin_conn, keep=schema)
        try:
            await admin_conn.execute(f'CREATE SCHEMA "{schema}"')
        except asyncpg.InsufficientPrivilegeError as exc:
            # Deliberately loud, never pytest.skip: this suite's history is
            # exactly silent green runs hiding unrun DB tests. Name the grant.
            raise RuntimeError(
                "the TEST_PG_DSN role cannot create schemas in this database; "
                "the suite builds a disposable per-session schema (issue #15). "
                "Fix: GRANT CREATE ON DATABASE <db> TO <role>."
            ) from exc
        return await apply(admin_conn)

    applied = asyncio.run(_admin(_create))
    yield SimpleNamespace(schema=schema, applied=applied)

    async def _drop(admin_conn) -> None:
        await admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')

    asyncio.run(_admin(_drop))
