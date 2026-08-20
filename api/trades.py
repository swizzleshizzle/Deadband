"""GET /api/trades and /api/trades/{id} (spec §5, §6)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Query

from api.deps import get_conn
from db.trades import query_trades

router = APIRouter()


@router.get("/api/trades")
async def trades(
    conn: asyncpg.Connection = Depends(get_conn),
    account: UUID | None = None,
    intent: Literal["trade", "investment", "unassigned"] | None = None,
    instrument: str | None = None,
    status: Literal["open", "closed"] | None = None,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    tag: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await query_trades(
        conn,
        account_id=account,
        intent=intent,
        underlying=instrument,
        status=status,
        opened_from=datetime.combine(from_, datetime.min.time(), tzinfo=UTC) if from_ else None,
        # Inclusive `to` date -> exclusive next-midnight bound.
        opened_before=(
            datetime.combine(to + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            if to
            else None
        ),
        tag=tag,
        limit=limit,
        offset=offset,
    )
    return {"trades": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}
