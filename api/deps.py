"""Connection plumbing for the read-only API.

Handlers draw connections through the app's pool, which is created lazily on
first use with `default_transaction_read_only = on` (spec D3): the read-only
milestone is a Postgres guarantee, not a review convention. Tests replace
`app.state.pool` with a wrapper around their rollback-per-test connection, so
the pool is never created there (see tests/api/conftest.py)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import asyncpg
from fastapi import Request

from db.pool import create_pool


async def ensure_pool(app) -> asyncpg.Pool:
    if getattr(app.state, "pool", None) is None:
        app.state.pool = await create_pool(
            server_settings={"default_transaction_read_only": "on"}
        )
    return app.state.pool


async def get_conn(request: Request) -> AsyncIterator[asyncpg.Connection]:
    pool = await ensure_pool(request.app)
    async with pool.acquire() as conn:
        yield conn
