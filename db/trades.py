"""Persist derived trades. The grouping and P&L logic itself lives in ledger/ —
this module only moves data between that pure core and Postgres."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import asyncpg

from db.accounts import UnknownAccountError, get_account
from db.corporate import actions_for_instruments
from db.fills import fetch_fills
from db.instruments import get_multipliers
from ledger.corporate import adjust_fills
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
    #
    # How much of each fill is already held by a manual trade. A manual trade may
    # hold only PART of a zero-crossing fill, so excluding the fill whole would
    # strand -- and then reap -- the remainder. Reduce the available quantity
    # instead, and let the pure grouper allocate what is left.
    manual_held: dict[UUID, Decimal] = {
        r["fill_id"]: r["held"]
        for r in await conn.fetch(
            """SELECT tf.fill_id, sum(tf.quantity) AS held
                 FROM trade_fill tf
                 JOIN trade t ON t.id = tf.trade_id
                WHERE t.account_id = $1 AND t.grouping_mode = 'manual'
             GROUP BY tf.fill_id""",
            account_id,
        )
    }

    fills = []
    for f in await fetch_fills(conn, account_id):
        remaining = f.quantity - manual_held.get(f.id, Decimal(0))
        if remaining <= 0:
            continue  # wholly owned by a manual trade
        fills.append(
            f
            if remaining == f.quantity
            else replace(f, quantity=remaining, fee=f.fee * remaining / f.quantity)
        )

    # Corporate actions are applied HERE: after the manual reduction, before
    # grouping -- and never written back to the fill table. Fills are ground
    # truth, an action is a separate fact, and the adjusted view is a
    # consequence of both. That is what makes removing an action a genuine undo
    # rather than a second restatement.
    #
    # THE ORDER IS LOAD-BEARING. trade_fill quantities were recorded in the
    # units that existed when a manual grouping was made -- pre-split units. If
    # adjustment ran first, a fill scaled from 1800 to 300 would be compared
    # against a manual holding of 1800, yield a negative remainder, and be
    # dropped entirely: the fill would vanish from the ledger rather than
    # merely being mis-sized.
    #
    # A consequence, recorded as a gap rather than solved here: fills WHOLLY
    # owned by a manual trade never reach this point (they are skipped above),
    # so manual groupings are not split-adjusted.
    if fills:
        actions = await actions_for_instruments(
            conn, list({f.instrument_id for f in fills})
        )
        if actions:
            fills = adjust_fills(fills, actions)

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
            opening_allocation = min(
                g.allocations, key=lambda a: (by_id[a.fill_id].executed_at, str(a.fill_id))
            )
            opening_fill_id = opening_allocation.fill_id
            seen_openings.append(opening_fill_id)

            # Any estimated fill taints the trade -- an opening-balance fill
            # makes the whole trade's P&L an estimate (spec section 4). ANY,
            # not ALL: this must roll up every constituent fill, not just the
            # opening one.
            is_estimated = any(
                by_id[a.fill_id].is_estimated for a in g.allocations
            )

            # UPSERT, never delete-and-rebuild: derived columns are overwritten,
            # user-authored ones (intent override, planned_risk, strategy_tag, notes,
            # and B's thesis link) are left exactly as the user set them.
            trade_id = await conn.fetchval(
                """
                INSERT INTO trade (
                    account_id, opening_fill_id, primary_underlying, effective_instrument_id,
                    direction, status, intent, grouping_mode, opened_at, closed_at, qty_opened,
                    qty_closed, avg_entry, avg_exit, realized_pnl, gross_realized_pnl,
                    fees_total, fees_realized, open_quantity, open_cost_basis, is_estimated
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,'auto',$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20
                )
                ON CONFLICT (account_id, opening_fill_id) WHERE opening_fill_id IS NOT NULL
                DO UPDATE SET
                    primary_underlying       = EXCLUDED.primary_underlying,
                    effective_instrument_id  = EXCLUDED.effective_instrument_id,
                    direction                = EXCLUDED.direction,
                    status                   = EXCLUDED.status,
                    opened_at                = EXCLUDED.opened_at,
                    closed_at                = EXCLUDED.closed_at,
                    qty_opened               = EXCLUDED.qty_opened,
                    qty_closed               = EXCLUDED.qty_closed,
                    avg_entry                = EXCLUDED.avg_entry,
                    avg_exit                 = EXCLUDED.avg_exit,
                    realized_pnl             = EXCLUDED.realized_pnl,
                    gross_realized_pnl       = EXCLUDED.gross_realized_pnl,
                    fees_total               = EXCLUDED.fees_total,
                    fees_realized            = EXCLUDED.fees_realized,
                    open_quantity            = EXCLUDED.open_quantity,
                    open_cost_basis          = EXCLUDED.open_cost_basis,
                    is_estimated             = EXCLUDED.is_estimated,
                    updated_at               = now()
                RETURNING id
                """,
                account_id,
                opening_fill_id,
                underlyings.get(g.instrument_ids[0]),
                by_id[opening_allocation.fill_id].instrument_id,
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
                pnl.fees_realized,
                pnl.open_quantity,
                pnl.open_cost_basis,
                is_estimated,
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
    # `is_estimated` is likewise NOT NULL DEFAULT FALSE, so NULL isn't an option
    # either — but unlike `status`, FALSE is a deliberate and correct value
    # here, not just the nearest available one: a trade owning zero live fills
    # has nothing estimated about it (its P&L is NULL, not a real-but-uncertain
    # number), and leaving a stale True from before protection would misrepresent
    # a judgment-only record as still carrying a reconstructed-price P&L.
    protected = await conn.fetch(
        """
        UPDATE trade
           SET grouping_mode = 'manual',
               updated_at = now(),
               opening_fill_id = NULL,
               qty_opened = NULL, qty_closed = NULL,
               avg_entry = NULL, avg_exit = NULL,
               realized_pnl = NULL, gross_realized_pnl = NULL,
               fees_total = NULL, fees_realized = NULL,
               open_quantity = NULL, open_cost_basis = NULL,
               r_multiple = NULL,
               is_estimated = FALSE
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
