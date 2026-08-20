"""GET /api/health (spec §3): liveness plus schema currency. Deliberately does
NOT use the get_conn dependency -- a dependency failure would 500 before the
handler ran, and this endpoint's contract is 200 with db=false when the
database is unreachable: health itself is reachable; what it REPORTS is the
problem."""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Request

from api.serialization import DeadbandJSONResponse
from db.migrate import MIGRATIONS

router = APIRouter()


async def _pending(conn: asyncpg.Connection) -> list[str]:
    done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
    return sorted(p.name for p in MIGRATIONS.glob("*.sql") if p.name not in done)


@router.get("/api/health")
async def health(request: Request) -> DeadbandJSONResponse:
    from api.deps import ensure_pool

    try:
        pool = await ensure_pool(request.app)
        async with pool.acquire() as conn:
            pending = await _pending(conn)
    except Exception:
        return DeadbandJSONResponse(
            {"db": False, "migrations_current": False, "pending_migrations": []}
        )
    return DeadbandJSONResponse(
        {"db": True, "migrations_current": not pending, "pending_migrations": pending}
    )
