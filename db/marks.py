"""Price marks. The only MarkSource is manual entry -- A does not fetch prices."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import asyncpg

# How far ahead of "now" a mark's as_of may sit before it is refused.
#
# latest_marks (below) treats the newest as_of as "the current price", with
# nothing else checking plausibility -- a fat-fingered year or a bad backfill
# would otherwise silently become today's price and produce a wrong
# unrealized figure with no signal at all. The tolerance absorbs clock skew
# between this box and the database, and the fact that "now" isn't identically
# defined on two machines, without opening the door to a meaningfully wrong
# future date. Two minutes comfortably covers ordinary clock drift for a
# command that is typed by hand, not fired in a tight loop.
#
# Lives here rather than in cli.py because api/marks.py needs the same value
# and two copies of a policy constant drifting apart is precisely the failure
# cli.py's _parse_as_of docstring records for its own duplicated parser.
# A timedelta is a duration, not a clock -- this file stays clock-free.
MARK_FUTURE_TOLERANCE = timedelta(minutes=2)


async def set_mark(
    conn: asyncpg.Connection,
    instrument_id: UUID,
    price: Decimal,
    as_of: datetime,
    source: str = "manual",
) -> None:
    if as_of.tzinfo is None:
        raise ValueError("mark as_of must be timezone-aware")
    await conn.execute(
        """
        INSERT INTO mark (instrument_id, as_of, price, source)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (instrument_id, as_of)
        DO UPDATE SET price = EXCLUDED.price, source = EXCLUDED.source
        """,
        instrument_id,
        as_of,
        price,
        source,
    )


async def latest_marks(
    conn: asyncpg.Connection, instrument_ids: Sequence[UUID]
) -> dict[UUID, tuple[Decimal, datetime]]:
    """Most recent mark per instrument, with its timestamp.

    The timestamp is returned, not discarded, so a caller can show a mark's
    age. A month-old mark rendered identically to a fresh one is a quiet way
    to mislead. An instrument with no mark is ABSENT from the mapping --
    never present with a zero, since `mark_price_chk` permits a genuine 0.

    Some ids passed in may not be real instrument ids at all -- callers may
    hand over a trade id used as a grouping key when an instrument could not
    be resolved (see db/positions.py). Such an id simply matches no row here;
    that is the correct outcome, not an error.
    """
    if not instrument_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (instrument_id) instrument_id, price, as_of
          FROM mark
         WHERE instrument_id = ANY($1::uuid[])
         ORDER BY instrument_id, as_of DESC
        """,
        list(instrument_ids),
    )
    return {r["instrument_id"]: (r["price"], r["as_of"]) for r in rows}


async def resolve_instrument_by_symbol(conn: asyncpg.Connection, symbol: str) -> UUID:
    """Symbol -> instrument id, refusing ambiguity.

    `instrument.symbol` is NOT unique -- only `natural_key` is. Two
    instruments can legitimately share a symbol (the same ticker quoted in
    two currencies, for instance). Returning "the first match" would mark the
    wrong instrument and produce a wrong unrealized figure with nothing
    indicating it, so an ambiguous symbol raises and names every candidate.
    """
    rows = await conn.fetch(
        """
        SELECT id, natural_key FROM instrument
         WHERE upper(symbol) = upper($1)
         ORDER BY natural_key
        """,
        symbol,
    )
    if not rows:
        raise ValueError(f"no instrument with symbol {symbol!r}")
    if len(rows) > 1:
        keys = ", ".join(r["natural_key"] for r in rows)
        raise ValueError(
            f"symbol {symbol!r} matches {len(rows)} instruments; "
            f"disambiguate by natural_key (candidates: {keys})"
        )
    return rows[0]["id"]
