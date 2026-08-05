"""asyncpg pool lifecycle. The only place that opens database connections."""

from __future__ import annotations

import os

import asyncpg


async def create_pool(dsn: str | None = None, **kwargs) -> asyncpg.Pool:
    resolved = dsn or os.environ.get("PG_DSN")
    if not resolved:
        raise RuntimeError("PG_DSN is not set and no dsn was provided")
    return await asyncpg.create_pool(resolved, min_size=1, max_size=5, **kwargs)
