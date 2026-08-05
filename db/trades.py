"""Persist derived trades. The grouping and P&L logic itself lives in ledger/ —
this module only moves data between that pure core and Postgres."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from db.fills import fetch_fills
from db.instruments import get_multipliers
from ledger.grouping import group_fills
from ledger.pnl import compute_pnl
from ledger.types import TradeIntent


async def regroup_account(conn: asyncpg.Connection, account_id: UUID) -> int:
    """Recompute auto-grouped trades for an account. Manual groupings are permanent
    and are never touched (spec §5)."""
    default_intent = await conn.fetchval(
        "SELECT default_intent FROM account WHERE id = $1", account_id
    )
    intent = (
        TradeIntent.UNASSIGNED.value
        if default_intent == "mixed"
        else TradeIntent(default_intent).value
    )

    manual_fill_ids = {
        r["fill_id"]
        for r in await conn.fetch(
            """
            SELECT tf.fill_id FROM trade_fill tf
            JOIN trade t ON t.id = tf.trade_id
            WHERE t.account_id = $1 AND t.grouping_mode = 'manual'
            """,
            account_id,
        )
    }

    fills = [f for f in await fetch_fills(conn, account_id) if f.id not in manual_fill_ids]
    if not fills:
        return 0

    by_id = {f.id: f for f in fills}
    multipliers = await get_multipliers(conn, [f.instrument_id for f in fills])
    symbols = {
        r["id"]: r["symbol"]
        for r in await conn.fetch(
            "SELECT id, symbol, underlying FROM instrument WHERE id = ANY($1::uuid[])",
            list({f.instrument_id for f in fills}),
        )
    }

    groups = group_fills(fills)
    seen_openings: list[UUID] = []
    written = 0

    for g in groups:
        pnl = compute_pnl(g.allocations, by_id, multipliers, g.direction)
        # The opening allocation is this trade's stable identity across regroups.
        opening_fill_id = min(
            g.allocations, key=lambda a: (by_id[a.fill_id].executed_at, str(a.fill_id))
        ).fill_id
        seen_openings.append(opening_fill_id)

        # UPSERT, never delete-and-rebuild: derived columns are overwritten,
        # user-authored ones (intent override, planned_risk, strategy_tag, notes,
        # and B's thesis link) are left exactly as the user set them.
        trade_id = await conn.fetchval(
            """
            INSERT INTO trade (
                account_id, opening_fill_id, primary_underlying, direction, status,
                intent, grouping_mode, opened_at, closed_at, qty_opened, qty_closed,
                avg_entry, avg_exit, realized_pnl, gross_realized_pnl, fees_total
            ) VALUES ($1,$2,$3,$4,$5,$6,'auto',$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT (account_id, opening_fill_id) WHERE opening_fill_id IS NOT NULL
            DO UPDATE SET
                primary_underlying = EXCLUDED.primary_underlying,
                direction          = EXCLUDED.direction,
                status             = EXCLUDED.status,
                opened_at          = EXCLUDED.opened_at,
                closed_at          = EXCLUDED.closed_at,
                qty_opened         = EXCLUDED.qty_opened,
                qty_closed         = EXCLUDED.qty_closed,
                avg_entry          = EXCLUDED.avg_entry,
                avg_exit           = EXCLUDED.avg_exit,
                realized_pnl       = EXCLUDED.realized_pnl,
                gross_realized_pnl = EXCLUDED.gross_realized_pnl,
                fees_total         = EXCLUDED.fees_total,
                updated_at         = now()
            RETURNING id
            """,
            account_id,
            opening_fill_id,
            symbols.get(g.instrument_ids[0]),
            g.direction.value,
            g.status.value,
            intent,
            g.opened_at,
            g.closed_at,
            pnl.qty_opened,
            pnl.qty_closed,
            pnl.avg_entry,
            pnl.avg_exit,
            pnl.realized_pnl,
            pnl.gross_realized_pnl,
            pnl.fees_total,
        )

        # r_multiple depends on planned_risk, which is user-authored — recompute it
        # from whatever risk the user has recorded rather than overwriting with NULL.
        await conn.execute(
            """
            UPDATE trade
               SET r_multiple = CASE
                     WHEN planned_risk IS NULL OR planned_risk = 0 THEN NULL
                     ELSE realized_pnl / planned_risk
                   END
             WHERE id = $1
            """,
            trade_id,
        )

        await conn.execute("DELETE FROM trade_fill WHERE trade_id = $1", trade_id)
        # AMENDMENT 1: trade_fill.account_id is NOT NULL and is pinned by composite
        # FK to both trade(id, account_id) and fill(id, account_id) — a cross-account
        # allocation is unrepresentable. The brief's original insert omitted the
        # column and would raise NotNullViolationError on every call.
        await conn.executemany(
            "INSERT INTO trade_fill (trade_id, fill_id, account_id, quantity) VALUES ($1,$2,$3,$4)",
            [(trade_id, a.fill_id, account_id, a.quantity) for a in g.allocations],
        )
        written += 1

    # AMENDMENT 2: opening_fill_id is ON DELETE SET NULL (not CASCADE), precisely so
    # that deleting a mis-imported fill leaves the trade — and its notes,
    # planned_risk, strategy_tag, intent — intact. A trade orphaned that way matches
    # "opening_fill_id IS NULL" and a naive DELETE here would reap it seconds later,
    # destroying the very judgment the schema was designed to preserve. So: first
    # protect anything carrying user-authored content by converting it to a manual
    # trade (manual trades are never touched by the auto pass), then delete only
    # what is left — genuinely stale auto trades with nothing to lose.
    await conn.execute(
        """
        UPDATE trade
           SET grouping_mode = 'manual', updated_at = now()
         WHERE account_id = $1
           AND grouping_mode = 'auto'
           AND (opening_fill_id IS NULL OR NOT (opening_fill_id = ANY($2::uuid[])))
           AND (notes IS NOT NULL OR planned_risk IS NOT NULL OR strategy_tag IS NOT NULL)
        """,
        account_id,
        seen_openings,
    )
    await conn.execute(
        """
        DELETE FROM trade
         WHERE account_id = $1
           AND grouping_mode = 'auto'
           AND (opening_fill_id IS NULL OR NOT (opening_fill_id = ANY($2::uuid[])))
        """,
        account_id,
        seen_openings,
    )

    return written


async def list_trades(
    conn: asyncpg.Connection, account_id: UUID | None = None
) -> list[asyncpg.Record]:
    if account_id:
        return await conn.fetch(
            "SELECT * FROM trade WHERE account_id = $1 ORDER BY opened_at DESC",
            account_id,
        )
    return await conn.fetch("SELECT * FROM trade ORDER BY opened_at DESC")
