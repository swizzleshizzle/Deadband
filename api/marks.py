"""GET /api/marks and POST /api/marks (spec section 4).

Thin, like api/fills.py: db/marks.py holds every decision and cli.py's
`marks set` calls the same function this does (spec E6).

What this module adds over db/marks.py is the LIST the entry table is built
from -- which instruments are worth marking, deduped the way the `mark` table
is actually keyed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_conn, get_write_conn
from api.identity import require_trusted_identity
from api.serialization import DeadbandJSONResponse
from api.validation import parse_decimal, parse_instant, refuse_future
from db.marks import latest_marks, set_mark
from db.positions import open_positions

router = APIRouter()


@router.get("/api/marks")
async def marks(conn: asyncpg.Connection = Depends(get_conn)) -> DeadbandJSONResponse:
    """Every instrument the ledger holds that a mark would actually value.

    Deduped by instrument_id, NOT by (account_id, instrument_id) as
    open_positions returns them: `mark`'s primary key is
    (instrument_id, as_of), so one instrument is one markable thing however
    many accounts hold it. Rendering it once per account would offer two
    inputs writing to a single row, last one winning silently.

    Positions with an unvaluable_reason are omitted -- they are exactly the
    ones api/dashboard.py excludes from its latest_marks call, because they
    are not priced against a mark at all. Offering an action that changes
    nothing is worse than not offering it.
    """
    positions = [p for p in await open_positions(conn, None) if p.unvaluable_reason is None]

    # Accumulated in first-seen order so the response is stable across calls;
    # dict preserves insertion order and open_positions' own ordering is
    # deterministic.
    rolled: dict[UUID, dict] = {}
    for p in positions:
        entry = rolled.setdefault(
            p.instrument_id,
            {
                "instrument_id": p.instrument_id,
                "symbol": p.symbol,
                "natural_key": None,
                "quantity": Decimal(0),
                "accounts": [],
                "last_mark": None,
            },
        )
        entry["quantity"] += p.quantity
        entry["accounts"].append({"id": p.account_id, "name": p.account_name})

    if not rolled:
        return DeadbandJSONResponse({"marks": [], "generated_at": datetime.now(UTC)})

    # natural_key is NOT on OpenPosition, and it is what distinguishes two
    # instruments that legitimately share a symbol (the same ticker quoted in
    # two currencies). instrument.symbol is not unique; only natural_key is.
    # Without it those are two identical-looking rows and the user cannot
    # tell which one they are pricing.
    key_rows = await conn.fetch(
        "SELECT id, natural_key FROM instrument WHERE id = ANY($1::uuid[])",
        list(rolled),
    )
    for row in key_rows:
        rolled[row["id"]]["natural_key"] = row["natural_key"]

    # latest_marks returns (price, as_of) per instrument and omits -- never
    # zero-fills -- an instrument with no mark, because mark_price_chk
    # permits a genuine 0. `last_mark: null` and a 0.00 mark must stay
    # distinguishable in the payload for the same reason.
    for instrument_id, (price, as_of) in (await latest_marks(conn, list(rolled))).items():
        rolled[instrument_id]["last_mark"] = {"price": price, "as_of": as_of}

    return DeadbandJSONResponse(
        {"marks": list(rolled.values()), "generated_at": datetime.now(UTC)}
    )


class MarkIn(BaseModel):
    instrument_id: UUID
    price: str


class MarksIn(BaseModel):
    as_of: str
    marks: list[MarkIn]


@router.post("/api/marks", status_code=201)
async def create_marks(
    body: MarksIn,
    # Identity is declared BEFORE get_write_conn: FastAPI resolves
    # dependencies in declaration order, so an unauthenticated caller is
    # refused before the write pool is ever touched. See api/fills.py's
    # identical comment -- the reverse order let a 403-bound request check
    # out a write-pool connection on every attempt.
    _identity: str = Depends(require_trusted_identity),
    conn: asyncpg.Connection = Depends(get_write_conn),
) -> DeadbandJSONResponse:
    """Set one price per instrument, all at one as_of, in one transaction.

    No regroup_account, unlike POST /api/fills: a mark is a valuation input,
    not a ledger event, so no trade grouping changes. The transaction is here
    so a batch lands whole, nothing more.
    """
    if not body.marks:
        raise HTTPException(422, "marks: at least one mark is required")

    # The clock is taken ONCE, here in the I/O layer, so the future-date
    # guard measures against a single instant -- cmd_marks_set does the same
    # for the same reason.
    now = datetime.now(UTC)
    as_of = parse_instant(body.as_of, "as_of")
    refuse_future(as_of, now, "as_of")

    # Refused, not merged: two entries for one instrument at one as_of would
    # ON CONFLICT DO UPDATE each other inside the transaction below, and the
    # second would win with nothing reported. That is the same silent
    # last-one-wins the GET's dedupe exists to prevent, arriving by another
    # door.
    seen: set[UUID] = set()
    for i, m in enumerate(body.marks):
        if m.instrument_id in seen:
            raise HTTPException(
                422, f"marks[{i}].instrument_id: duplicate instrument in one submission"
            )
        seen.add(m.instrument_id)

    # Validate EVERY row before opening the transaction: a bad price on row 4
    # must not leave rows 1-3 written. The transaction makes that true anyway,
    # but failing early keeps the error clean -- api/fills.py's identical
    # comment.
    parsed: list[tuple[UUID, Decimal]] = []
    for i, m in enumerate(body.marks):
        price = parse_decimal(m.price, f"marks[{i}].price")
        # mark_price_chk is `price >= 0 AND price < 'Infinity'`. Zero is a
        # LEGAL mark -- an expired option is worth zero, and that is not the
        # same as having no mark at all. Negative is not, and reaching the
        # database with one produces an uncaught CheckViolationError, i.e. a
        # 500 for what is plainly a bad request.
        if price < 0:
            raise HTTPException(422, f"marks[{i}].price: {m.price!r} must not be negative")
        parsed.append((m.instrument_id, price))

    known = {
        r["id"]
        for r in await conn.fetch(
            "SELECT id FROM instrument WHERE id = ANY($1::uuid[])", list(seen)
        )
    }
    missing = seen - known
    if missing:
        raise HTTPException(404, f"instrument not found: {sorted(str(m) for m in missing)[0]}")

    async with conn.transaction():
        for instrument_id, price in parsed:
            await set_mark(conn, instrument_id, price, as_of)

    return DeadbandJSONResponse({"marks_set": len(parsed), "as_of": as_of}, status_code=201)
