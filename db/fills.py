"""Fill persistence. Idempotent import is the whole point: re-running an
importer over overlapping exports must not create duplicate fills."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from ledger.types import Fill, FillSource, Side


@dataclass(frozen=True, slots=True)
class InsertResult:
    inserted: int
    skipped: int


async def insert_fills(conn: asyncpg.Connection, fills: list[Fill]) -> InsertResult:
    """Insert fills idempotently. Duplicates by venue_fill_id or content_hash are
    skipped, which is what makes re-importing overlapping exports safe."""
    inserted = 0
    for f in fills:
        row = await conn.fetchval(
            """
            INSERT INTO fill (
                id, account_id, instrument_id, executed_at, side, quantity, price,
                fee, fee_currency, source, venue_order_id, venue_fill_id,
                content_hash, is_estimated, funding_source
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            f.id,
            f.account_id,
            f.instrument_id,
            f.executed_at,
            f.side.value,
            f.quantity,
            f.price,
            f.fee,
            f.fee_currency,
            f.source.value,
            f.venue_order_id,
            f.venue_fill_id,
            f.content_hash,
            f.is_estimated,
            f.funding_source,
        )
        if row is not None:
            inserted += 1
    return InsertResult(inserted=inserted, skipped=len(fills) - inserted)


async def add_manual_fills(conn: asyncpg.Connection, fills: list[Fill]) -> list[UUID]:
    """Insert hand-entered fills and return their ids, in order.

    Manual fills deliberately carry NEITHER dedupe key. venue_fill_id is None
    because no venue issued them, and content_hash is None because the import
    hash -- (executed_at, symbol, side, quantity, price) plus a within-batch
    occurrence index -- cannot distinguish two genuinely identical manual
    entries made in separate submissions. Hashing them would silently drop the
    second, which is worse than a visible duplicate the user can delete: both
    partial unique indexes skip NULLs, so every fill here lands.
    """
    for f in fills:
        if f.source is not FillSource.MANUAL:
            raise ValueError(f"add_manual_fills got a {f.source.value} fill; expected manual")
        if f.content_hash is not None or f.venue_fill_id is not None:
            raise ValueError("manual fills must carry neither venue_fill_id nor content_hash")
    await insert_fills(conn, fills)
    return [f.id for f in fills]


async def delete_manual_fill(conn: asyncpg.Connection, fill_id: UUID) -> bool:
    """Delete a hand-entered fill. Returns False if no such fill exists OR it
    was imported -- the source check lives in the WHERE clause so it cannot be
    bypassed by a caller that forgets to make it."""
    result = await conn.execute(
        "DELETE FROM fill WHERE id = $1 AND source = 'manual'", fill_id
    )
    return result != "DELETE 0"


def _to_fill(r: asyncpg.Record) -> Fill:
    return Fill(
        id=r["id"],
        account_id=r["account_id"],
        instrument_id=r["instrument_id"],
        executed_at=r["executed_at"],
        side=Side(r["side"]),
        quantity=r["quantity"],
        price=r["price"],
        fee=r["fee"],
        fee_currency=r["fee_currency"],
        source=FillSource(r["source"]),
        venue_order_id=r["venue_order_id"],
        venue_fill_id=r["venue_fill_id"],
        content_hash=r["content_hash"],
        is_estimated=r["is_estimated"],
        funding_source=r["funding_source"],
    )


async def fetch_fills(conn: asyncpg.Connection, account_id: UUID | None = None) -> list[Fill]:
    if account_id:
        rows = await conn.fetch(
            "SELECT * FROM fill WHERE account_id = $1 ORDER BY executed_at, id",
            account_id,
        )
    else:
        rows = await conn.fetch("SELECT * FROM fill ORDER BY executed_at, id")
    return [_to_fill(r) for r in rows]
