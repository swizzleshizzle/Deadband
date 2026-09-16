"""Which instruments a mark would actually value.

One rule, one home. `GET /api/marks` renders this set as a table to type into
and `GET /api/quotes` proposes prices for it; if the two ever disagreed, the
quotes endpoint would either propose a price for a row the user cannot see or
silently skip one they can. The filter is small enough to have been inlined in
both, which is exactly how it would have drifted.
"""

from __future__ import annotations

import asyncpg

from db.positions import open_positions
from ledger.positions import OpenPosition


async def markable_positions(conn: asyncpg.Connection) -> list[OpenPosition]:
    """Open positions excluding the unvaluable ones.

    `unvaluable_reason is not None` means the position is not priced against
    a mark at all -- api/dashboard.py leaves those out of its latest_marks
    call for the same reason. Offering an action that changes nothing is
    worse than not offering it.
    """
    return [p for p in await open_positions(conn, None) if p.unvaluable_reason is None]
