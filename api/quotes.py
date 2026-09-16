"""GET /api/quotes -- proposed prices for the instruments the ledger holds.

Read-only, and structurally so: it draws from the read pool, whose Postgres
session carries `default_transaction_read_only = on`, so no code path here can
write a mark however this file changes. Writing stays with POST /api/marks and
its identity check. That separation is deliberate -- a fetched price is a
PROPOSAL the user reviews, not a fact about the portfolio, and a third-party
delayed quote moving P&L without anyone looking at it is the failure this
design exists to avoid.

The provider lives on app.state so tests inject a stub; nothing in the suite
may reach a network (spec section 9).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Request

from api.deps import get_conn
from api.markable import markable_positions
from api.serialization import DeadbandJSONResponse
from marketdata.base import QuoteSource
from marketdata.symbols import UnmappableInstrument, provider_symbol

router = APIRouter()

# A whole refresh, not per symbol. yfinance batches, and an unbounded wait
# would hold a request open for as long as a scraped endpoint feels like
# taking -- with the screen showing a spinner and no way to fall back to
# typing. On expiry every row is reported unquoted and stays hand-enterable.
_PROVIDER_TIMEOUT_SECONDS = 20.0


def get_quote_source(request: Request) -> QuoteSource:
    return request.app.state.quote_source


@router.get("/api/quotes")
async def quotes(
    conn: asyncpg.Connection = Depends(get_conn),
    source: QuoteSource = Depends(get_quote_source),
) -> DeadbandJSONResponse:
    """A proposed price per markable instrument, plus what could not be
    priced and why.

    `unquoted` is never silent. A row that vanishes from the response is
    indistinguishable from a position that stopped existing, and the two
    instruments this cannot price today -- a warrant with no provider listing
    and the merged blank-symbol row (known-gap #77) -- both still need
    marking by hand.
    """
    # Deduped by instrument_id, not by (account_id, instrument_id): `mark`'s
    # primary key is (instrument_id, as_of), so one instrument is one markable
    # thing however many accounts hold it. Identical rule to api/marks.py,
    # which is why both draw the set through api/markable.py.
    ordered: dict[UUID, str] = {}
    for p in await markable_positions(conn):
        ordered.setdefault(p.instrument_id, p.symbol)

    if not ordered:
        return DeadbandJSONResponse(
            {"quotes": [], "unquoted": [], "generated_at": datetime.now(UTC)}
        )

    rows = {
        r["id"]: r
        for r in await conn.fetch(
            """SELECT id, symbol, asset_class, underlying, expiry, strike, option_right
               FROM instrument WHERE id = ANY($1::uuid[])""",
            list(ordered),
        )
    }

    unquoted: list[dict] = []
    wanted: dict[str, UUID] = {}
    for instrument_id, symbol in ordered.items():
        r = rows[instrument_id]
        try:
            mapped = provider_symbol(
                asset_class=r["asset_class"],
                symbol=r["symbol"],
                underlying=r["underlying"],
                expiry=r["expiry"],
                strike=r["strike"],
                option_right=r["option_right"],
            )
        except UnmappableInstrument as e:
            unquoted.append(
                {"instrument_id": instrument_id, "symbol": symbol, "reason": str(e)}
            )
            continue
        # Last writer wins only if two instruments map to one provider symbol,
        # which would itself be a mapping bug; the fetched price is then
        # ambiguous, so both are reported rather than one silently taking the
        # other's quote.
        if mapped in wanted:
            unquoted.append({
                "instrument_id": instrument_id, "symbol": symbol,
                "reason": f"two instruments map to the same provider symbol ({mapped})",
            })
            continue
        wanted[mapped] = instrument_id

    fetched = {}
    if wanted:
        try:
            # to_thread, not a bare call: the provider is synchronous and does
            # network I/O, so calling it directly would block the event loop
            # and stall every other request for the duration.
            fetched = await asyncio.wait_for(
                asyncio.to_thread(source.quote, list(wanted)),
                timeout=_PROVIDER_TIMEOUT_SECONDS,
            )
        except Exception as e:
            # Deliberately broad, and it covers the wait_for timeout too
            # (TimeoutError is an Exception). yfinance scrapes an undocumented
            # endpoint and raises whatever that day's page shape produces; a
            # 500 here would take the marks TABLE down along with the quotes,
            # leaving no way to type prices in by hand. Every row is reported
            # unquoted with the reason instead, and the exception type is part
            # of it so a timeout reads differently from a parse failure.
            reason = f"provider {source.name} failed: {type(e).__name__}"
            for instrument_id in wanted.values():
                unquoted.append({
                    "instrument_id": instrument_id,
                    "symbol": ordered[instrument_id],
                    "reason": reason,
                })
            wanted = {}

    quoted: list[dict] = []
    for mapped, instrument_id in wanted.items():
        q = fetched.get(mapped)
        if q is None:
            unquoted.append({
                "instrument_id": instrument_id,
                "symbol": ordered[instrument_id],
                "reason": f"no quote returned for {mapped}",
            })
            continue
        quoted.append({
            "instrument_id": instrument_id,
            "symbol": ordered[instrument_id],
            "provider_symbol": mapped,
            "price": q.price,
            "currency": q.currency,
            "source": q.source,
        })

    return DeadbandJSONResponse(
        {"quotes": quoted, "unquoted": unquoted, "generated_at": datetime.now(UTC)}
    )
