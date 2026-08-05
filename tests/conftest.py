import os

import pytest
import pytest_asyncio

from db.migrate import apply
from db.pool import create_pool

TEST_DSN = os.environ.get("TEST_PG_DSN")

requires_db = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set — database tests are opt-in"
)


@pytest_asyncio.fixture
async def pool():
    if not TEST_DSN:
        pytest.skip("TEST_PG_DSN not set")
    p = await create_pool(TEST_DSN)
    async with p.acquire() as conn:
        await apply(conn)
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
