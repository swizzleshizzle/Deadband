"""Spec D3: the API's own pool is read-only at the Postgres session level --
a write endpoint added by accident fails loudly, regardless of review."""

import asyncpg
import pytest

from api.app import create_app
from api.deps import ensure_pool
from tests.conftest import TEST_DSN, requires_db

pytestmark = requires_db


async def test_api_pool_refuses_writes(monkeypatch):
    monkeypatch.setenv("PG_DSN", TEST_DSN)
    app = create_app()
    pool = await ensure_pool(app)
    try:
        async with pool.acquire() as conn:
            with pytest.raises(asyncpg.ReadOnlySQLTransactionError):
                await conn.execute(
                    "INSERT INTO schema_migrations (name) VALUES ('never_lands.sql')"
                )
    finally:
        await pool.close()
