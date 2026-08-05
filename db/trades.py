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

    # PASS A (before grouping): a trade orphaned by a deleted fill has
    # opening_fill_id IS NULL (ON DELETE SET NULL, not CASCADE — see schema).
    # If it carries user content, protect it as manual *before* computing
    # manual_fill_ids, so its still-live fills (if any) land in manual_fill_ids
    # and are excluded from the auto pass below. It keeps its existing
    # trade_fill allocations untouched, exactly like any user-created manual
    # trade — there is nothing stale to drop here since none of its fills are
    # about to be regrouped.
    await conn.execute(
        """
        UPDATE trade
           SET grouping_mode = 'manual', updated_at = now()
         WHERE account_id = $1
           AND grouping_mode = 'auto'
           AND opening_fill_id IS NULL
           AND (notes IS NOT NULL OR planned_risk IS NOT NULL
                OR strategy_tag IS NOT NULL OR intent <> $2)
        """,
        account_id,
        intent,  # the account-derived default; anything else is a user override
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

    seen_openings: list[UUID] = []
    written = 0

    if fills:
        by_id = {f.id: f for f in fills}
        multipliers = await get_multipliers(conn, [f.instrument_id for f in fills])
        # IMPORTANT 3: primary_underlying must roll options up under their stock
        # (e.g. 'SPY'), not store the option's own contract symbol. COALESCE falls
        # back to symbol for instruments (equities, crypto) that have no underlying.
        underlyings = {
            r["id"]: r["primary_underlying"]
            for r in await conn.fetch(
                """
                SELECT id, COALESCE(underlying, symbol) AS primary_underlying
                  FROM instrument WHERE id = ANY($1::uuid[])
                """,
                list({f.instrument_id for f in fills}),
            )
        }

        groups = group_fills(fills)

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
                underlyings.get(g.instrument_ids[0]),
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

            # r_multiple depends on planned_risk, which is user-authored — recompute
            # it from whatever risk the user has recorded rather than overwriting
            # with NULL.
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
            # AMENDMENT 1: trade_fill.account_id is NOT NULL and is pinned by
            # composite FK to both trade(id, account_id) and fill(id, account_id) —
            # a cross-account allocation is unrepresentable. The brief's original
            # insert omitted the column and would raise NotNullViolationError on
            # every call.
            await conn.executemany(
                "INSERT INTO trade_fill (trade_id, fill_id, account_id, quantity) "
                "VALUES ($1,$2,$3,$4)",
                [(trade_id, a.fill_id, account_id, a.quantity) for a in g.allocations],
            )
            written += 1

    # PASS B (after grouping): a trade whose opening_fill_id is still set but no
    # longer opens anything (a backdated fill changed the grouping) has just had
    # its fills reallocated to new auto trades above. If it carries user content,
    # protecting it as manual must ALSO drop its now-stale trade_fill rows, or the
    # same fills end up allocated to both the new auto trade and this one —
    # double-counted forever. A protected Pass-B trade becomes a judgment-only
    # record with no allocations; the user re-links it. That is the honest outcome:
    # the fills genuinely belong to a different trade now.
    protected = await conn.fetch(
        """
        UPDATE trade
           SET grouping_mode = 'manual', updated_at = now()
         WHERE account_id = $1
           AND grouping_mode = 'auto'
           AND opening_fill_id IS NOT NULL
           AND NOT (opening_fill_id = ANY($2::uuid[]))
           AND (notes IS NOT NULL OR planned_risk IS NOT NULL
                OR strategy_tag IS NOT NULL OR intent <> $3)
        RETURNING id
        """,
        account_id,
        seen_openings,
        intent,
    )
    if protected:
        await conn.execute(
            "DELETE FROM trade_fill WHERE trade_id = ANY($1::uuid[])",
            [r["id"] for r in protected],
        )

    # Whatever neither pass protected is genuinely stale — reaped unconditionally.
    # This runs even when `fills` was empty (IMPORTANT 1): an account whose fills
    # were all deleted must not keep reporting phantom P&L from auto trades that no
    # longer correspond to anything.
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
