"""Commit a parsed import batch. The parse and preview phases are pure and live in
importers/; this is the only phase that writes.

This module never opens its own transaction — the caller (cli.py's cmd_import)
wraps commit_batch + regroup_account in a single `async with conn.transaction():`
so a fill can never be inserted without its corresponding trade regrouping, or
vice versa. Keeping commit_batch transaction-free is what lets it compose with
that outer transaction rather than fighting it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

import asyncpg

from db.fills import insert_fills
from db.instruments import upsert_instrument
from importers.base import ImportBatch, content_hash
from ledger.types import Fill, FillSource


@dataclass(frozen=True, slots=True)
class CommitResult:
    fills_inserted: int
    fills_skipped: int
    cash_inserted: int
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    by_account: dict[UUID, ImportBatch]
    unknown_refs: tuple[str, ...]
    ignored_refs: tuple[str, ...]


async def route_batch(conn: asyncpg.Connection, venue: str, batch: ImportBatch) -> RoutingPlan:
    """Split a parsed batch by account, one export can span several.

    Each row's external_ref (the venue's own account NUMBER -- see
    importers/fidelity.py) is matched against account.external_ref within
    the given venue:

    - No matching account -> the ref goes into `unknown_refs`. Never silently
      merged into another account.
    - A matching account with `ignore_on_import` -> the ref goes into
      `ignored_refs`; its rows are dropped from `by_account` entirely (they
      route SUCCESSFULLY -- this is the deliberate escape hatch for an
      account the user never intends to import, not a failure).
    - Otherwise -> its rows land in `by_account[account.id]`.

    A row whose external_ref is None is never routed at all -- not even to
    an account whose own external_ref is NULL. `UNIQUE (venue, external_ref)`
    does not constrain NULLs in Postgres, so several accounts can legitimately
    have none; matching on NULL would make the first such account a silent
    catch-all for every unroutable row. Comparing to NULL in SQL is always
    UNKNOWN (never true), so this falls out of the ordinary `= ANY(...)`
    lookup below without any special-casing -- the guard is in never handing
    a None ref to that lookup in the first place, and never treating an
    account row with a NULL external_ref as a match for one.
    """
    refs = sorted(
        {f.external_ref for f in batch.fills if f.external_ref is not None}
        | {c.external_ref for c in batch.cash if c.external_ref is not None}
    )

    unknown_refs: list[str] = []
    ignored_refs: list[str] = []
    # ref -> account id, only for refs that route successfully (known,
    # not ignored). Refs with no entry here are unknown or ignored -- both
    # already recorded above -- or simply have no rows (unreachable, since
    # `refs` is built only from refs that actually appear in the batch).
    routable: dict[str, UUID] = {}

    if refs:
        rows = await conn.fetch(
            """
            SELECT id, external_ref, ignore_on_import
              FROM account
             WHERE venue = $1 AND external_ref = ANY($2::text[])
            """,
            venue,
            refs,
        )
        # UNIQUE (venue, external_ref) means at most one row per ref here.
        by_ref = {r["external_ref"]: r for r in rows}

        for ref in refs:
            row = by_ref.get(ref)
            if row is None:
                unknown_refs.append(ref)
            elif row["ignore_on_import"]:
                ignored_refs.append(ref)
            else:
                routable[ref] = row["id"]

    fills_by_account: dict[UUID, list] = defaultdict(list)
    cash_by_account: dict[UUID, list] = defaultdict(list)

    for f in batch.fills:
        account_id = routable.get(f.external_ref) if f.external_ref is not None else None
        if account_id is not None:
            fills_by_account[account_id].append(f)

    for c in batch.cash:
        account_id = routable.get(c.external_ref) if c.external_ref is not None else None
        if account_id is not None:
            cash_by_account[account_id].append(c)

    by_account = {
        account_id: ImportBatch(
            fills=tuple(fills_by_account.get(account_id, ())),
            cash=tuple(cash_by_account.get(account_id, ())),
        )
        for account_id in routable.values()
    }

    return RoutingPlan(
        by_account=by_account,
        unknown_refs=tuple(unknown_refs),
        ignored_refs=tuple(ignored_refs),
    )


async def _find_instrument_by_symbol(
    conn: asyncpg.Connection, account_id: UUID, symbol: str
) -> list[UUID]:
    """Instruments already tied to this account (via a fill) whose symbol matches,
    case-insensitively. `instrument` has no account_id of its own — a fill's
    instrument_id + account_id is the only place "this account trades this
    symbol" is recorded, so that's what defines "existing instrument in that
    account" for cash-movement attribution."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT i.id
          FROM fill f
          JOIN instrument i ON i.id = f.instrument_id
         WHERE f.account_id = $1 AND lower(i.symbol) = lower($2)
        """,
        account_id,
        symbol,
    )
    return [r["id"] for r in rows]


def _append_symbol_note(note: str | None, symbol: str) -> str:
    """Preserve a cash movement's instrument attribution in the note when it
    cannot be resolved to a single instrument_id, so the information survives
    for a human to resolve later instead of being silently dropped."""
    tag = f"symbol={symbol}"
    return f"{note} [{tag}]" if note else tag


async def commit_batch(
    conn: asyncpg.Connection,
    account_id: UUID,
    batch: ImportBatch,
    source: str = "csv",
) -> CommitResult:
    """Residual limitation, written down rather than left to be discovered: the
    occurrence index (see below) only disambiguates repeats that appear
    together in the SAME call to commit_batch. Two genuinely distinct same-day
    identical trades split across two separate exports that are never both
    present in one batch — e.g. one arrives today, its identical twin arrives
    in next month's export — will still collapse onto the same hash and one
    will be deduped away. Fixing that needs a venue-supplied ordinal (an
    intra-day sequence number), which no importer here has; there is no way to
    distinguish "the same trade, re-exported" from "a different trade that
    happens to look identical" once the rows are in different batches. This is
    far narrower than the same-batch collision round 1 fixed — most exports of
    the same account naturally overlap enough that a repeat and its earlier
    occurrence end up together — but it is not zero, so it is recorded here
    rather than left implicit.
    """
    fills: list[Fill] = []

    # Fidelity (and some other venues) export a date with no time component, so
    # two genuinely distinct same-day trades with identical symbol/side/qty/price
    # would otherwise hash identically and one would be silently deduped away as
    # a "duplicate" of the other — a real trade lost, not a benign re-import. The
    # occurrence counter below breaks that tie while staying stable across
    # re-imports: the same batch, walked in the same order, always assigns the
    # same indices, so a genuine re-import still dedupes to zero.
    #
    # The key is built from the SAME normalized fields content_hash itself
    # hashes (symbol upper-cased, side lower-cased) rather than the raw
    # CanonicalFill values. content_hash normalizes internally, so two rows
    # differing only in symbol casing ("SPY" vs "Spy") already hash to the same
    # payload; if the occurrence key used raw casing instead, those two rows
    # would each get occurrence 0 (distinct keys, "SPY" != "Spy") and therefore
    # the same final hash by coincidence for anything else that also matched —
    # or worse, diverge from what content_hash considers "the same shape" in
    # the opposite direction. Keeping the two normalizations identical is what
    # makes "same occurrence key" and "same hash inputs" the same statement.
    fill_occurrence: dict[tuple, int] = defaultdict(int)

    for cf in batch.fills:
        instrument_id = await upsert_instrument(conn, cf.instrument)

        # A fill lacking a venue_fill_id has nothing else to dedupe on —
        # without a content_hash here, insert_fills' ON CONFLICT can never
        # match it and re-importing the same export duplicates every row. Only
        # these rows draw an occurrence index: a row that already dedupes on
        # its own venue_fill_id must not also consume a slot, or its presence
        # (and its position in the batch) would shift the index assigned to an
        # unrelated hash-carrying row with the same shape, changing that row's
        # hash on a reordered re-import and producing a phantom duplicate.
        if cf.venue_fill_id:
            fill_hash = None
        else:
            key = (
                cf.executed_at,
                cf.instrument.symbol.upper(),
                cf.side.value.lower(),
                cf.quantity,
                cf.price,
            )
            occurrence = fill_occurrence[key]
            fill_occurrence[key] += 1
            fill_hash = content_hash(
                account_id,
                cf.executed_at,
                cf.instrument.symbol,
                cf.side.value,
                cf.quantity,
                cf.price,
                occurrence,
            )

        fills.append(
            Fill(
                id=uuid4(),
                account_id=account_id,
                instrument_id=instrument_id,
                executed_at=cf.executed_at,
                side=cf.side,
                quantity=cf.quantity,
                price=cf.price,
                fee=cf.fee,
                fee_currency=cf.fee_currency,
                source=FillSource(source),
                venue_order_id=cf.venue_order_id,
                venue_fill_id=cf.venue_fill_id,
                content_hash=fill_hash,
                is_estimated=False,
                funding_source=cf.funding_source,
            )
        )

    fill_result = await insert_fills(conn, fills)

    cash_inserted = 0
    cash_occurrence: dict[tuple, int] = defaultdict(int)
    for c in batch.cash:
        instrument_id = None
        note = c.note
        if c.symbol:
            matches = await _find_instrument_by_symbol(conn, account_id, c.symbol)
            if len(matches) == 1:
                instrument_id = matches[0]
            else:
                # Zero matches (no such instrument traded in this account yet) or
                # more than one (ambiguous — e.g. an equity and a crypto asset
                # sharing a symbol) both leave instrument_id NULL. Never guess;
                # preserve the symbol instead so a human can resolve it later.
                note = _append_symbol_note(note, c.symbol)

        cash_key = (c.occurred_at, c.symbol or c.kind, c.kind, c.amount)
        cash_occ = cash_occurrence[cash_key]
        cash_occurrence[cash_key] += 1

        row = await conn.fetchval(
            """
            INSERT INTO cash_movement (account_id, occurred_at, kind, amount,
                                       currency, instrument_id, venue_ref,
                                       content_hash, note)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            account_id,
            c.occurred_at,
            c.kind,
            c.amount,
            c.currency,
            instrument_id,
            c.venue_ref,
            content_hash(
                account_id,
                c.occurred_at,
                c.symbol or c.kind,
                c.kind,
                c.amount,
                Decimal(0),
                cash_occ,
            ),
            note,
        )
        if row is not None:
            cash_inserted += 1

    return CommitResult(
        fills_inserted=fill_result.inserted,
        fills_skipped=fill_result.skipped,
        cash_inserted=cash_inserted,
        warnings=batch.warnings,
    )
