"""Connection plumbing for the API's two pools: read and write.

`get_conn` draws from a pool created lazily with
`default_transaction_read_only = on` (spec D3) -- for that pool specifically,
the read-only guarantee is a Postgres property of the connection, not a review
convention, and no handler using it can write regardless of what the code
above it does. `get_write_conn` draws from a second pool with no such
setting, used only by routes that need to write (tests/api/test_write_pool.py
checks mechanically that each route uses the pool its HTTP method implies).
Tests replace `app.state.pool` and `app.state.write_pool` with wrappers around
their rollback-per-test connection, so neither pool is actually created there
(see tests/api/conftest.py)."""

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
