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


async def ensure_write_pool(app) -> asyncpg.Pool:
    """The write pool, created lazily and separately from the read pool.

    Deliberately NOT the same pool with the flag flipped per transaction:
    `default_transaction_read_only` is a server setting applied at connection
    time, so one pool cannot be both. Two pools make the read guarantee a
    property of which dependency a handler declares, which the test in
    tests/api/test_write_pool.py can then check mechanically.
    """
    if getattr(app.state, "write_pool", None) is None:
        app.state.write_pool = await create_pool()
    return app.state.write_pool


async def get_write_conn(request: Request) -> AsyncIterator[asyncpg.Connection]:
    pool = await ensure_write_pool(request.app)
    async with pool.acquire() as conn:
        yield conn
