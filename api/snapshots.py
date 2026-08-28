"""GET /api/accounts/{id}/snapshot and POST /api/snapshots (spec section 4).

Thin, like api/fills.py: db/snapshots.py holds every decision and cli.py's
`snapshot add` calls the same function this does (spec E6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps import get_conn, get_write_conn
from api.identity import require_trusted_identity
from api.serialization import DeadbandJSONResponse
from api.validation import parse_as_of, parse_decimal, refuse_future
from db.accounts import get_account
from db.snapshots import add_snapshot

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


class SnapshotIn(BaseModel):
    account_id: UUID
    as_of: str
    cash_balance: str
    total_equity: str
    note: str | None = None


@router.post("/api/snapshots", status_code=201)
async def create_snapshot(
    body: SnapshotIn,
    # Identity before get_write_conn -- see api/fills.py's create_fills for
    # why the ORDER matters, not just that both are present.
    _identity: str = Depends(require_trusted_identity),
    conn: asyncpg.Connection = Depends(get_write_conn),
) -> DeadbandJSONResponse:
    """Record what the broker reported for one account on one statement date.

    Re-posting the same (account, as_of) UPDATES the row -- correcting a
    mistyped figure is the point and the table has no history columns. The
    response says which happened, because a silent overwrite and a fresh
    insert must not look identical to whoever just clicked Save.
    """
    now = datetime.now(UTC)
    if await get_account(conn, body.account_id) is None:
        raise HTTPException(404, "account not found")

    as_of = parse_as_of(body.as_of, "as_of")
    refuse_future(as_of, now, "as_of")

    # No sign guard on either figure: account_snapshot carries no CHECK
    # constraints and a margin debit is a legitimate negative cash balance.
    # is_finite() inside parse_decimal is doing real work here though --
    # Postgres NUMERIC accepts 'NaN' and nothing downstream would refuse it.
    cash_balance = parse_decimal(body.cash_balance, "cash_balance")
    total_equity = parse_decimal(body.total_equity, "total_equity")

    async with conn.transaction():
        replaced = await conn.fetchval(
            "SELECT true FROM account_snapshot WHERE account_id = $1 AND as_of = $2",
            body.account_id,
            as_of,
        )
        # KEYWORD arguments, and add_snapshot's `*` is what forces it. These
        # two are adjacent parameters of the same type with no way to tell
        # them apart positionally: transposed, cash is stored as equity and
        # equity as cash, both are valid NUMERIC, nothing raises, and
        # reconcile reports the swap days later as unexplained drift on both
        # lines at once. Do not simplify this call.
        await add_snapshot(
            conn,
            body.account_id,
            as_of,
            cash_balance=cash_balance,
            total_equity=total_equity,
            note=body.note,
        )

    return DeadbandJSONResponse(
        {"account_id": body.account_id, "as_of": as_of, "replaced": bool(replaced)},
        status_code=201,
    )
