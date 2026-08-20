"""asset_transfer storage: the share leg of outbound ACATs (branch B)."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from db.fills import InsertResult
from ledger.types import AssetTransfer


async def insert_transfers(
    conn: asyncpg.Connection, transfers: list[AssetTransfer]
) -> InsertResult:
    """Insert transfers idempotently. Duplicates by content_hash are skipped,
    same contract as insert_fills -- re-importing overlapping exports is safe."""
    inserted = 0
    for t in transfers:
        row = await conn.fetchval(
            """
            INSERT INTO asset_transfer (
                id, account_id, instrument_id, occurred_at, direction,
                quantity, market_value, venue_ref, content_hash, note
            ) VALUES ($1,$2,$3,$4,'out',$5,$6,$7,$8,$9)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            t.id,
            t.account_id,
            t.instrument_id,
            t.occurred_at,
            t.quantity,
            t.market_value,
            t.venue_ref,
            t.content_hash,
            t.note,
        )
        if row is not None:
            inserted += 1
    return InsertResult(inserted=inserted, skipped=len(transfers) - inserted)


def _to_transfer(r: asyncpg.Record) -> AssetTransfer:
    return AssetTransfer(
        id=r["id"],
        account_id=r["account_id"],
        instrument_id=r["instrument_id"],
        occurred_at=r["occurred_at"],
        quantity=r["quantity"],
        market_value=r["market_value"],
        venue_ref=r["venue_ref"],
        content_hash=r["content_hash"],
        note=r["note"],
    )


async def fetch_transfers(
    conn: asyncpg.Connection, account_id: UUID | None = None
) -> list[AssetTransfer]:
    if account_id:
        rows = await conn.fetch(
            "SELECT * FROM asset_transfer WHERE account_id = $1 ORDER BY occurred_at, id",
            account_id,
        )
    else:
        rows = await conn.fetch("SELECT * FROM asset_transfer ORDER BY occurred_at, id")
    return [_to_transfer(r) for r in rows]
