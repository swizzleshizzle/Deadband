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
    fills: list[Fill] = []

    # Fidelity (and some other venues) export a date with no time component, so
    # two genuinely distinct same-day trades with identical symbol/side/qty/price
    # would otherwise hash identically and one would be silently deduped away as
    # a "duplicate" of the other — a real trade lost, not a benign re-import. The
    # occurrence counter below breaks that tie while staying stable across
    # re-imports: the same batch, walked in the same order, always assigns the
    # same indices, so a genuine re-import still dedupes to zero.
    fill_occurrence: dict[tuple, int] = defaultdict(int)

    for cf in batch.fills:
        instrument_id = await upsert_instrument(conn, cf.instrument)
        key = (cf.executed_at, cf.instrument.symbol, cf.side.value, cf.quantity, cf.price)
        occurrence = fill_occurrence[key]
        fill_occurrence[key] += 1
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
                # A fill lacking a venue_fill_id has nothing else to dedupe on —
                # without a content_hash here, insert_fills' ON CONFLICT can never
                # match it and re-importing the same export duplicates every row.
                content_hash=(
                    None
                    if cf.venue_fill_id
                    else content_hash(
                        account_id,
                        cf.executed_at,
                        cf.instrument.symbol,
                        cf.side.value,
                        cf.quantity,
                        cf.price,
                        occurrence,
                    )
                ),
                is_estimated=False,
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
