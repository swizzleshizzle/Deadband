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

async def open_positions(
    conn: asyncpg.Connection, account_id: UUID | None = None
) -> tuple[OpenPosition, ...]:
    records = await conn.fetch(_SQL, account_id)
    rows = [
        TradeRow(
            # A trade with no reachable instrument still has to appear -- but
            # two such trades are not necessarily "the same unknown thing".
            # A shared sentinel here would merge them into one row with a
            # summed quantity and a cost basis averaged across instruments
            # that have nothing to do with each other (an equity and an
            # option, or the same symbol in two different accounts): not
            # wrong-ish, meaningless, and worse because the row *looks*
            # populated. Falling back to the trade's OWN id instead gives
            # each orphaned trade its own grouping key, so none of them merge.
            #
            # This is a deliberate lie of type -- a trade id standing in
            # where an instrument id belongs -- but it never leaks past this
            # module: nothing here (or in ledger/positions.py) uses
            # `instrument_id` to look up an instrument or a mark directly,
            # and every row built this way also has open_quantity/
            # open_cost_basis NULL (the only path to a NULL joined instrument
            # is opening_fill_id IS NULL, which db/trades.py's protection
            # step always nulls those two columns alongside), so
            # `aggregate_positions` always sets `unvaluable_reason` on it. A
            # future caller (e.g. Task 3's mark lookup) that fetches marks
            # for these instrument_ids will simply find no matching mark row
            # -- trade ids and instrument ids are drawn from the same
            # gen_random_uuid() space but are never cross-inserted, so this
            # is a safe miss, not a silent collision.
            instrument_id=r["instrument_id"] or r["id"],
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
