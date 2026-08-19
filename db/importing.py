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
from db.transfers import insert_transfers
from db.instruments import upsert_instrument
from importers.base import CanonicalCash, CanonicalFill, CanonicalTransfer, ImportBatch, content_hash
from ledger.types import AssetTransfer, Fill, FillSource


@dataclass(frozen=True, slots=True)
class CommitResult:
    fills_inserted: int
    fills_skipped: int
    cash_inserted: int
    warnings: tuple[str, ...]
    transfers_inserted: int = 0
    transfers_skipped: int = 0


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    """Result of probe_duplicates -- see its docstring below."""

    fill_dupes: int
    cash_dupes: int


def _fill_dedupe_keys(
    account_id: UUID, fills: tuple[CanonicalFill, ...]
) -> list[tuple[str | None, str | None]]:
    """The dedupe key for each fill, in order: (venue_fill_id, None) for a row
    the venue itself identifies, or (None, content_hash) for one that must
    dedupe on its computed shape -- exactly the choice commit_batch makes
    below. Factored out so commit_batch and probe_duplicates share ONE
    computation of "what counts as a duplicate" rather than maintaining two
    hashing schemes that could silently drift apart; see the occurrence-index
    commentary on commit_batch for why the logic here can't be simplified
    further.
    """
    fill_occurrence: dict[tuple, int] = defaultdict(int)
    keys: list[tuple[str | None, str | None]] = []
    for cf in fills:
        if cf.venue_fill_id:
            keys.append((cf.venue_fill_id, None))
            continue
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
        keys.append((None, fill_hash))
    return keys


def _cash_dedupe_hashes(account_id: UUID, cash: tuple[CanonicalCash, ...]) -> list[str]:
    """The content_hash for each cash movement, in order -- exactly what
    commit_batch computes below. See _fill_dedupe_keys' docstring for why
    this is factored out rather than duplicated."""
    cash_occurrence: dict[tuple, int] = defaultdict(int)
    hashes: list[str] = []
    for c in cash:
        cash_key = (c.occurred_at, c.symbol or c.kind, c.kind, c.amount)
        cash_occ = cash_occurrence[cash_key]
        cash_occurrence[cash_key] += 1
        hashes.append(
            content_hash(
                account_id,
                c.occurred_at,
                c.symbol or c.kind,
                c.kind,
                c.amount,
                Decimal(0),
                cash_occ,
            )
        )
    return hashes


def _transfer_dedupe_hashes(
    account_id: UUID, transfers: tuple[CanonicalTransfer, ...]
) -> list[str]:
    """The content_hash for each transfer, in order. Same occurrence-index
    tie-break as fills and cash: exports carry date-only timestamps, so two
    genuinely distinct same-day identical transfers would otherwise collapse
    onto one hash and one would be silently deduped away."""
    occurrence: dict[tuple, int] = defaultdict(int)
    hashes: list[str] = []
    for t in transfers:
        key = (t.occurred_at, t.instrument.symbol.upper(), t.quantity)
        occ = occurrence[key]
        occurrence[key] += 1
        hashes.append(
            content_hash(
                account_id,
                t.occurred_at,
                t.instrument.symbol,
                "transfer_out",
                t.quantity,
                t.market_value if t.market_value is not None else Decimal(0),
                occ,
            )
        )
    return hashes


@dataclass(frozen=True, slots=True)
class RoutingPlan:
    by_account: dict[UUID, ImportBatch]
    # Refs with no matching account that ALSO carry money (a fill, a cash
    # movement, or a blocking reason). This is what cli.py refuses the whole
    # commit on -- see route_batch's docstring for why it stays narrower than
    # reported_unknown_refs below.
    unknown_refs: tuple[str, ...]
    ignored_refs: tuple[str, ...]
    # F: EVERY ref seen anywhere in the batch (money-carrying refs union
    # batch.refs_seen) that has no matching account -- a strict superset of
    # unknown_refs. Exists for REPORTING only: a caller (cli.py) can now say
    # "unknown" about an unregistered account whose rows are ALL unmapped and
    # non-financial, instead of that account being invisible to
    # classification entirely. Must NEVER be used to decide refusal -- that
    # would reintroduce the over-block trap A2-6 exists to avoid, where one
    # stray boilerplate row attributed to an unregistered account refuses
    # every import permanently. unknown_refs (money-scoped) is the only field
    # that may drive a refusal decision.
    reported_unknown_refs: tuple[str, ...] = ()


async def route_batch(conn: asyncpg.Connection, venue: str, batch: ImportBatch) -> RoutingPlan:
    """Split a parsed batch by account, one export can span several.

    Each row's external_ref (the venue's own account NUMBER -- see
    importers/fidelity.py) is matched against account.external_ref within
    the given venue:

    - No matching account -> the ref goes into `unknown_refs` (if it also
      carries money) and always into `reported_unknown_refs`. Never silently
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
    # C1: refs must also be drawn from batch.blocking, not only fills/cash.
    # An account whose rows are ENTIRELY blocking (e.g. a retirement plan
    # with no instrument identity -- zero symbols, zero prices, every row
    # unmapped but money-carrying) contributes nothing to fills or cash, so
    # its ref would otherwise never reach this query at all -- meaning it
    # could never be recognised as ignore_on_import here, and the caller's
    # "drop blocking reasons whose ref is ignored" check (see cli.py) would
    # have nothing to drop against. Rows this pulls in that have no other
    # fills/cash of their own simply route to an empty ImportBatch below,
    # which is harmless. This is the set that may drive REFUSAL (see
    # unknown_refs above) -- it must stay scoped to money-carrying refs only.
    money_refs = (
        {f.external_ref for f in batch.fills if f.external_ref is not None}
        | {c.external_ref for c in batch.cash if c.external_ref is not None}
        | {t.external_ref for t in batch.transfers if t.external_ref is not None}
        | {ref for ref, _msg in batch.blocking if ref is not None}
    )

    # F: batch.refs_seen carries every account ref seen in the RAW rows,
    # independent of whether the row went on to become a fill, a cash
    # movement, or a blocking reason (see ImportBatch.refs_seen's docstring).
    # An account whose rows are ALL unmapped and non-financial contributes
    # to NONE of fills/cash/blocking, so money_refs alone would never see it
    # -- it would be invisible to classification entirely, neither unknown
    # nor ignored, just absent. Included here so it CAN be classified, for
    # reporting -- but deliberately kept separate from money_refs, which is
    # the only set allowed to drive refusal (see RoutingPlan.unknown_refs vs
    # .reported_unknown_refs).
    all_refs = sorted(money_refs | set(batch.refs_seen))

    unknown_refs: list[str] = []
    reported_unknown_refs: list[str] = []
    ignored_refs: list[str] = []
    # ref -> account id, only for refs that route successfully (known,
    # not ignored). Refs with no entry here are unknown or ignored -- both
    # already recorded above -- or simply have no rows (unreachable, since
    # `refs` is built only from refs that actually appear in the batch).
    routable: dict[str, UUID] = {}

    if all_refs:
        rows = await conn.fetch(
            """
            SELECT id, external_ref, ignore_on_import
              FROM account
             WHERE venue = $1 AND external_ref = ANY($2::text[])
            """,
            venue,
            all_refs,
        )
        # UNIQUE (venue, external_ref) means at most one row per ref here.
        by_ref = {r["external_ref"]: r for r in rows}

        for ref in all_refs:
            row = by_ref.get(ref)
            if row is None:
                reported_unknown_refs.append(ref)
                if ref in money_refs:
                    unknown_refs.append(ref)
            elif row["ignore_on_import"]:
                ignored_refs.append(ref)
            else:
                routable[ref] = row["id"]

    fills_by_account: dict[UUID, list] = defaultdict(list)
    cash_by_account: dict[UUID, list] = defaultdict(list)
    transfers_by_account: dict[UUID, list] = defaultdict(list)

    for f in batch.fills:
        account_id = routable.get(f.external_ref) if f.external_ref is not None else None
        if account_id is not None:
            fills_by_account[account_id].append(f)

    for c in batch.cash:
        account_id = routable.get(c.external_ref) if c.external_ref is not None else None
        if account_id is not None:
            cash_by_account[account_id].append(c)

    for t in batch.transfers:
        account_id = routable.get(t.external_ref) if t.external_ref is not None else None
        if account_id is not None:
            transfers_by_account[account_id].append(t)

    by_account = {
        account_id: ImportBatch(
            fills=tuple(fills_by_account.get(account_id, ())),
            cash=tuple(cash_by_account.get(account_id, ())),
            transfers=tuple(transfers_by_account.get(account_id, ())),
        )
        for account_id in routable.values()
    }

    return RoutingPlan(
        by_account=by_account,
        unknown_refs=tuple(unknown_refs),
        ignored_refs=tuple(ignored_refs),
        reported_unknown_refs=tuple(reported_unknown_refs),
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
    source: str,
) -> CommitResult:
    """`source` has NO default (I2). It used to default to "csv", and cli.py's
    shared commit path never overrode it -- so every fill written by `deadband
    sync coinbase` was recorded as CSV-provenance, which is the one thing
    `fill.source` exists to be able to distinguish. Every caller now states it;
    a wrong value has to be typed, rather than inherited from a default that
    was only ever right for the first caller written.

    Residual limitation, written down rather than left to be discovered: the
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
    # occurrence counter (inside _fill_dedupe_keys) breaks that tie while
    # staying stable across re-imports: the same batch, walked in the same
    # order, always assigns the same indices, so a genuine re-import still
    # dedupes to zero.
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
    fill_keys = _fill_dedupe_keys(account_id, batch.fills)

    for cf, (_venue_fill_id, fill_hash) in zip(batch.fills, fill_keys, strict=True):
        instrument_id = await upsert_instrument(conn, cf.instrument)

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
    cash_hashes = _cash_dedupe_hashes(account_id, batch.cash)
    for c, chash in zip(batch.cash, cash_hashes, strict=True):
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
            chash,
            note,
        )
        if row is not None:
            cash_inserted += 1

    transfer_rows: list[AssetTransfer] = []
    for t, thash in zip(
        batch.transfers, _transfer_dedupe_hashes(account_id, batch.transfers), strict=True
    ):
        transfer_rows.append(
            AssetTransfer(
                id=uuid4(),
                account_id=account_id,
                instrument_id=await upsert_instrument(conn, t.instrument),
                occurred_at=t.occurred_at,
                quantity=t.quantity,
                market_value=t.market_value,
                venue_ref=t.venue_ref,
                content_hash=thash,
                note=t.note,
            )
        )
    transfer_result = await insert_transfers(conn, transfer_rows)

    return CommitResult(
        fills_inserted=fill_result.inserted,
        fills_skipped=fill_result.skipped,
        cash_inserted=cash_inserted,
        transfers_inserted=transfer_result.inserted,
        transfers_skipped=transfer_result.skipped,
        warnings=batch.warnings,
    )


async def probe_duplicates(
    conn: asyncpg.Connection, account_id: UUID, batch: ImportBatch
) -> DuplicateReport:
    """Explicit, opt-in, READ-ONLY duplicate check for the preview report
    (spec §7). Preview itself never calls this and stays connection-free by
    default -- see cli.py's --check-duplicates, and
    tests/test_cli.py's test_preview_import_never_opens_a_database_connection,
    which pins that guarantee for the default (no-flag) path. Only the
    explicit flag reaches this function, and this function issues only
    SELECTs: no INSERT, UPDATE, DELETE, or explicit transaction.

    Uses _fill_dedupe_keys/_cash_dedupe_hashes -- the SAME functions
    commit_batch itself uses -- so this can never report a row as new that
    commit_batch would then silently skip as a duplicate, or vice versa. A
    probe with its own, independently-derived notion of "duplicate" would be
    worse than no probe at all: it could tell the user an import is clean
    when commit_batch would actually drop rows, or warn about "duplicates"
    that would actually commit as new rows.
    """
    fill_keys = _fill_dedupe_keys(account_id, batch.fills)
    fill_ids = [venue_fill_id for venue_fill_id, _ in fill_keys if venue_fill_id is not None]
    fill_hashes = [fill_hash for _, fill_hash in fill_keys if fill_hash is not None]

    fill_dupes = await conn.fetchval(
        """
        SELECT count(*) FROM fill
         WHERE account_id = $1
           AND (venue_fill_id = ANY($2::text[]) OR content_hash = ANY($3::text[]))
        """,
        account_id,
        fill_ids,
        fill_hashes,
    )

    cash_hashes = _cash_dedupe_hashes(account_id, batch.cash)
    cash_dupes = await conn.fetchval(
        """
        SELECT count(*) FROM cash_movement
         WHERE account_id = $1 AND content_hash = ANY($2::text[])
        """,
        account_id,
        cash_hashes,
    )

    return DuplicateReport(fill_dupes=fill_dupes, cash_dupes=cash_dupes)
