"""What an import batch routes to, what refuses it, and what a commit wrote.

Every decision here used to live inline in `cli.py:_preview_or_commit`, where
each one was expressed as a `print` plus an exit code. Nothing but a terminal
can consume that shape: the HTTP import wizard would have had to RESTATE every
routing and refusal rule, and a second statement of a rule is a second place
for it to drift out of agreement with the first -- exactly the CLI/HTTP
divergence that produced a Critical defect on the previous plan. These
functions return the decisions instead; `cli.py` renders them and maps them to
its exit codes, and the API renders the same values its own way.

NOTHING IN THIS MODULE PRINTS. A refusal is an exception carrying the evidence
a caller needs to explain it, never an exit code an HTTP handler cannot act on.

This module never opens a connection either. `preview` takes `conn` as an
optional argument and `commit` requires one; neither imports `create_pool`, so
"preview opens no database connection" is structural here rather than a rule
someone has to remember (cli.py pins it from the outside as well --
tests/test_cli.py::test_preview_import_never_opens_a_database_connection).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from db.accounts import get_account
from db.importing import DuplicateReport, commit_batch, probe_duplicates, route_batch
from db.trades import regroup_account
from importers.base import ImportBatch
from ledger.grouping import TransferError


class ImportRefused(Exception):
    """Base for every refusal below. Each one is raised BEFORE anything is
    written (or from inside the commit's own transaction, which then rolls
    back), so "refused" always means "the ledger is exactly as it was".

    Partial commits are not acceptable -- a silently-skipped account looks
    like a successful import -- so each of these refuses the WHOLE batch.
    """


class UnroutableRowsError(ImportRefused):
    """Rows with no external_ref at all (a venue whose export carries no
    per-row account number, e.g. Coinbase) are never routed by route_batch --
    they need an explicit destination. Committing without one would silently
    drop every such row.

    Depends only on the parsed batch, not on the database, which is why it is
    raised before any query runs and why cli.py can make the same call before
    it even opens a pool.
    """


class BlockingRowsError(ImportRefused):
    """`ImportBatch.blocking` reasons that survived routing.

    See ImportBatch.blocking's docstring for why this is neither "block on
    every unmapped row" nor "block on none": an unmapped row that also carries
    money is exactly the shape of the defect this refusal exists for --
    committing everything else and dropping that row's money would look like a
    successful import.
    """

    def __init__(self, reasons: tuple[tuple[str | None, str], ...]) -> None:
        super().__init__(f"{len(reasons)} row(s) block this commit")
        self.reasons = reasons


class AccountNotFoundError(ImportRefused):
    """The explicit destination for unrouted rows does not exist."""

    def __init__(self, account_id: UUID) -> None:
        super().__init__(f"no account with id {account_id}")
        self.account_id = account_id


class AccountVenueMismatchError(ImportRefused):
    """A file parsed by one venue's importer must never be committed to an
    account belonging to a different venue -- that would permanently attribute
    (e.g.) Coinbase fills to a Fidelity account, with no CLI path to undo it.
    """

    def __init__(self, account_id: UUID, account_venue: str, batch_venue: str) -> None:
        super().__init__(
            f"account {account_id} is a {account_venue!r} account; "
            f"refusing to commit a {batch_venue!r} import to it"
        )
        self.account_id = account_id
        self.account_venue = account_venue
        self.batch_venue = batch_venue


class UnknownRefsError(ImportRefused):
    """A row routes to an account ref that has no matching account AND carries
    money (RoutingPlan.unknown_refs -- the money-scoped set).

    `refs` is deliberately NOT RoutingPlan.reported_unknown_refs, which is a
    strict superset kept for reporting only. Refusing on the superset
    reintroduces the trap A2-6 exists to avoid, where one stray boilerplate
    row attributed to an unregistered account refuses every import
    permanently. `routing` carries the report the caller had already earned by
    the time this was raised, so a refusal can still show where everything
    else would have gone.
    """

    def __init__(self, refs: tuple[str, ...], routing: RoutingReport) -> None:
        super().__init__(f"unknown account ref(s): {', '.join(refs)}")
        self.refs = refs
        self.routing = routing


class MixedDedupePathsError(ImportRefused):
    """I3: this batch's fills carry a venue fill id, but a target account
    already holds fill(s) keyed on content_hash instead.

    The two partial unique indexes are disjoint BY CONSTRUCTION:
    fill_venue_id_uniq is WHERE venue_fill_id IS NOT NULL,
    fill_content_hash_uniq is WHERE content_hash IS NOT NULL, and
    db/importing.py gives a fill exactly one of the two keys, never both. A
    pre-cut-over CSV Coinbase fill therefore has (venue_fill_id NULL,
    content_hash SET); the SAME trade arriving via `sync` has (venue_fill_id
    SET, content_hash NULL). Neither index can see the other. Both rows land,
    both feed regroup_account, and the account's position and realized P&L
    silently DOUBLE. Nothing else in the system would notice.

    `accounts` is (account_id, count of existing content_hash-keyed fills).
    """

    def __init__(
        self, accounts: tuple[tuple[UUID, int], ...], routing: RoutingReport
    ) -> None:
        super().__init__(f"{len(accounts)} account(s) already hold content_hash-keyed fills")
        self.accounts = accounts
        self.routing = routing


class TransferRefused(ImportRefused):
    """An outbound transfer this batch carries cannot be honoured by the
    account's ledger (importing years out of order, typically).

    Raised from inside the commit's own transaction, which rolls back on the
    way out, so nothing was written. `cause` is the ledger's own TransferError
    -- kept rather than reformatted, since its message names the instrument
    and quantity involved. Wrapped at all so that this refusal carries
    `routing` like every other one: a caller reporting the failure can still
    say where the rest of the batch would have gone.
    """

    def __init__(self, cause: TransferError, routing: RoutingReport) -> None:
        super().__init__(str(cause))
        self.cause = cause
        self.routing = routing


@dataclass(frozen=True, slots=True)
class RoutingReport:
    """Where this batch's rows landed, in the order a caller should report it.

    Every ref in the batch appears in exactly ONE of these four fields, so a
    renderer can walk them in order without deduplicating and without any
    account going unmentioned.
    """

    # (account_id, row count) per destination, in routing order. A count of 0
    # is possible and meaningful -- it says the account was reached and had
    # nothing committable.
    mapped: tuple[tuple[UUID, int], ...]
    # Refs whose account is registered ignore_on_import. These routed
    # SUCCESSFULLY: their rows are dropped on purpose, and this is NOT a
    # failure path. Without it a deliberately-excluded account (a retirement
    # plan with no instrument identity, say) would refuse every import of the
    # file it appears in, permanently.
    ignored_refs: tuple[str, ...]
    # F: RoutingPlan.reported_unknown_refs -- EVERY ref with no matching
    # account, including one that appears only in batch.refs_seen (an account
    # whose rows are all unmapped and non-financial). A strict superset of the
    # money-scoped set that drives UnknownRefsError. REPORT ONLY: using it to
    # refuse is the over-block trap described on UnknownRefsError.
    unknown_refs: tuple[str, ...]
    # Registered accounts seen in the raw rows that produced no fill, cash
    # movement or blocking reason at all -- route_batch never sees them, since
    # it only classifies refs reachable from those. Reported explicitly rather
    # than left to vanish from the report while the commit still claims
    # success.
    unclassified_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreviewReport:
    """Everything a caller can say about a batch WITHOUT writing anything.

    With `conn=None` the routing fields are all empty and `blocking` is the
    batch's own unfiltered list -- not because nothing is wrong, but because
    nothing about accounts can be known without a query. See `preview`.
    """

    fill_count: int
    cash_count: int
    transfer_count: int
    warnings: tuple[str, ...]
    unmapped_row_count: int
    refs_seen: tuple[str, ...]
    # (ref, row count) for every ref in refs_seen, in that order. Derived from
    # refs_seen rather than from fills/cash: an account whose rows are
    # ENTIRELY unmapped contributes nothing to either, so a report built from
    # those would never name it -- exactly the account most in need of being
    # flagged. Its count is 0, and that 0 is itself the signal that something
    # is wrong with that account.
    rows_per_ref: tuple[tuple[str, int], ...]
    # REPORT ONLY -- RoutingPlan.reported_unknown_refs. See RoutingReport.
    unknown_refs: tuple[str, ...]
    # The money-scoped subset (RoutingPlan.unknown_refs): the ONLY field in
    # this report that may drive a refusal. Kept separate rather than folded
    # into unknown_refs above so that a caller reaching for "what should stop
    # this import" cannot pick up the reporting superset by accident.
    unknown_money_refs: tuple[str, ...]
    ignored_refs: tuple[str, ...]
    # Blocking reasons with any ref belonging to an ignore_on_import account
    # already dropped -- but only when a connection was supplied, since
    # ignore status is a database fact. Unfiltered otherwise.
    blocking: tuple[tuple[str | None, str], ...]
    # One summary line per proposal in batch.corporate_actions, for a caller
    # with no renderer of its own. cli.py deliberately ignores this and prints
    # its own far richer section (spec Sec8) from batch.corporate_actions
    # directly; cash-in-lieu is excluded here for the same reason it is kept
    # out of corporate_actions (D6) -- it is recognised, never applied, and
    # must never be mistaken for something `corporate add` can record.
    corporate_proposals: tuple[str, ...]
    # None means the probe DID NOT RUN, never "no duplicates": with no
    # connection there is nothing to probe, and with unknown money-carrying
    # refs (or unrouted rows and no destination) any count it produced would
    # silently omit the rows it could not reach -- indistinguishable from "this
    # file is clean" even though it was never checked (finding C).
    duplicates: DuplicateReport | None
    # Rows exist that carry no account ref to route by, and no explicit
    # account was given. Depends only on the parsed batch, so a caller can act
    # on it before spending a connection.
    needs_account: bool


@dataclass(frozen=True, slots=True)
class ImportCommitReport:
    """What a successful commit actually wrote."""

    fills_inserted: int
    fills_skipped: int
    cash_inserted: int
    transfers_inserted: int
    transfers_skipped: int
    trades_regrouped: int
    warnings: tuple[str, ...]
    # Surfaced from `routing` because it is the one routing outcome that is
    # part of SUCCESS: these accounts' rows were dropped deliberately, and a
    # caller reporting a clean import still has to say so.
    ignored_refs: tuple[str, ...]
    routing: RoutingReport


def _corporate_proposal_lines(batch: ImportBatch) -> tuple[str, ...]:
    return tuple(
        f"{p.kind} ex {p.ex_date.isoformat()} -- {p.description}"
        for p in batch.corporate_actions
    )


def _rows_per_ref(batch: ImportBatch) -> tuple[tuple[str, int], ...]:
    return tuple(
        (
            ref,
            sum(1 for f in batch.fills if f.external_ref == ref)
            + sum(1 for c in batch.cash if c.external_ref == ref),
        )
        for ref in batch.refs_seen
    )


def _unclassified_refs(batch: ImportBatch, plan_refs: set[str]) -> tuple[str, ...]:
    """Refs seen in the raw rows that routing never reached.

    route_batch only ever sees refs that appear on a fill, cash movement or
    blocking reason -- a REGISTERED account whose rows all warned but produced
    none of those (an edge route_batch's own classification doesn't reach) can
    still fall out here. batch.refs_seen is the only place such an account is
    visible. Refs already reported as ignored or unknown are excluded so each
    ref is reported exactly once.
    """
    covered = {f.external_ref for f in batch.fills if f.external_ref} | {
        c.external_ref for c in batch.cash if c.external_ref
    }
    return tuple(sorted(set(batch.refs_seen) - covered - plan_refs))


async def preview(
    batch: ImportBatch,
    *,
    venue: str,
    conn: asyncpg.Connection | None = None,
    account_id: UUID | None = None,
) -> PreviewReport:
    """Describe a batch without writing anything.

    Always async and always accepting `conn=None`, deliberately: the
    connection-free guarantee is about never OPENING a connection, not about
    being synchronous, and an async function that simply never touches `conn`
    honours it exactly. One signature beats a sync/async pair that every
    caller would have to choose between.

    `conn=None` is the default path (`deadband import` with no flags) and
    answers only what the parsed file can answer. Supplying a connection is
    the explicit opt-in that also routes the batch and probes for duplicates
    -- READ-ONLY in both cases; route_batch and probe_duplicates issue nothing
    but SELECTs, so even the opt-in path cannot write.
    """
    unrouted = batch.unrouted()
    needs_account = unrouted.has_rows() and account_id is None
    common = {
        "fill_count": len(batch.fills),
        "cash_count": len(batch.cash),
        "transfer_count": len(batch.transfers),
        "warnings": batch.warnings,
        "unmapped_row_count": len(batch.unmapped_rows),
        "refs_seen": batch.refs_seen,
        "rows_per_ref": _rows_per_ref(batch),
        "corporate_proposals": _corporate_proposal_lines(batch),
        "needs_account": needs_account,
    }

    if conn is None:
        return PreviewReport(
            unknown_refs=(),
            unknown_money_refs=(),
            ignored_refs=(),
            # Unfiltered: ignore_on_import status is a database fact, so with
            # no connection there is no honest way to drop the reasons that
            # belong to an ignored account. Reported as-is rather than as ()
            # so a caller is never told "nothing blocks this" on the strength
            # of a question that was never asked.
            blocking=batch.blocking,
            duplicates=None,
            **common,
        )

    plan = await route_batch(conn, venue, batch)
    # C1: drop any reason whose row belongs to an account registered
    # ignore_on_import -- otherwise a money-carrying unmapped row on an
    # account the user has explicitly said to skip refuses the ENTIRE import,
    # permanently, with no escape.
    effective_blocking = tuple(
        (ref, msg) for ref, msg in batch.blocking if ref not in plan.ignored_refs
    )

    duplicates: DuplicateReport | None = None
    if not plan.unknown_refs and not needs_account:
        # Both conditions are about COMPLETENESS, not about refusing: a row
        # that routes to an unknown account, or that has no ref and no
        # explicit destination, never lands in `targets` and so is never
        # probed. Reporting a count that silently omitted it would be
        # indistinguishable from "this file has no duplicates" even though it
        # was never checked, which is worse than reporting nothing at all.
        targets = dict(plan.by_account)
        if unrouted.has_rows() and account_id is not None:
            targets[account_id] = targets.get(account_id, ImportBatch()).merge_rows(unrouted)
        fill_dupes = cash_dupes = transfer_dupes = 0
        for target_id, sub_batch in targets.items():
            probed = await probe_duplicates(conn, target_id, sub_batch)
            fill_dupes += probed.fill_dupes
            cash_dupes += probed.cash_dupes
            transfer_dupes += probed.transfer_dupes
        duplicates = DuplicateReport(
            fill_dupes=fill_dupes, cash_dupes=cash_dupes, transfer_dupes=transfer_dupes
        )

    return PreviewReport(
        unknown_refs=plan.reported_unknown_refs,
        unknown_money_refs=plan.unknown_refs,
        ignored_refs=plan.ignored_refs,
        blocking=effective_blocking,
        duplicates=duplicates,
        **common,
    )


async def commit(
    conn: asyncpg.Connection,
    *,
    venue: str,
    batch: ImportBatch,
    account_id: UUID | None,
    source: str,
    regroup: Callable[[asyncpg.Connection, UUID], Awaitable[int]] = regroup_account,
) -> ImportCommitReport:
    """Route a batch and write it, or refuse the whole thing and write nothing.

    `venue` is always an importer's `.account_venue` (see the Importer
    Protocol in importers/base.py), never its `.venue` identity -- those two
    differ for `coinbase-api`, whose own identity is a TRANSPORT ("coinbase-api"
    vs. the CSV importer) rather than the venue accounts are registered under
    (every real account is "coinbase"). This function only ever needs "which
    registered account venue does this batch route/match against", so taking
    `account_venue` directly makes it structurally impossible for a caller to
    pass the wrong one -- the shape of the bug that motivated adding
    `account_venue` at all.

    `source` is the provenance recorded on every fill this call writes
    (`fill.source`): "csv" from a file, "api" from a venue endpoint. It is
    keyword-only and has NO default on purpose. commit_batch's own
    `source: str = "csv"` default is what made every API-synced fill claim it
    came from a CSV (I2) -- silently, since nothing downstream reads the
    column yet. A default here would let the next entry point reintroduce the
    same lie by omission; requiring the argument makes the caller state it.

    Every refusal below raises before `conn.transaction()` opens (route_batch
    and get_account issue only SELECTs), so a refused commit has written
    nothing at all.

    `regroup` is a seam, not a configuration knob: production callers never
    pass it. tests/db/test_cli.py::test_a_crash_during_regroup_leaves_no_fills_
    through_the_real_cli proves the insert and the regroup share ONE
    transaction by patching cli.regroup_account to raise, and that proof has
    to point at the function this call actually invokes.
    """
    unrouted = batch.unrouted()
    if unrouted.has_rows() and account_id is None:
        raise UnroutableRowsError(
            "this batch has row(s) with no account ref to route by "
            "(this venue's export carries no per-row account number)"
        )

    plan = await route_batch(conn, venue, batch)

    # C1: this check must run AFTER route_batch, not before, and must drop any
    # reason whose row belongs to an account registered ignore_on_import --
    # see BlockingRowsError and PreviewReport.blocking. route_batch issues
    # only SELECTs, so this still refuses before anything is written.
    effective_blocking = tuple(
        (ref, msg) for ref, msg in batch.blocking if ref not in plan.ignored_refs
    )
    if effective_blocking:
        raise BlockingRowsError(effective_blocking)

    targets: dict[UUID, ImportBatch] = dict(plan.by_account)
    if unrouted.has_rows():
        assert account_id is not None  # guaranteed by the UnroutableRowsError above
        account = await get_account(conn, account_id)
        if account is None:
            raise AccountNotFoundError(account_id)
        if account["venue"] != venue:
            raise AccountVenueMismatchError(account_id, account["venue"], venue)
        targets[account_id] = targets.get(account_id, ImportBatch()).merge_rows(unrouted)

    routing = RoutingReport(
        mapped=tuple(
            (target_id, len(sub_batch.fills) + len(sub_batch.cash))
            for target_id, sub_batch in targets.items()
        ),
        ignored_refs=plan.ignored_refs,
        unknown_refs=plan.reported_unknown_refs,
        unclassified_refs=_unclassified_refs(
            batch, set(plan.ignored_refs) | set(plan.reported_unknown_refs)
        ),
    )

    # Refuse the WHOLE batch on a money-carrying unknown ref, and write
    # nothing at all -- a partial commit that silently skipped one account
    # would look like a successful import. Keyed on plan.unknown_refs alone;
    # the fuller reported set above changes what is REPORTED, never what
    # refuses (see UnknownRefsError).
    if plan.unknown_refs:
        raise UnknownRefsError(plan.unknown_refs, routing)

    # I3: refuse a batch that would make an account's fills reachable by two
    # mutually-blind dedupe paths -- see MixedDedupePathsError for why the two
    # indexes cannot see each other. Deliberately generic, not
    # Coinbase-specific: any venue that ever cuts a CSV path over to an API
    # one has this exact hazard, and the check costs one SELECT count(*) per
    # target account, on the commit path only. It is unreachable from preview,
    # which opens no connection at all.
    mixed: list[tuple[UUID, int]] = []
    for target_id, sub_batch in targets.items():
        if not any(f.venue_fill_id for f in sub_batch.fills):
            continue
        legacy = await conn.fetchval(
            """
            SELECT count(*) FROM fill
             WHERE account_id = $1
               AND content_hash IS NOT NULL
               AND venue_fill_id IS NULL
            """,
            target_id,
        )
        if legacy:
            mixed.append((target_id, legacy))
    if mixed:
        raise MixedDedupePathsError(tuple(mixed), routing)

    fills_inserted = fills_skipped = cash_inserted = trades_regrouped = 0
    transfers_inserted = transfers_skipped = 0
    warnings: list[str] = []
    # One transaction across every target account's inserts AND its regroup:
    # a fill must never be inserted without its corresponding trade
    # regrouping, or vice versa, and a second account's failure must not leave
    # the first account's rows behind.
    try:
        async with conn.transaction():
            for target_id, sub_batch in targets.items():
                result = await commit_batch(conn, target_id, sub_batch, source=source)
                fills_inserted += result.fills_inserted
                fills_skipped += result.fills_skipped
                cash_inserted += result.cash_inserted
                transfers_inserted += result.transfers_inserted
                transfers_skipped += result.transfers_skipped
                warnings.extend(result.warnings)
                trades_regrouped += await regroup(conn, target_id)
    except TransferError as exc:
        # `from exc` (ruff B904): without it the ledger's own TransferError is
        # reported as "During handling of the above exception, another
        # exception occurred", which reads like a bug in this handler rather
        # than the cause it actually is.
        raise TransferRefused(exc, routing) from exc

    return ImportCommitReport(
        fills_inserted=fills_inserted,
        fills_skipped=fills_skipped,
        cash_inserted=cash_inserted,
        transfers_inserted=transfers_inserted,
        transfers_skipped=transfers_skipped,
        trades_regrouped=trades_regrouped,
        warnings=tuple(warnings),
        ignored_refs=plan.ignored_refs,
        routing=routing,
    )
