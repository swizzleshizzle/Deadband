"""Open positions, read from the database and aggregated by the pure layer."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import asyncpg

from ledger.positions import OpenPosition, TradeRow, aggregate_positions
from ledger.types import Direction

# The fill/instrument LEFT JOINs below are LEFT, not INNER: a protected
# trade has opening_fill_id NULL (the composite FK is ON DELETE SET NULL),
# and an inner join would silently drop it from a listing whose whole job is
# to show everything the account holds.
_SQL = """
    SELECT t.id,
           t.direction,
           t.open_quantity,
           t.open_cost_basis,
           t.is_estimated,
           t.account_id AS account_id,
           a.name       AS account_name,
           i.id     AS instrument_id,
           i.symbol AS symbol,
           i.contract_multiplier AS multiplier
      FROM trade t
      -- Inner join, not left: trade.account_id is NOT NULL and account is
      -- never deleted out from under a trade (ON DELETE CASCADE runs the
      -- other way -- deleting an account deletes its trades, per
      -- db/schema.sql), so every open trade has exactly one reachable
      -- account. Unlike the instrument join below, there is no orphaned
      -- case here to protect against.
      JOIN account a         ON a.id = t.account_id
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
            account_id=r["account_id"],
            account_name=r["account_name"],
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
            # `instrument_id` to look up an instrument or a mark directly.
            # Trade ids and instrument ids are drawn from the same
            # gen_random_uuid() space but are never cross-inserted, so a
            # future caller (e.g. Task 3's mark lookup) that fetches marks
            # for these instrument_ids will simply find no matching mark row
            # -- a safe miss, not a silent collision.
            #
            # `is not None`, never truthiness, and the same test the two
            # quantity fields below use. A UUID is always truthy so `or`
            # happens to work here today, but the two spellings sitting side
            # by side invite someone to "simplify" the quantity lines to
            # `r["open_quantity"] or None` -- which would turn a genuine
            # Decimal(0) into "unknown". One spelling, everywhere, so that
            # edit never looks like a tidy-up.
            instrument_id=(
                r["instrument_id"] if r["instrument_id"] is not None else r["id"]
            ),
            # Keyed off the instrument's reachability, NOT off the symbol's
            # own truthiness: `instrument.symbol` is TEXT NOT NULL with no
            # non-empty check, and an empty-string symbol on a perfectly
            # reachable instrument would otherwise be labelled "(unknown
            # instrument)" while its quantity, basis and mark were still
            # priced normally -- a row that contradicts itself.
            symbol=(
                r["symbol"] if r["instrument_id"] is not None else "(unknown instrument)"
            ),
            multiplier=r["multiplier"] if r["multiplier"] is not None else Decimal(1),
            direction=Direction(r["direction"]),
            # When the instrument is unreachable, quantity/cost_basis are
            # forced to None HERE rather than forwarded from the row,
            # deliberately not relying on them already being NULL. Today
            # every db/trades.py path that nulls opening_fill_id also nulls
            # these two columns, but nothing enforces that pairing -- no
            # CHECK ties them together, and the FK's ON DELETE SET NULL only
            # touches opening_fill_id. The day a delete-a-fill action or an
            # import-undo runs without an immediate regroup_account, a trade
            # could carry a real, non-NULL open_quantity while its
            # instrument is unreachable, and forwarding it here would render
            # a wrong number as a real position under a trade-id standing in
            # for an instrument id. Forcing None instead makes the
            # "unreachable instrument" case unconditionally unvaluable: if we
            # cannot say which instrument a quantity belongs to, we cannot
            # report that quantity as a position in one.
            open_quantity=r["open_quantity"] if r["instrument_id"] is not None else None,
            open_cost_basis=r["open_cost_basis"] if r["instrument_id"] is not None else None,
            is_estimated=r["is_estimated"],
        )
        for r in records
    ]
    return aggregate_positions(rows)
