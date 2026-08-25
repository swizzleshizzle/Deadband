"""POST /api/fills and DELETE /api/fills/{id} (spec section 3).

Thin: every decision lives in db/fills.py and cli.py's commands call the same
functions. Money and quantities arrive as STRINGS and are parsed straight to
Decimal -- never through float (spec section 5).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from api.deps import get_write_conn
from api.serialization import DeadbandJSONResponse
from db.accounts import get_account
from db.fills import add_manual_fills, delete_manual_fill
from db.instruments import upsert_instrument
from db.trades import regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side

router = APIRouter()


class LegIn(BaseModel):
    symbol: str
    side: str
    quantity: str
    price: str
    fee: str = "0"
    fee_currency: str = "USD"
    executed_at: str


class FillsIn(BaseModel):
    account_id: UUID
    fills: list[LegIn]


def _decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise HTTPException(422, f"{field}: {raw!r} is not a valid number") from None
    if not value.is_finite():
        raise HTTPException(422, f"{field}: {raw!r} must be finite")
    return value


@router.post("/api/fills", status_code=201)
async def create_fills(
    body: FillsIn, conn: asyncpg.Connection = Depends(get_write_conn)
) -> DeadbandJSONResponse:
    if not body.fills:
        raise HTTPException(422, "fills: at least one leg is required")
    if await get_account(conn, body.account_id) is None:
        raise HTTPException(404, "account not found")

    # Validate every leg BEFORE opening the transaction: a blank symbol on
    # leg 4 must not leave legs 1-3 written. The transaction below makes that
    # true even so, but failing early keeps the error clean.
    parsed = []
    for i, leg in enumerate(body.fills):
        symbol = leg.symbol.strip()
        if not symbol:
            raise HTTPException(422, f"fills[{i}].symbol: must not be blank")
        quantity = _decimal(leg.quantity, f"fills[{i}].quantity")
        if quantity <= 0:
            raise HTTPException(422, f"fills[{i}].quantity: must be positive")
        try:
            side = Side(leg.side)
        except ValueError:
            raise HTTPException(422, f"fills[{i}].side: {leg.side!r} is not buy/sell") from None
        parsed.append(
            (
                symbol.upper(),
                side,
                quantity,
                _decimal(leg.price, f"fills[{i}].price"),
                _decimal(leg.fee, f"fills[{i}].fee"),
                leg.fee_currency,
                datetime.fromisoformat(leg.executed_at),
            )
        )

    async with conn.transaction():
        fills = []
        for symbol, side, quantity, price, fee, currency, executed_at in parsed:
            instrument_id = await upsert_instrument(
                conn,
                Instrument(
                    id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency=currency
                ),
            )
            fills.append(
                Fill(
                    id=uuid4(), account_id=body.account_id, instrument_id=instrument_id,
                    executed_at=executed_at, side=side, quantity=quantity, price=price,
                    fee=fee, fee_currency=currency, source=FillSource.MANUAL,
                    venue_fill_id=None, is_estimated=False,
                )
            )
        fill_ids = await add_manual_fills(conn, fills)
        regrouped = await regroup_account(conn, body.account_id)

    return DeadbandJSONResponse(
        {"fill_ids": fill_ids, "trades_regrouped": regrouped}, status_code=201
    )


@router.delete("/api/fills/{fill_id}", status_code=204)
async def remove_fill(fill_id: UUID, conn: asyncpg.Connection = Depends(get_write_conn)):
    account_id = await conn.fetchval("SELECT account_id FROM fill WHERE id = $1", fill_id)
    if account_id is None:
        raise HTTPException(404, "fill not found")
    async with conn.transaction():
        if not await delete_manual_fill(conn, fill_id):
            raise HTTPException(409, "imported fills are immutable")
        await regroup_account(conn, account_id)
    return Response(status_code=204)
