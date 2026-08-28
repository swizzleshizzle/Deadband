"""GET /api/accounts/{id}/snapshot and POST /api/snapshots (spec section 4).

Thin, like api/fills.py: db/snapshots.py holds every decision and cli.py's
`snapshot add` calls the same function this does (spec E6).
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_conn
from api.serialization import DeadbandJSONResponse
from api.validation import parse_as_of
from db.accounts import get_account

# NOTE: only what the GET below uses. Task 5 adds BaseModel, get_write_conn,
# require_trusted_identity, add_snapshot, parse_decimal and refuse_future when
# it adds the POST that needs them.

router = APIRouter()


@router.get("/api/accounts/{account_id}/snapshot")
async def snapshot_for_date(
    account_id: UUID,
    as_of: str = Query(...),
    conn: asyncpg.Connection = Depends(get_conn),
) -> DeadbandJSONResponse:
    """The snapshot stored for EXACTLY this account and date, or null.

    Deliberately not db.snapshots.latest_snapshot, which returns the most
    recent snapshot ON OR BEFORE its as_of. That is the right function for
    reconciling and the wrong one here: this route answers "will saving
    replace an existing row?", and add_snapshot's
    ON CONFLICT (account_id, as_of) fires only on an exact match. A fallback
    would warn about replacing July's statement while entering August's,
    which replaces nothing.
    """
    if await get_account(conn, account_id) is None:
        raise HTTPException(404, "account not found")
    parsed = parse_as_of(as_of, "as_of")
    row = await conn.fetchrow(
        """
        SELECT as_of, cash_balance, total_equity, note
          FROM account_snapshot
         WHERE account_id = $1 AND as_of = $2
        """,
        account_id,
        parsed,
    )
    return DeadbandJSONResponse({"snapshot": dict(row) if row is not None else None})
