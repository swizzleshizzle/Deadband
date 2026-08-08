"""Open positions, read from the database and aggregated by the pure layer."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import asyncpg

from ledger.positions import OpenPosition, TradeRow, aggregate_positions
from ledger.types import Direction

# LEFT JOIN, not INNER: a protected trade has opening_fill_id NULL (the
# composite FK is ON DELETE SET NULL), and an inner join would silently drop
# it from a listing whose whole job is to show everything the account holds.
_SQL = """
    SELECT t.id,
           t.direction,
           t.open_quantity,
           t.open_cost_basis,
           t.is_estimated,
           i.id     AS instrument_id,
           i.symbol AS symbol,
           i.contract_multiplier AS multiplier
      FROM trade t
      LEFT JOIN fill f       ON f.id = t.opening_fill_id
      LEFT JOIN instrument i ON i.id = f.instrument_id
     WHERE t.status = 'open'
       AND ($1::uuid IS NULL OR t.account_id = $1)
"""

# A trade with no reachable instrument still has to appear. It is grouped
# under this sentinel so the aggregator's own "unknown" path renders it,
# rather than the query silently deciding it does not exist.
_UNKNOWN_INSTRUMENT = UUID("00000000-0000-0000-0000-000000000000")


async def open_positions(
    conn: asyncpg.Connection, account_id: UUID | None = None
) -> tuple[OpenPosition, ...]:
    records = await conn.fetch(_SQL, account_id)
    rows = [
        TradeRow(
            instrument_id=r["instrument_id"] or _UNKNOWN_INSTRUMENT,
            symbol=r["symbol"] or "(unknown instrument)",
            multiplier=r["multiplier"] if r["multiplier"] is not None else Decimal(1),
            direction=Direction(r["direction"]),
            open_quantity=r["open_quantity"],
            open_cost_basis=r["open_cost_basis"],
            is_estimated=r["is_estimated"],
        )
        for r in records
    ]
    return aggregate_positions(rows)
