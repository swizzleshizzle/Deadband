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
from fastapi import APIRouter, Depends

from api.deps import get_conn
from api.serialization import DeadbandJSONResponse
from db.marks import latest_marks
from db.positions import open_positions

# NOTE: this block imports only what the GET below uses. Task 3 adds the POST
# and the imports it needs (BaseModel, get_write_conn, require_trusted_identity,
# set_mark, the validation helpers) at that point — not here, where they would
# sit unused.

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
