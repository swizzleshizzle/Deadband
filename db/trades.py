"""Persist derived trades. The grouping and P&L logic itself lives in ledger/ —
this module only moves data between that pure core and Postgres."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID

import asyncpg

from db.accounts import UnknownAccountError, get_account
from db.corporate import actions_with_ids_for_instruments
from db.fills import fetch_fills
from db.instruments import get_multipliers

# `_spinoff_fill_id` is private by name, but this is the allowed import direction
# (db -> ledger; the purity test only forbids the reverse), and inverting this
# exact hash is how a derived fill's provenance is recovered without re-deriving
# WHICH fills a spinoff applies to. See the design's section 5.1a.
from ledger.corporate import ActionType, _spinoff_fill_id, adjust_fills
from ledger.grouping import group_fills
from ledger.pnl import compute_pnl
from ledger.types import TradeIntent


class UnattributableDerivedFillError(RuntimeError):
    """adjust_fills produced a synthetic fill whose id inverts to no known
    (parent, action) pair. See the design's section 5.1a."""

    def __init__(self, fill_id: UUID) -> None:
        super().__init__(f"cannot attribute derived fill {fill_id} to a corporate action")
        self.fill_id = fill_id


# The trade UPSERT, in two forms. A spinoff-derived trade's opening allocation is
# a derived_fill row rather than a fill row, so its id belongs in
# opening_derived_fill_id and its ON CONFLICT target is the partial unique index
# over that column instead. Both forms are generated from one body so the column
# list, the placeholders and the DO UPDATE SET cannot drift apart -- in
# particular effective_instrument_id must be written by both, since it is the
# only place a derived trade's instrument comes from at all (db/positions.py).
# Only one of the two opening columns is ever written; the other stays NULL,
# which is what trade's "at most one opening" CHECK requires.
_TRADE_UPSERT_BODY = """
                INSERT INTO trade (
                    account_id, {opening}, primary_underlying, effective_instrument_id,
                    direction, status, intent, grouping_mode, opened_at, closed_at, qty_opened,
                    qty_closed, avg_entry, avg_exit, realized_pnl, gross_realized_pnl,
                    fees_total, fees_realized, open_quantity, open_cost_basis, is_estimated
                ) VALUES (
                    $1,$2,$3,$4,$5,$6,$7,'auto',$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20
                )
                ON CONFLICT (account_id, {opening}) WHERE {opening} IS NOT NULL
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
"""
_TRADE_UPSERT_ON_FILL = _TRADE_UPSERT_BODY.format(opening="opening_fill_id")
_TRADE_UPSERT_ON_DERIVED = _TRADE_UPSERT_BODY.format(opening="opening_derived_fill_id")


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
    #
    # Every action type except spinoff rescales or remaps a fill in place, keeping
    # its id; a spinoff additionally MINTS a second fill for the child instrument
    # (spec section 5.1). Those synthetic ids have no `fill` row behind them, so
    # they are identified here by set difference against the ids that were
    # fetched, and persisted to `derived_fill` below before any trade points at
    # them.
    real_ids = {f.id for f in fills}
    derived_provenance: dict[UUID, tuple[UUID, UUID]] = {}

    if fills:
        pairs = await actions_with_ids_for_instruments(
            conn, list({f.instrument_id for f in fills})
        )
        if pairs:
            # Invert _spinoff_fill_id over (candidate parent x spinoff action): a
            # handful of hashes, giving every id a spinoff COULD mint and the
            # (root real fill, action) it would have come from -- see the note on
            # the root below. adjust_fills returns bare Fills with no link
            # back to the action that produced them, and derived_fill's two
            # provenance columns are NOT NULL. Enumerating a hash cannot drift from
            # adjust_fills; re-deciding WHICH fills a spinoff applies to would
            # duplicate _ordered_actions' ordering and ex-date rules in a second
            # place and silently disagree the first time either changes. See the
            # design's section 5.1a.
            #
            # The candidate parents are the CLOSURE of the fetched ids under the
            # spinoff actions, not the fetched ids alone: a spinoff whose source
            # instrument is another spinoff's resulting instrument applies to the
            # first one's synthetic child, and seeding with real_ids only would
            # leave that grandchild inverting to nothing -- which raised
            # UnattributableDerivedFillError for the whole account, on every
            # regroup, exactly the wedge this branch exists to remove.
            #
            # The round bound is what makes this terminate. _ordered_actions
            # applies each action at most once per lineage, so no real chain can
            # be deeper than the number of spinoff actions; without the bound the
            # closure would keep hashing its own output forever, since applying an
            # action to its own child yields a fresh id every time.
            #
            # What is recorded as the parent is the lineage ROOT -- the real fill
            # the chain started from -- not the immediate parent. For a one-step
            # spinoff those are the same id. For a chain they are not, and the
            # root is the only one that can be stored: derived_fill.
            # derived_from_fill_id references fill(id), and an intermediate
            # derived fill has no `fill` row (verified: the immediate parent
            # raises ForeignKeyViolationError on derived_fill_derived_from_fill_id_fkey).
            # Nothing is lost that cannot be reconstructed -- corporate_action_id
            # names the exact action, and the stored action set gives every
            # intermediate step. Recovering the immediate parent directly would
            # need a derived_from_derived_fill_id column; recorded as a gap.
            spinoffs = [(aid, a) for aid, a in pairs if a.action_type is ActionType.SPINOFF]
            candidates = set(real_ids)
            roots: dict[UUID, UUID] = {fill_id: fill_id for fill_id in real_ids}
            for _round in range(len(spinoffs)):
                minted: set[UUID] = set()
                for action_id, action in spinoffs:
                    for parent_id in candidates:
                        child_id = _spinoff_fill_id(parent_id, action)
                        if child_id in derived_provenance:
                            continue
                        derived_provenance[child_id] = (roots[parent_id], action_id)
                        roots[child_id] = roots[parent_id]
                        minted.add(child_id)
                if not minted:
                    break
                candidates |= minted
            fills = adjust_fills(fills, [a for _id, a in pairs])

    derived = [f for f in fills if f.id not in real_ids]

    # Write order is forced by the foreign keys (spec section 5.2): the derived
    # rows exist before the trades and trade_fill rows that reference them, and
    # the stale ones are reaped at the very end, after those references are gone.
    # ON CONFLICT (id) DO UPDATE rather than delete-and-insert: the uuid5 is
    # stable across regroups, so a live derived fill keeps its identity (and any
    # trade opening on it) while its quantity and price are refreshed.
    for d in derived:
        provenance = derived_provenance.get(d.id)
        if provenance is None:
            # Not a guess-and-insert: a synthetic fill we cannot attribute means
            # adjust_fills minted an id we do not model, and inserting it with
            # NULL provenance would bury that.
            raise UnattributableDerivedFillError(d.id)
        # `root_fill_id`, not `parent_id`: derived_from_fill_id holds the REAL
        # fill the lineage started from. For a one-step spinoff that is the
        # immediate parent; for a chained one it is the grandparent, because the
        # immediate parent is itself a derived_fill row and this column
        # references fill(id). See the closure above.
        root_fill_id, action_id = provenance
        await conn.execute(
            """
            INSERT INTO derived_fill
                (id, account_id, instrument_id, executed_at, side, quantity, price,
                 fee, is_estimated, derived_from_fill_id, corporate_action_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (id) DO UPDATE SET
                instrument_id = EXCLUDED.instrument_id,
                quantity      = EXCLUDED.quantity,
                price         = EXCLUDED.price
            """,
            d.id,
            account_id,
            d.instrument_id,
            d.executed_at,
            d.side.value,
            d.quantity,
            d.price,
            d.fee,
            d.is_estimated,
            root_fill_id,
            action_id,
        )

    derived_ids = {f.id for f in derived}
    # Two seen-lists, not one: each trade's opening allocation is either a real
    # fill or a derived one, and the reaping predicates below have to test
    # membership in whichever list matches that trade's opening kind.
    seen_openings: list[UUID] = []
    seen_derived_openings: list[UUID] = []
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
            # `opening_id`, not `opening_fill_id`: for a spinoff-derived trade it
            # is a derived_fill id, and which of trade's two opening columns it
            # is written to below follows from that.
            opening_id = opening_allocation.fill_id
            is_derived_opening = opening_id in derived_ids
            if is_derived_opening:
                seen_derived_openings.append(opening_id)
            else:
                seen_openings.append(opening_id)

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
                _TRADE_UPSERT_ON_DERIVED if is_derived_opening else _TRADE_UPSERT_ON_FILL,
                account_id,
                opening_id,
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
            #
            # Each allocation routes to exactly one of fill_id / derived_fill_id
            # (trade_fill_one_source_chk requires exactly one to be non-NULL), on
            # the same membership test the opening allocation used above. A
            # synthetic id sent to fill_id violates trade_fill_fill_fk.
            await conn.executemany(
                "INSERT INTO trade_fill "
                "(trade_id, fill_id, derived_fill_id, account_id, quantity) "
                "VALUES ($1,$2,$3,$4,$5)",
                [
                    (
                        trade_id,
                        None if a.fill_id in derived_ids else a.fill_id,
                        a.fill_id if a.fill_id in derived_ids else None,
                        account_id,
                        a.quantity,
                    )
                    for a in g.allocations
                ],
            )
            written += 1

    # Single protection step, AFTER grouping and BEFORE the final DELETE. A trade
    # is stale here if it either lost its opening allocation entirely (BOTH
    # opening columns NULL — deleted) or its opening allocation no longer opens
    # anything (not in the seen-list matching its opening kind — a backdated fill
    # changed the grouping).
    #
    # The predicate tests each trade against the ONE list its opening kind belongs
    # to (spec section 5.3). A spinoff-derived trade has opening_fill_id NULL by
    # construction, so the older `opening_fill_id IS NULL OR ...` spelling called
    # every such trade stale and reaped it in the very next statement — quietly,
    # since it happens after the trade was correctly written. "Both columns NULL"
    # is still stale, which preserves the orphan path exactly.
    # By this point every live fill, including any that partially belonged to a
    # stale trade via a zero-crossing split, has already been reallocated in
    # full to a fresh auto trade above — that is what makes a single pass here
    # correct where the old two-pass version was not. If a stale trade carries
    # user content, convert it to a permanent manual record: free BOTH opening
    # columns (so a future auto upsert can never collide with it on either
    # partial unique index) and null every derived column (it owns zero fills
    # now; leaving stale P&L on it would double-count against whatever trade its
    # fills now belong to).
    # effective_instrument_id is one of those derived columns: it is written
    # only from a live opening allocation's fill, so once opening_fill_id is
    # freed there is no live fill left to have derived it from. Leaving it
    # behind would let db/positions.py's COALESCE resolve a stale instrument
    # for a trade that no longer has one -- the same "unreachable instrument
    # must not look reachable" contract open_quantity/open_cost_basis are
    # nulled here to uphold.
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
               opening_derived_fill_id = NULL,
               effective_instrument_id = NULL,
               qty_opened = NULL, qty_closed = NULL,
               avg_entry = NULL, avg_exit = NULL,
               realized_pnl = NULL, gross_realized_pnl = NULL,
               fees_total = NULL, fees_realized = NULL,
               open_quantity = NULL, open_cost_basis = NULL,
               r_multiple = NULL,
               is_estimated = FALSE
         WHERE account_id = $1
           AND grouping_mode = 'auto'
           AND ((opening_fill_id IS NULL AND opening_derived_fill_id IS NULL)
                OR (opening_fill_id IS NOT NULL
                    AND NOT (opening_fill_id = ANY($2::uuid[])))
                OR (opening_derived_fill_id IS NOT NULL
                    AND NOT (opening_derived_fill_id = ANY($4::uuid[]))))
           AND (notes IS NOT NULL OR planned_risk IS NOT NULL
                OR strategy_tag IS NOT NULL OR intent <> $3)
        RETURNING id
        """,
        account_id,
        seen_openings,
        intent,
        seen_derived_openings,
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
           AND ((opening_fill_id IS NULL AND opening_derived_fill_id IS NULL)
                OR (opening_fill_id IS NOT NULL
                    AND NOT (opening_fill_id = ANY($2::uuid[])))
                OR (opening_derived_fill_id IS NOT NULL
                    AND NOT (opening_derived_fill_id = ANY($3::uuid[]))))
        """,
        account_id,
        seen_openings,
        seen_derived_openings,
    )

    # Derived fills are reaped LAST, after every trade and trade_fill row that
    # could reference them is gone (spec section 5.2). Doing it earlier would let
    # trade_opening_derived_fill_fk's ON DELETE SET NULL quietly strip a live
    # trade's opening. Unconditional, like the trade DELETE above and for the
    # same reason: when the action that produced a derived fill is removed, this
    # run produces no derived fills at all and the old rows must not survive it
    # -- that is what makes removal a genuine undo (spec section 5.4).
    await conn.execute(
        """
        DELETE FROM derived_fill
         WHERE account_id = $1
           AND NOT (id = ANY($2::uuid[]))
        """,
        account_id,
        list(derived_ids),
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
