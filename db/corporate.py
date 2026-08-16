# db/corporate.py
"""Corporate action storage, and the preview of what applying one would change.

The adjustment itself lives in ledger/corporate.py and is never performed here:
this module fetches, maps to the pure dataclass, and delegates -- the same
shape db/cash.py has.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from uuid import UUID

import asyncpg

from db.fills import fetch_fills
from ledger.corporate import ActionType, CorporateAction, adjust_fills
from ledger.types import Fill


@dataclass(frozen=True, slots=True)
class EffectPreview:
    accounts: int
    fills_changed: int
    # (before, after) pairs, capped -- the preview is for a human deciding
    # whether the ratio is right, not an audit log.
    samples: tuple[tuple[Fill, Fill], ...]


_SAMPLE_CAP = 3


def _to_action(row: asyncpg.Record) -> CorporateAction:
    return CorporateAction(
        instrument_id=row["instrument_id"],
        action_type=ActionType(row["action_type"]),
        ex_date=row["ex_date"],
        ratio_numerator=row["ratio_numerator"],
        ratio_denominator=row["ratio_denominator"],
        resulting_instrument_id=row["resulting_instrument_id"],
        cash_component=row["cash_component"],
        basis_allocation=row["basis_allocation"],
    )


async def _fetch_actions_for_instruments(
    conn: asyncpg.Connection, instrument_ids: Sequence[UUID]
) -> list[tuple[UUID, CorporateAction]]:
    """(id, CorporateAction) pairs for every action touching any of
    `instrument_ids`, either as the instrument it applies to or as the
    instrument it produces.

    Single source of truth for that row set, shared by `actions_for_instruments`
    and `preview_effect`'s `removing=` branch -- so "stored" and "proposed"
    differ by exactly the removed action, rather than by two independently
    written queries that happen to usually agree. The `removing=` branch needs
    the id to drop by; `actions_for_instruments` only needs the dataclass, so
    it discards the id at the end.
    """
    if not instrument_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT * FROM corporate_action
         WHERE instrument_id = ANY($1::uuid[]) OR resulting_instrument_id = ANY($1::uuid[])
         ORDER BY ex_date
        """,
        list(instrument_ids),
    )
    return [(r["id"], _to_action(r)) for r in rows]


async def add_action(
    conn: asyncpg.Connection, action: CorporateAction, note: str | None = None
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO corporate_action
            (instrument_id, action_type, ex_date, ratio_numerator,
             ratio_denominator, resulting_instrument_id, cash_component,
             basis_allocation, note)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        RETURNING id
        """,
        action.instrument_id,
        action.action_type.value,
        action.ex_date,
        action.ratio_numerator,
        action.ratio_denominator,
        action.resulting_instrument_id,
        action.cash_component,
        action.basis_allocation,
        note,
    )


async def list_actions(
    conn: asyncpg.Connection, instrument_id: UUID | None = None
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """
            SELECT * FROM corporate_action
             WHERE ($1::uuid IS NULL OR instrument_id = $1)
             ORDER BY ex_date, action_type
            """,
            instrument_id,
        )
    )


async def remove_action(conn: asyncpg.Connection, action_id: UUID) -> bool:
    """True if a row was deleted. False means the id was unknown -- the caller
    refuses rather than reporting a successful no-op."""
    result = await conn.execute("DELETE FROM corporate_action WHERE id = $1", action_id)
    return result != "DELETE 0"


async def find_duplicate(
    conn: asyncpg.Connection,
    instrument_id: UUID,
    ex_date: date,
    action_type: ActionType,
) -> UUID | None:
    """The id of an existing action with the same key, or None.

    There is no UNIQUE constraint on the table (adding one is a migration and
    is out of scope), so this is an application-level guard. Its absence at the
    database level is a recorded gap.
    """
    return await conn.fetchval(
        """
        SELECT id FROM corporate_action
         WHERE instrument_id = $1 AND ex_date = $2 AND action_type = $3
         LIMIT 1
        """,
        instrument_id,
        ex_date,
        action_type.value,
    )


async def actions_for_instruments(
    conn: asyncpg.Connection, instrument_ids: Sequence[UUID]
) -> list[CorporateAction]:
    pairs = await _fetch_actions_for_instruments(conn, instrument_ids)
    return [action for _id, action in pairs]


async def preview_effect(
    conn: asyncpg.Connection,
    instrument_id: UUID,
    *,
    adding: CorporateAction | None = None,
    removing: UUID | None = None,
) -> EffectPreview:
    """What would change if `adding` were stored, or `removing` deleted.

    CUMULATIVE, not the proposed action against raw fills. With one 1:6 split
    already stored, previewing a second identical one against raw fills would
    print the same plausible 1800 -> 300 while the stored state silently became
    1800 -> 50. See the design's section 5.

    `stored` and `proposed` are both built from `_fetch_actions_for_instruments`,
    the same helper `actions_for_instruments` uses -- so they differ by exactly
    the added/removed action, not by two queries with different predicates
    (one scoped by instrument_id only, the other also matching
    resulting_instrument_id) that could silently select different row sets.
    """
    pairs = await _fetch_actions_for_instruments(conn, [instrument_id])
    stored = [action for _id, action in pairs]
    if adding is not None:
        proposed = [*stored, adding]
    else:
        keep = {row_id: action for row_id, action in pairs}
        keep.pop(removing, None)
        proposed = list(keep.values())

    account_ids = [
        r["account_id"]
        for r in await conn.fetch(
            "SELECT DISTINCT account_id FROM fill WHERE instrument_id = $1", instrument_id
        )
    ]

    accounts = 0
    changed = 0
    samples: list[tuple[Fill, Fill]] = []
    for account_id in account_ids:
        fills = await fetch_fills(conn, account_id)
        before = {f.id: f for f in adjust_fills(fills, stored)}
        after = {f.id: f for f in adjust_fills(fills, proposed)}
        touched = 0
        for fill_id, b in before.items():
            a = after.get(fill_id)
            if a is None or a.quantity != b.quantity or a.price != b.price:
                touched += 1
                if len(samples) < _SAMPLE_CAP and a is not None:
                    samples.append((b, a))
        if touched:
            accounts += 1
            changed += touched

    return EffectPreview(accounts=accounts, fills_changed=changed, samples=tuple(samples))
