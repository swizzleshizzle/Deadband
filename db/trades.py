"""Persist derived trades. The grouping and P&L logic itself lives in ledger/ —
this module only moves data between that pure core and Postgres."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from db.accounts import UnknownAccountError, get_account
from db.fills import fetch_fills
from db.instruments import get_multipliers
from ledger.grouping import group_fills
from ledger.pnl import compute_pnl
from ledger.types import TradeIntent


async def regroup_account(conn: asyncpg.Connection, account_id: UUID) -> int:
    """Recompute auto-grouped trades for an account. Manual groupings are permanent
    and are never touched (spec §5)."""
    # An account_id with no matching row used to reach TradeIntent(None) below
    # (fetchval returns None on no match, and None != "mixed"), raising
    # `ValueError: None is not a valid TradeIntent` — a message that never
    # names the offending account id and looks like a corrupt enum rather than
    # a plain "you gave me an id that doesn't exist." cmd_import already
    # handles this properly via get_account + a clean CLI error; this makes
    # regroup_account (and therefore cmd_regroup) do the same.
    account = await get_account(conn, account_id)
    if account is None:
        raise UnknownAccountError(account_id)
    default_intent = account["default_intent"]
    intent = (
        TradeIntent.UNASSIGNED.value
        if default_intent == "mixed"
        else TradeIntent(default_intent).value
    )

    # A trade is only excluded from this run's auto pass if it is *already*
    # manual going into this call. Fix-round-2 correction: an earlier version
    # protected orphaned trades in a "Pass A" here, before grouping, so their
    # fills would be excluded below. That is wrong for a zero-crossing fill: a
    # fill that both closes trade X and opens trade Y is only PARTLY X's.
    # Excluding it whole starved Y of its opening allocation, and Y's share was
    # silently reaped by the final DELETE (verified: 16/200 fuzz cases lost an
    # open position this way, with zero over-allocation — the original
    # double-count bug was fixed, but a new under-allocation bug replaced it).
    # A genuinely manual (user-created or previously-protected) trade has no
    # such problem — its fills were never partially claimed by anything else —
    # so only those are excluded here. Every other trade, including one that
    # is about to be protected below, is regrouped in full first.
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

    # A manual trade holding only PART of a fill would make the auto pass exclude
    # that fill whole via manual_fill_ids above, stranding the rest of its
    # quantity — the same failure shape as round 2's Pass A bug, reached through a
    # hand-marked manual trade instead of the protection step. Nothing in db/,
    # ledger/, or importers/ creates that state today (the only writer of
    # grouping_mode='manual' is the protection step, which drops its allocations
    # first), but a future manual-grouping UI could. Fail loudly rather than
    # silently losing an open position.
    partial = await conn.fetch(
        """
        SELECT tf.fill_id, f.quantity AS fill_quantity, SUM(tf.quantity) AS held
          FROM trade_fill tf
          JOIN trade t ON t.id = tf.trade_id
          JOIN fill  f ON f.id = tf.fill_id
         WHERE t.account_id = $1 AND t.grouping_mode = 'manual'
         GROUP BY tf.fill_id, f.quantity
        HAVING SUM(tf.quantity) < f.quantity
        """,
        account_id,
    )
    if partial:
        raise NotImplementedError(
            "a manual trade holds a partial allocation of "
            f"fill {partial[0]['fill_id']} ({partial[0]['held']} of "
            f"{partial[0]['fill_quantity']}); regrouping would strand the remainder. "
            "Partial manual allocations need quantity-aware exclusion in group_fills."
        )

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

    # Single protection step, AFTER grouping and BEFORE the final DELETE. A trade
    # is stale here if it either lost its opening fill entirely
    # (opening_fill_id IS NULL — deleted) or its opening fill no longer opens
    # anything (NOT IN seen_openings — a backdated fill changed the grouping).
    # By this point every live fill, including any that partially belonged to a
    # stale trade via a zero-crossing split, has already been reallocated in
    # full to a fresh auto trade above — that is what makes a single pass here
    # correct where the old two-pass version was not. If a stale trade carries
    # user content, convert it to a permanent manual record: free its
    # opening_fill_id (so a future auto upsert can never collide with it) and
    # null every derived column (it owns zero fills now; leaving stale P&L on
    # it would double-count against whatever trade its fills now belong to).
    # `status` is left as-is — it is NOT NULL and no longer meaningful once the
    # row is judgment-only, but there is no null-able substitute for it.
    protected = await conn.fetch(
        """
        UPDATE trade
           SET grouping_mode = 'manual',
               updated_at = now(),
               opening_fill_id = NULL,
               qty_opened = NULL, qty_closed = NULL,
               avg_entry = NULL, avg_exit = NULL,
               realized_pnl = NULL, gross_realized_pnl = NULL,
               fees_total = NULL, r_multiple = NULL
         WHERE account_id = $1
           AND grouping_mode = 'auto'
           AND (opening_fill_id IS NULL OR NOT (opening_fill_id = ANY($2::uuid[])))
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

    # Whatever protection didn't save is genuinely stale — reaped unconditionally.
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
