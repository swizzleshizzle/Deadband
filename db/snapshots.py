# db/snapshots.py
"""Broker-statement snapshots. The figures reconcile compares against."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import asyncpg


async def add_snapshot(
    conn: asyncpg.Connection,
    account_id: UUID,
    as_of: datetime,
    cash_balance: Decimal,
    total_equity: Decimal,
    source: str = "statement",
    note: str | None = None,
) -> None:
    """Record what the broker reported. Re-adding the same `as_of` UPDATES it --
    correcting a mistyped figure is the point, and the table has no history
    columns. Same reasoning as db/marks.py's set_mark."""
    if as_of.tzinfo is None:
        raise ValueError("snapshot as_of must be timezone-aware")
    await conn.execute(
        """
        INSERT INTO account_snapshot
            (account_id, as_of, cash_balance, total_equity, source, note)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (account_id, as_of) DO UPDATE SET
            cash_balance = EXCLUDED.cash_balance,
            total_equity = EXCLUDED.total_equity,
            source       = EXCLUDED.source,
            note         = EXCLUDED.note
        """,
        account_id, as_of, cash_balance, total_equity, source, note,
    )


async def latest_snapshot(
    conn: asyncpg.Connection, account_id: UUID, as_of: datetime | None = None
) -> asyncpg.Record | None:
    """The most recent snapshot on or before `as_of`, by DATE not by insertion.

    A correction entered today for last month's statement must not become "the
    latest" -- the same ordering hazard db/marks.py's latest_marks has. Returns
    None when the account has none: absent must stay distinguishable from a
    genuine zero-equity snapshot.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM account_snapshot
         WHERE account_id = $1 AND ($2::timestamptz IS NULL OR as_of <= $2)
         ORDER BY as_of DESC
         LIMIT 1
        """,
        account_id, as_of,
    )
