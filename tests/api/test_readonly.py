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
                # DDL, not an INSERT into a named table: the refusal must not
                # depend on any table existing -- on a fresh CI database an
                # INSERT dies on UndefinedTableError before proving anything
                # about the read-only setting.
                await conn.execute("CREATE TABLE api_readonly_probe (id int)")
    finally:
        await pool.close()
