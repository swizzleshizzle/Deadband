"""Deadband CLI. Preview before commit, always."""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID

from db.accounts import UnknownAccountError, create_account, get_account, list_accounts
from db.cash import MixedCurrencyError, account_cash
from db.importing import commit_batch, probe_duplicates, route_batch
from db.marks import latest_marks, resolve_instrument_by_symbol, set_mark
from db.migrate import apply as apply_migrations
from db.pool import create_pool
from db.positions import open_positions
from db.snapshots import add_snapshot, latest_snapshot
from db.trades import list_trades, regroup_account
from importers.base import ImportBatch
from importers.registry import get_importer, list_importers
from ledger.pnl import unrealized_pnl
from ledger.reconcile import Position, ReconcileVerdict, Snapshot, UnvaluableRef, reconcile
from venues.coinbase_client import CoinbaseCredentials, fetch_all_fills


async def cmd_migrate(_args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            # apply() unconditionally (re-)executes schema.sql, and db/migrations/
            # holds real migrations (starting with 001_a2_ledger_completion.sql),
            # so `applied` can be non-empty for two different reasons: pending
            # migrations on a database that already existed, or the entire schema
            # having just been created on a virgin one. Those are different
            # outcomes and must not share one message. Check for a table
            # schema.sql creates before calling apply(), while it's still
            # meaningful to ask "did this exist already?".
            existed_before = await conn.fetchval(
                "SELECT to_regclass('public.account') IS NOT NULL"
            )
            applied = await apply_migrations(conn)
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    if applied:
        print(f"applied {len(applied)} migration(s):")
        for name in applied:
            print(f"  {name}")
        if existed_before:
            # A migration can add columns but cannot recompute existing rows —
            # migration 001 changes how realized_pnl is derived, so rows written
            # before it keep the old convention until regrouped. A virgin
            # database has no pre-existing rows to be stale, so this warning is
            # scoped to `existed_before` rather than printed unconditionally;
            # doing otherwise on every fresh install would train the operator
            # to ignore it.
            print(
                "\nDerived columns are stale: migration 001 changes how realized_pnl\n"
                "is computed. Run `regroup --account <uuid>` for every account before\n"
                "trusting any P&L figure."
            )
    else:
        # Unreachable with existed_before == False: db/migrations/ always holds
        # at least one migration file (001_a2_ledger_completion.sql onward), so
        # a virgin database's empty schema_migrations table makes `applied`
        # non-empty every time -- the `if applied:` branch above always wins on
        # a fresh install. This branch only ever runs on a database that was
        # already fully up to date.
        print("already up to date")
    return 0


async def cmd_accounts(_args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            for a in await list_accounts(conn):
                print(f"{a['id']}  {a['venue']:<10} {a['name']:<24} {a['external_ref'] or '-'}")
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    return 0


async def cmd_accounts_add(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            account_id = await create_account(
                conn,
                name=args.name,
                venue=args.venue,
                account_type=args.account_type,
                default_intent=args.default_intent,
                external_ref=args.external_ref,
                # getattr, not args.ignore_on_import: a Namespace built by
                # hand (rather than through argparse, which always supplies
                # the store_true default) may omit the attribute entirely.
                ignore_on_import=getattr(args, "ignore_on_import", False),
            )
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    print(account_id)
    return 0


async def cmd_import(args) -> int:
    importer = get_importer(args.venue)
    batch = importer.parse(pathlib.Path(args.file).read_text())
    return await _preview_or_commit(importer.account_venue, batch, args, source="csv")


async def _preview_or_commit(venue: str, batch: ImportBatch, args, *, source: str) -> int:
    """The three-phase body every entry point (`import`, `sync`) shares.

    `source` is the provenance recorded on every fill this call writes
    (`fill.source`): "csv" from `cmd_import`, "api" from `cmd_sync`. It is
    keyword-only and has NO default on purpose. commit_batch's own
    `source: str = "csv"` default is what made every API-synced fill claim it
    came from a CSV (I2) -- silently, since nothing downstream reads the
    column yet. `fill.source` is the only column that can answer "which of my
    Coinbase fills came from the retired CSV path?", which is exactly the
    question the mixed-provenance refusal below and any future reconciliation
    have to ask. A default here would let the next entry point reintroduce the
    same lie by omission; requiring the argument makes the caller state it.

    `venue` is always an importer's `.account_venue` (see the Importer
    Protocol in importers/base.py), never its `.venue` identity -- those
    two differ for `coinbase-api`, whose own identity is a TRANSPORT
    ("coinbase-api" vs. the CSV importer) rather than the venue accounts are
    registered under (every real account is "coinbase"). This function only
    ever needs "which registered account venue does this batch route/match
    against", so taking `account_venue` directly (rather than `venue` plus a
    caller-side literal) makes it structurally impossible for a caller to
    pass the wrong one -- which is exactly the shape of the bug that
    motivated adding `account_venue` at all: `cmd_sync` used to pass the
    literal "coinbase" here itself, alongside `get_importer("coinbase-api")`
    a few lines away, with nothing forcing the two to agree as venues were
    added. Keeping this one function (rather than a second copy of the
    preview/commit body for `sync`) is what the plan's "no second, parallel
    write path" constraint requires.
    """
    print(f"parsed {len(batch.fills)} fills, {len(batch.cash)} cash movements")
    for w in batch.warnings:
        print(f"  warning: {w}", file=sys.stderr)
    if batch.unmapped_rows:
        print(f"  {len(batch.unmapped_rows)} row(s) not mapped", file=sys.stderr)

    if not args.commit:
        # A single export can carry rows for more than one venue account
        # (Fidelity's account-number column, for instance). --commit routes
        # each row to its own account automatically (see db.importing.route_batch);
        # this preview-only warning is the pure, DB-free heads-up for the same
        # situation, since preview deliberately never opens a connection.
        #
        # Derived from batch.refs_seen -- every account ref seen in the RAW
        # rows -- rather than from batch.fills/batch.cash. An account whose
        # rows are ENTIRELY unmapped (every action is one the classifier
        # doesn't know) contributes nothing to fills or cash, so a report
        # built from those would never name it -- exactly the account most in
        # need of being flagged. Printing its row count (0, in that case) is
        # itself the signal that something is wrong with that account.
        if len(batch.refs_seen) > 1:
            for ref in batch.refs_seen:
                n = sum(1 for f in batch.fills if f.external_ref == ref) + sum(
                    1 for c in batch.cash if c.external_ref == ref
                )
                print(f"    {ref}: {n} row(s)", file=sys.stderr)
            print(
                "  warning: this file mixes multiple account refs "
                f"({', '.join(batch.refs_seen)}); --commit routes each row to "
                "its own account automatically",
                file=sys.stderr,
            )

        # --check-duplicates is the one explicit, opt-in exception to preview's
        # no-connection guarantee (see test_preview_import_never_opens_a_
        # database_connection in tests/test_cli.py, which pins the default
        # no-flag path). getattr, not args.check_duplicates: several existing
        # tests build a bare Namespace by hand without this attribute, same
        # reasoning as cmd_accounts_add's ignore_on_import getattr above. Spec
        # §7 requires preview to report what's already present; preview
        # deliberately never opens a connection on its own, so it structurally
        # cannot answer that without an explicit ask.
        if getattr(args, "check_duplicates", False):
            # C: rows with no external_ref (e.g. Coinbase) need --account to
            # be probed at all -- identical to --commit's own `unrouted`
            # check below. Checked here, before any connection is opened,
            # for the same reason --commit's version runs before its pool:
            # whether it's a problem depends only on the parsed file, not on
            # the database. Before this check existed, such rows were simply
            # dropped from `targets` below and the probe printed a count
            # that silently omitted them -- indistinguishable from "this
            # file has no duplicates" even though it was never checked.
            unrouted = ImportBatch(
                fills=tuple(f for f in batch.fills if f.external_ref is None),
                cash=tuple(c for c in batch.cash if c.external_ref is None),
            )
            if (unrouted.fills or unrouted.cash) and not args.account:
                print(
                    "error: cannot check duplicates -- this file has row(s) "
                    "with no account ref to route by; pass --account to say "
                    "where they go",
                    file=sys.stderr,
                )
                return 2

            pool = await create_pool()
            try:
                async with pool.acquire() as conn:
                    # Read-only routing (route_batch issues only SELECTs) to
                    # find which account(s) each row belongs to -- same
                    # mechanism --commit uses, reused rather than reinvented so
                    # the probe never disagrees with --commit about where a row
                    # lands.
                    plan = await route_batch(conn, venue, batch)
                    targets: dict[UUID, ImportBatch] = dict(plan.by_account)

                    if (unrouted.fills or unrouted.cash) and args.account:
                        account_id = UUID(args.account)
                        existing = targets.get(account_id, ImportBatch())
                        targets[account_id] = ImportBatch(
                            fills=existing.fills + unrouted.fills,
                            cash=existing.cash + unrouted.cash,
                        )

                    # C: mirror --commit's own refusal exactly (see the
                    # identical check further below) -- a row that routes to
                    # an unknown account ref is never probed (it never lands
                    # in `targets`), so printing a count without checking
                    # this first would silently omit it while looking
                    # complete. plan.unknown_refs is money-scoped (see
                    # db.importing.RoutingPlan's docstring); a non-money
                    # unknown ref does not stand behind --commit's refusal
                    # either, so it must not stand behind this one.
                    if plan.unknown_refs:
                        print(
                            "error: cannot check duplicates -- unknown "
                            f"account ref(s): {', '.join(plan.unknown_refs)}",
                            file=sys.stderr,
                        )
                        return 2

                    fill_dupes = cash_dupes = 0
                    for account_id, sub_batch in targets.items():
                        report = await probe_duplicates(conn, account_id, sub_batch)
                        fill_dupes += report.fill_dupes
                        cash_dupes += report.cash_dupes
                    print(
                        f"  duplicate check: {fill_dupes} fill(s), "
                        f"{cash_dupes} cash movement(s) already present"
                    )
            finally:
                # See cmd_import's identical comment further below: pool.close()
                # must run after the `async with pool.acquire()` block has
                # exited, never from inside it, or close() deadlocks waiting for
                # a release that will never come.
                await pool.close()

        print("\npreview only — rerun with --commit to write")
        return 0

    # Rows with no external_ref at all (a venue with no per-row account
    # identifier, e.g. Coinbase) are never routed by route_batch -- they need
    # an explicit destination. Whether that's a problem depends only on the
    # parsed file, not on the database, so this check runs before the pool is
    # ever opened.
    unrouted = ImportBatch(
        fills=tuple(f for f in batch.fills if f.external_ref is None),
        cash=tuple(c for c in batch.cash if c.external_ref is None),
    )
    if (unrouted.fills or unrouted.cash) and not args.account:
        print(
            "error: this file has row(s) with no account ref to route by "
            "(e.g. this venue's export carries no per-row account number); "
            "pass --account to say where they go",
            file=sys.stderr,
        )
        return 2

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            plan = await route_batch(conn, venue, batch)

            # Same "refuse the whole batch, write nothing" shape as the
            # unknown-ref refusal below. See ImportBatch.blocking's
            # docstring for why this is neither "block on every unmapped
            # row" nor "block on none": an unmapped row that also carries
            # money (a non-zero quantity or amount) is exactly the shape of
            # the defect that motivated this whole task -- committing
            # everything else and silently dropping that row's money would
            # look like a successful import.
            #
            # C1: this check must run AFTER route_batch, not before, and
            # must drop any reason whose row belongs to an account
            # registered ignore_on_import (plan.ignored_refs) -- otherwise a
            # money-carrying unmapped row on an account the user has
            # explicitly said to skip (e.g. a retirement plan with no
            # instrument identity) refuses the ENTIRE import, permanently,
            # with no escape. route_batch issues only SELECTs, so this still
            # returns before `async with conn.transaction():` below --
            # "refuses and writes nothing" is preserved.
            effective_blocking = [
                (ref, msg) for ref, msg in batch.blocking if ref not in plan.ignored_refs
            ]
            if effective_blocking:
                print(
                    "error: refusing to commit -- unmapped row(s) carry money and no "
                    "rule matched them:",
                    file=sys.stderr,
                )
                for _ref, msg in effective_blocking:
                    print(f"  {msg}", file=sys.stderr)
                return 2

            targets: dict[UUID, ImportBatch] = dict(plan.by_account)

            if unrouted.fills or unrouted.cash:
                account_id = UUID(args.account)
                account = await get_account(conn, account_id)
                if account is None:
                    print(f"error: no account with id {account_id}", file=sys.stderr)
                    return 2
                # A file parsed by one venue's importer must never be committed to an
                # account belonging to a different venue — that would permanently
                # attribute (e.g.) Coinbase fills to a Fidelity account, with no CLI
                # path to undo it.
                if account["venue"] != venue:
                    print(
                        f"error: account {account_id} is a {account['venue']!r} account; "
                        f"refusing to commit a {venue!r} import to it",
                        file=sys.stderr,
                    )
                    return 2
                existing = targets.get(account_id, ImportBatch())
                targets[account_id] = ImportBatch(
                    fills=existing.fills + unrouted.fills,
                    cash=existing.cash + unrouted.cash,
                )

            for account_id, sub_batch in targets.items():
                n = len(sub_batch.fills) + len(sub_batch.cash)
                print(f"  {account_id}: mapped, {n} row(s)")
            for ref in plan.ignored_refs:
                print(f"  {ref}: ignored (ignore_on_import), skipped")
            # F: plan.reported_unknown_refs is a superset of plan.unknown_refs
            # -- it also includes a ref that appears ONLY in batch.refs_seen
            # (an account whose rows are ALL unmapped and non-financial),
            # which route_batch used to be unable to see at all since it only
            # looked at fills/cash/blocking. Reporting the fuller set here
            # does not change what refuses the commit -- that stays keyed on
            # plan.unknown_refs alone, checked below, unaffected by this.
            for ref in plan.reported_unknown_refs:
                print(f"  {ref}: no matching account", file=sys.stderr)

            # route_batch only ever sees refs that appear on a fill, cash
            # movement, or blocking reason -- a REGISTERED account whose rows
            # all warned but produced none of those (an edge route_batch's
            # own classification doesn't reach) can still fall out here.
            # batch.refs_seen (every ref seen in the raw rows) is the only
            # place such an account is visible; report it explicitly rather
            # than let a real account silently vanish from the report while
            # the commit still reports success. Refs already reported above
            # (ignored, or unknown -- F) are excluded so each ref is reported
            # exactly once.
            covered_refs = (
                {f.external_ref for f in batch.fills if f.external_ref}
                | {c.external_ref for c in batch.cash if c.external_ref}
            )
            already_reported = set(plan.ignored_refs) | set(plan.reported_unknown_refs)
            for ref in sorted(set(batch.refs_seen) - covered_refs - already_reported):
                print(
                    f"  {ref}: 0 row(s) mapped -- every row for this account "
                    "failed to classify; see warnings above",
                    file=sys.stderr,
                )

            # Partial commits are not acceptable -- a silently-skipped account
            # looks like a successful import. Refuse the WHOLE batch, and write
            # nothing at all, if any row routes to an account that doesn't exist.
            if plan.unknown_refs:
                print(
                    "error: refusing to commit -- unknown account ref(s): "
                    f"{', '.join(plan.unknown_refs)}",
                    file=sys.stderr,
                )
                return 2

            # I3: refuse a batch that would make an account's fills reachable
            # by two mutually-blind dedupe paths.
            #
            # The two partial unique indexes are disjoint BY CONSTRUCTION:
            # fill_venue_id_uniq is WHERE venue_fill_id IS NOT NULL,
            # fill_content_hash_uniq is WHERE content_hash IS NOT NULL, and
            # db/importing.py gives a fill exactly one of the two keys, never
            # both. A pre-cut-over CSV Coinbase fill therefore has
            # (venue_fill_id NULL, content_hash SET); the SAME trade arriving
            # via `sync` has (venue_fill_id SET, content_hash NULL). Neither
            # index can see the other. Both rows land, both feed
            # regroup_account, and the account's position and realized P&L
            # silently DOUBLE. Nothing else in the system would notice.
            #
            # Deliberately generic, not Coinbase-specific: any venue that ever
            # cuts a CSV path over to an API one has this exact hazard, and
            # the check costs one SELECT count(*) per target account, on the
            # commit path only. It runs here -- after route_batch, before
            # `async with conn.transaction():` -- so it refuses and writes
            # nothing, and it is unreachable from preview, which opens no
            # connection at all (a tested invariant).
            mixed = []
            for account_id, sub_batch in targets.items():
                if not any(f.venue_fill_id for f in sub_batch.fills):
                    continue
                legacy = await conn.fetchval(
                    """
                    SELECT count(*) FROM fill
                     WHERE account_id = $1
                       AND content_hash IS NOT NULL
                       AND venue_fill_id IS NULL
                    """,
                    account_id,
                )
                if legacy:
                    mixed.append((account_id, legacy))
            if mixed:
                print(
                    "error: refusing to commit -- this batch's fills carry a venue "
                    "fill id, but the target account already holds fill(s) that "
                    "dedupe on content_hash instead:",
                    file=sys.stderr,
                )
                for account_id, legacy in mixed:
                    print(
                        f"  {account_id}: {legacy} existing fill(s) with "
                        "content_hash set and venue_fill_id null",
                        file=sys.stderr,
                    )
                print(
                    "  The two dedupe indexes are disjoint, so the same trade "
                    "arriving by both paths would be inserted twice and double "
                    "the account's position and realized P&L. Remedy: delete the "
                    "older content_hash-keyed fills for this account (they are "
                    "the ones with source='csv' and venue_fill_id null) and "
                    "re-sync, or commit into a fresh account.",
                    file=sys.stderr,
                )
                return 2

            fills_inserted = fills_skipped = cash_inserted = trades_regrouped = 0
            async with conn.transaction():
                for account_id, sub_batch in targets.items():
                    result = await commit_batch(conn, account_id, sub_batch, source=source)
                    fills_inserted += result.fills_inserted
                    fills_skipped += result.fills_skipped
                    cash_inserted += result.cash_inserted
                    trades_regrouped += await regroup_account(conn, account_id)
    finally:
        # pool.close() waits for every checked-out connection to be released.
        # It must run after the `async with pool.acquire()` block has exited
        # (or after an early `return` inside it unwound out of that `with`) —
        # never from inside it while the connection returned by acquire() is
        # still held, or close() deadlocks waiting for a release that will
        # never come from a still-open acquire block.
        await pool.close()

    print(
        f"inserted {fills_inserted} fills ({fills_skipped} already present), "
        f"{cash_inserted} cash movements, {trades_regrouped} trades regrouped"
    )
    return 0


def _parse_sync_bound(raw: str | None) -> datetime | None:
    """--start/--end are ISO-8601 strings on the CLI; fetch_all_fills wants a
    datetime and calls .astimezone(UTC) on whichever it's given. A bound
    with no offset is anchored to UTC here rather than left for
    astimezone() to silently treat as the local zone -- the venue API's own
    sequence_timestamp is UTC, so reinterpreting a bare bound as local time
    would shift the requested window with no error at all."""
    if raw is None:
        return None
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def cmd_sync(args) -> int:
    """Fetch fills from a venue API and run them through the exact same
    preview/commit body `cmd_import` uses (`_preview_or_commit`) -- `sync`
    differs from `import` only in where the text comes from (an API call
    instead of a file on disk). Never grows its own write path.

    No `if args.venue != "coinbase"` guard here: argparse's own
    `choices=["coinbase"]` on the `venue` positional (main(), below) is the
    only thing that ever needs to reject an unknown sync venue, and it does
    so before cmd_sync is even called -- a second, hand-written check here
    could only ever agree with argparse's `choices` or silently drift out of
    sync with it, never usefully disagree. When a second venue is added,
    branch here on `args.venue` to pick its client/importer; until then
    there is nothing else for this function to check.
    """
    try:
        creds = CoinbaseCredentials.from_env()
    except RuntimeError as exc:
        # Fail loud: absent or rejected credentials must surface as an
        # error and a non-zero exit, never as a request that silently runs
        # unauthenticated and reports "0 fills found" (spec §10 gap 5).
        # Raised as SystemExit (rather than returned) so a caller driving
        # cmd_sync directly -- not through main()'s asyncio.run wrapper --
        # still gets a hard stop instead of a return code it could ignore.
        print(f"error: {exc}", file=sys.stderr)
        # `from exc` (M2, ruff B904): without it the credentials RuntimeError
        # is reported as "During handling of the above exception, another
        # exception occurred", which reads like a bug in the handler rather
        # than the cause it actually is.
        raise SystemExit(2) from exc

    text = await fetch_all_fills(
        creds,
        start=_parse_sync_bound(args.start),
        end=_parse_sync_bound(args.end),
    )
    importer = get_importer("coinbase-api")
    batch = importer.parse(text)
    # importer.account_venue ("coinbase"), not importer.venue
    # ("coinbase-api"): see importers/base.py's Importer.account_venue
    # docstring and _preview_or_commit's docstring above.
    #
    # source="api" (I2): these fills came off the REST endpoint, not a CSV.
    # commit_batch's `source` defaulted to "csv" and nothing overrode it, so
    # every fill `sync` had ever written claimed CSV provenance.
    return await _preview_or_commit(importer.account_venue, batch, args, source="api")


async def cmd_regroup(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                written = await regroup_account(conn, UUID(args.account))
    except UnknownAccountError as exc:
        # Same clean-error treatment cmd_import gives an unknown --account,
        # rather than the ValueError('None is not a valid TradeIntent')
        # traceback this used to produce with no account id in it anywhere.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited (including via the
        # early return above), never from inside it, or close() deadlocks
        # waiting for a release that will never come.
        await pool.close()
    print(f"{written} trades")
    return 0


async def cmd_trades(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            rows = await list_trades(conn, UUID(args.account) if args.account else None)
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    for t in rows:
        print(
            f"{t['opened_at']:%Y-%m-%d}  {t['primary_underlying'] or '?':<8} "
            f"{t['direction']:<6} {t['status']:<6} "
            f"pnl={t['realized_pnl'] or 0:>12}  intent={t['intent']}"
        )
    return 0


# Display-only scale bounds for `deadband positions`. NOTHING below this
# comment changes what ledger/ computes or what the database stores -- the
# pure layer keeps its full 50-digit precision and the numerics keep theirs;
# only the string handed to print() is bounded.
#
# Why a bound is needed at all: `cost_basis` is a division (weighted notional
# / quantity) evaluated at ctx.prec = 50, so an ordinary two-lot position
# whose weighted average does not terminate (1 @ 10 + 2 @ 20) renders a
# 50-digit basis and, downstream, a 28-digit unrealized. Those digits assert a
# precision the inputs never had and wrap the row off a normal terminal.
#
# Why 8 dp and not 2: a 2-dp display quantum would print a satoshi-scale
# crypto price or quantity as "0.00" -- a silently wrong number, which is the
# outcome this project ranks worst. 8 dp covers every price and quantity scale
# the importers actually produce.
_DISPLAY_QUANT = Decimal("1E-8")

# ...and a floor, so a genuine zero renders "0.00" rather than "0". The
# unmarked-position placeholder is "--"; a real zero has to be visibly a
# number, since mark_price_chk permits a genuine 0 price.
_DISPLAY_MIN_DP = 2


def _fmt_decimal(value: Decimal) -> str:
    """Render a Decimal for a positions row: bounded scale, no exponent.

    Trailing zeros beyond two decimal places are trimmed, so an exact 25
    prints "25.00" and not "25.00000000".

    Two escape hatches, both deliberately preferring a wide-but-true column
    over a narrow-but-false one:

    * a value too large to quantize (InvalidOperation) is printed in full;
    * a non-zero value that would round to zero at 8 dp is printed in full,
      because "0.00" for a position that is not flat is exactly the silent
      lie the bound exists to avoid.
    """
    try:
        q = value.quantize(_DISPLAY_QUANT)
    except InvalidOperation:  # magnitude too large for the display scale
        return str(value)
    if q == 0:
        # `value != 0` means rounding, not the value, produced the zero.
        return str(value) if value != 0 else "0.00"
    text = format(q, "f")
    if "." in text:
        whole, _, frac = text.rstrip("0").partition(".")
        text = f"{whole}.{frac.ljust(_DISPLAY_MIN_DP, '0')}"
    return text


async def cmd_positions(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            positions = await open_positions(
                conn, UUID(args.account) if args.account else None
            )
            marks = await latest_marks(conn, [p.instrument_id for p in positions])
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()

    for p in positions:
        mark = marks.get(p.instrument_id)
        # Gate on unvaluable_reason, NEVER on direction and NEVER by catching
        # unrealized_pnl's NotImplementedError(SPREAD): a position can carry a
        # real, single-valued direction and still be unvaluable for another
        # reason (e.g. an unknown quantity on one contributing trade), and
        # catching the exception here would also swallow a future genuine
        # bug in unrealized_pnl itself. A position with a reason set is still
        # printed -- never filtered out -- because a position missing from a
        # position listing is this project's recurring silent-loss shape.
        if p.unvaluable_reason is not None:
            unreal, mark_col = f"n/a ({p.unvaluable_reason})", "--"
            # The quantity and cost basis go to "--" too, not just the mark
            # and unrealized columns. For a mixed-direction group `quantity`
            # is the sum of MAGNITUDES (long 10 + short 4 = 14: not the net,
            # not either leg, not gross exposure in any direction) and
            # `cost_basis` averages a long basis with a short one. For an
            # "open quantity unknown" group it is a partial sum over only the
            # priced contributors. Both are fabricated figures in the two
            # columns a reader parses first, and the "n/a (reason)"
            # disclaimer sits four fields to their right where it reads as
            # "we can't price this", not "the 14 is meaningless too".
            #
            # The row itself is still printed -- a position missing from a
            # position listing is this project's recurring silent-loss shape.
            # Blanking the numbers is the opposite of hiding the row: it
            # leaves the symbol, the reason, and nothing that could be
            # mistaken for a holding.
            qty_col = basis_col = "--"
        elif mark is None:
            # Absent from `marks`, not a zero -- db.marks.latest_marks never
            # reports a zero for an unmarked instrument (mark_price_chk
            # permits a genuine 0.00, so a placeholder must be visibly
            # different from that, not just "0.00" again).
            unreal, mark_col = "--", "--"
            qty_col, basis_col = _fmt_decimal(p.quantity), _fmt_decimal(p.cost_basis)
        else:
            price, as_of = mark
            unreal = _fmt_decimal(
                unrealized_pnl(p.quantity, p.cost_basis, price, p.multiplier, p.direction)
            )
            # The mark's age rides along in the same column as its price: a
            # month-old mark must never render identically to one from a
            # minute ago, so the as_of date is always shown, not just the
            # price.
            mark_col = f"{_fmt_decimal(price)} @{as_of:%Y-%m-%d}"
            qty_col, basis_col = _fmt_decimal(p.quantity), _fmt_decimal(p.cost_basis)
        estimated = " ~" if p.is_estimated else "  "
        # 21, not 10: an OCC option symbol is up to 21 characters
        # ("SPY   260821C00500000"), and at width 10 every later column on an
        # option row shifted right by whatever the symbol overflowed by.
        # Deliberately widened rather than truncated -- a truncated contract
        # symbol names a DIFFERENT contract (a different strike or expiry)
        # just as plausibly as the real one, and a misread strike is a wrong
        # position, whereas a wide column is only ugly. Anything longer than
        # 21 still overflows, loudly, for the same reason.
        # Account name, not just id: positions now group by (account,
        # instrument) rather than instrument alone (a taxable and a
        # retirement account's cost basis are not fungible), and --account
        # filters that grouping rather than changing what a row means, so an
        # unscoped listing can show the same symbol more than once, once per
        # account -- the account column is what tells those rows apart.
        # 15 wide, left-justified like the symbol column, and never
        # truncated for the same reason the symbol column isn't: a
        # truncated account name can read as a different, shorter-named
        # account that happens to exist, which is a wrong answer dressed as
        # a real one, whereas an overflowing column is only ugly. An
        # explicit space follows it (unlike the symbol column, which relies
        # on the estimated marker's own leading space) so a name at or past
        # the 15-char width still can't run straight into the quantity
        # column with no gap at all.
        print(
            f"{p.symbol:<21}{estimated} {p.account_name:<15} {qty_col:>14} {basis_col:>14} "
            f"{mark_col:>22} {unreal}"
        )
    if not positions:
        print("no open positions")
    return 0


# latest_marks (db/marks.py) treats the newest as_of as "the current price"
# with nothing else checking plausibility -- a fat-fingered year or a bad
# backfill would otherwise silently become today's price and produce a wrong
# unrealized figure with no signal at all. The tolerance absorbs clock skew
# between this box and the database, and the fact that "now" isn't identically
# defined on two machines, without opening the door to a meaningfully wrong
# future date. Two minutes comfortably covers ordinary clock drift for a
# command that is typed by hand, not fired in a tight loop.
_MARK_FUTURE_TOLERANCE = timedelta(minutes=2)


async def cmd_marks_set(args) -> int:
    # The clock lives here, in the I/O layer -- db/marks.py and everything
    # under ledger/ are clock-free by design. This single `now` anchors both
    # the omitted-as_of default and the future-date guard below, so the two
    # measure against the exact same instant.
    now = datetime.now(UTC)

    # Decimal("abc") raises decimal.InvalidOperation, which does NOT descend
    # from ValueError -- same class of gotcha the --account UUID parsing in
    # main() works around below (see its comment): a bare `except ValueError`
    # would let this crash through uncaught instead of becoming a clean
    # message. Decimal("NaN") and Decimal("Infinity") construct successfully
    # and slip past that catch entirely -- is_finite() is this codebase's
    # established check for catching them afterward (see
    # importers/fidelity.py, importers/coinbase_api.py); left unchecked,
    # mark_price_chk would refuse them as an uncaught
    # asyncpg.CheckViolationError instead of a clean CLI error. Parsed before
    # opening the pool: whether this is a problem depends only on the
    # argument, never on the database.
    try:
        price = Decimal(args.price)
    except InvalidOperation:
        print(f"error: --price {args.price!r} is not a valid number", file=sys.stderr)
        return 2
    if not price.is_finite():
        print(f"error: --price {args.price!r} must be a finite number", file=sys.stderr)
        return 2

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            if args.symbol:
                try:
                    instrument_id = await resolve_instrument_by_symbol(conn, args.symbol)
                except ValueError as exc:
                    print(
                        f"error: {exc} -- pass --natural-key instead of --symbol "
                        "to name the exact instrument",
                        file=sys.stderr,
                    )
                    return 2
            else:
                instrument_id = await conn.fetchval(
                    "SELECT id FROM instrument WHERE natural_key = $1", args.natural_key
                )
                if instrument_id is None:
                    print(
                        f"error: no instrument with natural_key {args.natural_key!r}",
                        file=sys.stderr,
                    )
                    return 2

            as_of = datetime.fromisoformat(args.as_of) if args.as_of else now

            # A naive (timezone-less) as_of must be caught HERE, before the
            # future-date comparison just below -- `as_of > now + tolerance`
            # between an offset-naive and an offset-aware datetime raises a
            # raw, uncaught TypeError ("can't compare offset-naive and
            # offset-aware datetimes"), never reaching set_mark's own
            # ValueError for exactly this case. Checking first means a
            # fat-fingered timestamp with no offset always gets a clean
            # message instead of a traceback.
            if as_of.tzinfo is None:
                print(
                    f"error: --as-of {args.as_of!r} has no UTC offset "
                    "(e.g. append +00:00 or Z)",
                    file=sys.stderr,
                )
                return 2

            # Refuse before writing: an ambiguous symbol, a naive as_of
            # (above), or a future-dated as_of (below) must never half-apply.
            # All of resolution and validation happens before set_mark is
            # ever called.
            if as_of > now + _MARK_FUTURE_TOLERANCE:
                print(
                    f"error: --as-of {as_of.isoformat()} is in the future "
                    f"(tolerance: {_MARK_FUTURE_TOLERANCE})",
                    file=sys.stderr,
                )
                return 2

            await set_mark(conn, instrument_id, price, as_of)
    finally:
        # See cmd_trades's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    return 0


async def cmd_snapshot_add(args) -> int:
    # The clock lives here, in the I/O layer -- db/snapshots.py is
    # clock-free by design, same reasoning as cmd_marks_set's identical
    # comment. This anchors the future-date guard below.
    now = datetime.now(UTC)

    # Same InvalidOperation/is_finite guards cmd_marks_set already has for
    # --price, applied to both broker figures: Decimal("abc") raises
    # InvalidOperation (not a ValueError), and Decimal("NaN") /
    # Decimal("Infinity") construct successfully and would otherwise reach
    # the database as a broker figure. Both parsed before opening the pool --
    # whether this is a problem depends only on the arguments, never on the
    # database.
    try:
        total_equity = Decimal(args.equity)
    except InvalidOperation:
        print(f"error: --equity {args.equity!r} is not a valid number", file=sys.stderr)
        return 2
    if not total_equity.is_finite():
        print(f"error: --equity {args.equity!r} must be a finite number", file=sys.stderr)
        return 2

    try:
        cash_balance = Decimal(args.cash)
    except InvalidOperation:
        print(f"error: --cash {args.cash!r} is not a valid number", file=sys.stderr)
        return 2
    if not cash_balance.is_finite():
        print(f"error: --cash {args.cash!r} must be a finite number", file=sys.stderr)
        return 2

    # A bare date ("2026-07-31") is the ordinary way to enter a statement
    # date and becomes midnight UTC -- but a naive TIMESTAMP is refused,
    # matching marks_set exactly, because unlike a bare date it silently
    # implies a wall-clock zone nobody named. date.fromisoformat accepts
    # ONLY a bare "YYYY-MM-DD" string, so it cleanly tells the two apart:
    # anything with a time component fails here and falls through to the
    # naive-timestamp check below, unchanged from marks_set's behavior.
    try:
        as_of = datetime.combine(date.fromisoformat(args.as_of), time.min, tzinfo=UTC)
    except ValueError:
        try:
            as_of = datetime.fromisoformat(args.as_of)
        except ValueError:
            print(
                f"error: --as-of {args.as_of!r} is not a valid date or timestamp",
                file=sys.stderr,
            )
            return 2
        # Same TypeError hazard cmd_marks_set's identical comment describes:
        # `as_of > now + tolerance` between an offset-naive and an
        # offset-aware datetime raises an uncaught TypeError, never reaching
        # add_snapshot's own ValueError for exactly this case. Checking here
        # means a fat-fingered timestamp with no offset always gets a clean
        # message instead of a traceback.
        if as_of.tzinfo is None:
            print(
                f"error: --as-of {args.as_of!r} has no UTC offset "
                "(e.g. append +00:00 or Z, or pass a bare date)",
                file=sys.stderr,
            )
            return 2

    # Same reasoning as cmd_marks_set's identical guard: latest_snapshot
    # treats the newest as_of as current, so a fat-fingered year would
    # silently become the figure every reconciliation compares against.
    # Reuses cmd_marks_set's tolerance constant rather than defining a
    # second one -- both commands are typed by hand, not fired in a loop,
    # and absorb the same clock skew for the same reason.
    if as_of > now + _MARK_FUTURE_TOLERANCE:
        print(
            f"error: --as-of {as_of.isoformat()} is in the future "
            f"(tolerance: {_MARK_FUTURE_TOLERANCE})",
            file=sys.stderr,
        )
        return 2

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            await add_snapshot(
                conn,
                UUID(args.account),
                as_of,
                cash_balance,
                total_equity,
                note=args.note,
            )
    finally:
        # See cmd_trades's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    return 0


async def cmd_reconcile(args) -> int:
    """Compare the ledger against a stored broker-statement snapshot and
    report one trustworthy verdict (spec §7). This is the command the whole
    branch exists for -- see ledger/reconcile.py for the pure comparison and
    its Drift.verdict field, which is THE thing rendered below.
    """
    # The clock lives here, in the I/O layer -- same reasoning as
    # cmd_marks_set's and cmd_snapshot_add's identical comments.
    now = datetime.now(UTC)

    as_of = now
    if args.as_of:
        try:
            as_of = datetime.fromisoformat(args.as_of)
        except ValueError:
            print(f"error: --as-of {args.as_of!r} is not a valid timestamp", file=sys.stderr)
            return 2
        # Same TypeError hazard cmd_marks_set's identical comment describes:
        # a naive datetime compared against an aware one downstream (here,
        # inside latest_snapshot's own `as_of <= $2` bind) would raise
        # asyncpg's own confusing error instead of a clean one naming the flag.
        if as_of.tzinfo is None:
            print(
                f"error: --as-of {args.as_of!r} has no UTC offset "
                "(e.g. append +00:00 or Z)",
                file=sys.stderr,
            )
            return 2

    # Same InvalidOperation/is_finite guards cmd_marks_set and cmd_snapshot_add
    # already have for their own Decimal arguments.
    tolerance = Decimal("0.01")
    if args.tolerance is not None:
        try:
            tolerance = Decimal(args.tolerance)
        except InvalidOperation:
            print(
                f"error: --tolerance {args.tolerance!r} is not a valid number",
                file=sys.stderr,
            )
            return 2
        if not tolerance.is_finite():
            print(
                f"error: --tolerance {args.tolerance!r} must be a finite number",
                file=sys.stderr,
            )
            return 2
        # is_finite() above rejects NaN/Infinity but not a negative number.
        # A negative tolerance makes `abs(difference) <= tolerance`
        # unsatisfiable, so EVERY run -- even a perfectly reconciled account
        # -- would report DRIFT: a confidently wrong verdict produced from a
        # silently accepted bad input, the same failure class the
        # mixed-currency refusal below exists to avoid. Checked here, before
        # the pool is ever opened, same as every other argument guard above.
        if tolerance < 0:
            print(
                f"error: --tolerance {args.tolerance!r} must not be negative "
                "-- a negative tolerance would make every comparison read as drift",
                file=sys.stderr,
            )
            return 2

    account_id = UUID(args.account)

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            # 1. Resolve the account. Unknown id refuses, exit 2 -- same
            # get_account-then-check-None shape cmd_import already uses for
            # its own --account, rather than letting a foreign-key violation
            # surface later as a raw traceback.
            account = await get_account(conn, account_id)
            if account is None:
                print(f"error: no account with id {account_id}", file=sys.stderr)
                return 2

            # 2. latest_snapshot. None refuses, exit 2 -- reporting "zero
            # drift" against nothing is the silent-success shape this whole
            # command exists to avoid.
            snap_row = await latest_snapshot(conn, account_id, as_of)
            if snap_row is None:
                print(
                    f"error: no snapshot on or before {as_of.isoformat()} for "
                    f"account {account_id} -- record one with `snapshot add`",
                    file=sys.stderr,
                )
                return 2
            snapshot = Snapshot(
                account_id=account_id,
                as_of=snap_row["as_of"],
                cash_balance=snap_row["cash_balance"],
                total_equity=snap_row["total_equity"],
            )

            # 3-4. open_positions, then partition on unvaluable_reason --
            # NEVER on direction. A group can agree on a single direction and
            # still be unvaluable for another reason (ledger/positions.py's
            # own OpenPosition docstring), so a non-None direction here is not
            # a signal that pricing is safe.
            open_pos = await open_positions(conn, account_id)
            positions: list[Position] = []
            unvaluable: list[UnvaluableRef] = []
            for p in open_pos:
                if p.unvaluable_reason is None:
                    positions.append(
                        Position(
                            instrument_id=p.instrument_id,
                            quantity=p.quantity,
                            cost_basis=p.cost_basis,
                            multiplier=p.multiplier,
                        )
                    )
                else:
                    unvaluable.append(
                        UnvaluableRef(
                            instrument_id=p.instrument_id,
                            symbol=p.symbol,
                            reason=p.unvaluable_reason,
                        )
                    )

            # 5. latest_marks for the valuable instrument ids only, mapped to
            # JUST the price. latest_marks returns a (price, timestamp) tuple
            # per instrument (db/marks.py) -- reconcile() wants a bare
            # Mapping[UUID, Decimal], so passing the tuple straight through
            # would misvalue every marked position. An instrument absent from
            # this dict (never present with a zero -- a genuine 0 mark is
            # legal) falls back to cost basis inside reconcile() itself.
            raw_marks = await latest_marks(conn, [p.instrument_id for p in positions])
            marks = {instrument_id: price for instrument_id, (price, _as_of) in raw_marks.items()}

            # 6. account_cash; MixedCurrencyError refuses, exit 2.
            try:
                computed_cash = await account_cash(conn, account_id)
            except MixedCurrencyError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
    finally:
        # See cmd_trades's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()

    # 7. reconcile() -> Drift. Pure, no I/O, no clock.
    drift = reconcile(
        snapshot, positions, marks, computed_cash, unvaluable=unvaluable, tolerance=tolerance
    )

    # 8. Render by verdict -- THE field callers render (see Drift's own
    # docstring). is_within_tolerance answers only "do the numbers agree" and
    # is a component, never the answer: rendering it alone would print a
    # clean pass on an account with unvalued positions.
    print(f"account {account_id}")
    # Two DIFFERENT clocks, deliberately labelled apart: drift.as_of is the
    # STATEMENT's date (snapshot.as_of, ledger/reconcile.py), but
    # computed_cash, open_positions and latest_marks above all read CURRENT
    # ledger state -- open_positions and latest_marks take no `as_of` at all.
    # A single "as of <statement date>" header above numbers that are
    # actually current would misrepresent a week of ordinary trading since
    # the statement as drift "as of" a date before any of it happened -- the
    # same phantom-hunt shape the unvaluable-exclusion message above exists
    # to prevent. `now` was captured at the very top of this function, so it
    # is the same instant the future-date guards above measured against.
    print(f"  statement as of {drift.as_of.isoformat()}")
    print(f"  ledger as of    {now.isoformat()}")
    print(f"  verdict: {drift.verdict.value}")
    print(
        f"  equity: computed {drift.computed_equity}  reported {drift.reported_equity}  "
        f"diff {drift.equity_difference}"
    )
    print(
        f"  cash:   computed {drift.computed_cash}  reported {drift.reported_cash}  "
        f"diff {drift.cash_difference}"
    )
    if drift.unmarked_instruments:
        print(
            f"  {len(drift.unmarked_instruments)} position(s) valued at cost basis -- "
            "no mark on file"
        )
    if drift.unvaluable_positions:
        # The output must explain the alarming number: computed_equity above
        # EXCLUDES these positions entirely (they were never turned into a
        # Position), so a large equity_difference here is expected, not
        # necessarily a defect -- saying so is the difference between a
        # useful report and a phantom hunt.
        print(
            f"  {len(drift.unvaluable_positions)} position(s) excluded from "
            "computed equity above (not included in the totals -- cannot be priced):"
        )
        for u in drift.unvaluable_positions:
            print(f"    {u.symbol}: {u.reason}")

    if drift.verdict == ReconcileVerdict.OK:
        return 0
    if drift.verdict == ReconcileVerdict.DRIFT:
        print("drift: the ledger and the statement disagree outside tolerance", file=sys.stderr)
        return 1
    print(
        "unreliable: one or more positions could not be priced, so this verdict "
        "cannot be trusted as a clean pass or a clean drift",
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="deadband")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending database migrations").set_defaults(fn=cmd_migrate)

    p_accounts = sub.add_parser("accounts")
    p_accounts.set_defaults(fn=cmd_accounts)
    accounts_sub = p_accounts.add_subparsers(dest="accounts_command")
    p_accounts_add = accounts_sub.add_parser("add", help="create a new account")
    p_accounts_add.add_argument("--name", required=True)
    p_accounts_add.add_argument("--venue", required=True)
    p_accounts_add.add_argument(
        "--account-type", required=True, choices=["cash", "margin", "funded", "wallet"]
    )
    p_accounts_add.add_argument("--external-ref", default=None)
    p_accounts_add.add_argument(
        "--default-intent", default="trade", choices=["trade", "investment", "mixed"]
    )
    p_accounts_add.add_argument(
        "--ignore-on-import",
        action="store_true",
        help=(
            "skip this account's rows on import instead of refusing the whole "
            "commit for an account you don't intend to import (e.g. a "
            "retirement plan with no instrument identity)"
        ),
    )
    p_accounts_add.set_defaults(fn=cmd_accounts_add)

    p_import = sub.add_parser("import", help="parse a venue export")
    p_import.add_argument("venue", choices=list_importers())
    p_import.add_argument("file")
    p_import.add_argument(
        "--account",
        help=(
            "account UUID for rows with no venue-supplied account ref "
            "(e.g. Coinbase); a venue that carries its own per-row account "
            "number (e.g. Fidelity) routes automatically and does not need this"
        ),
    )
    p_import.add_argument("--commit", action="store_true", help="write to the database")
    p_import.add_argument(
        "--check-duplicates",
        action="store_true",
        help=(
            "preview only: open a READ-ONLY database connection and report "
            "how many rows are already present. Plain preview (without this "
            "flag) deliberately never touches the database at all -- this is "
            "an explicit opt-in exception, not a change to preview's default"
        ),
    )
    p_import.set_defaults(fn=cmd_import)

    p_sync = sub.add_parser("sync", help="fetch from a venue API and import")
    p_sync.add_argument("venue", choices=["coinbase"])
    p_sync.add_argument(
        "--account", required=True, help="account UUID: the API carries no per-row account ref"
    )
    p_sync.add_argument("--start", help="ISO-8601 lower bound on sequence_timestamp")
    p_sync.add_argument("--end", help="ISO-8601 upper bound on sequence_timestamp")
    p_sync.add_argument("--commit", action="store_true", help="write to the database")
    p_sync.set_defaults(fn=cmd_sync)

    p_regroup = sub.add_parser("regroup")
    p_regroup.add_argument("--account", required=True)
    p_regroup.set_defaults(fn=cmd_regroup)

    p_trades = sub.add_parser("trades")
    p_trades.add_argument("--account")
    p_trades.set_defaults(fn=cmd_trades)

    p_positions = sub.add_parser(
        "positions", help="open positions, with unrealized P&L where marked"
    )
    p_positions.add_argument("--account")
    p_positions.set_defaults(fn=cmd_positions)

    p_marks = sub.add_parser("marks", help="manual price marks")
    marks_sub = p_marks.add_subparsers(dest="marks_command", required=True)
    p_marks_set = marks_sub.add_parser("set", help="record a price mark for an instrument")
    group = p_marks_set.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbol", help="instrument symbol; refused if it is ambiguous")
    group.add_argument("--natural-key", help="exact instrument natural key")
    # The unit matters and is not guessable: for an option the correct input
    # is the per-share premium (2.50), not the per-contract cost (250). The
    # contract multiplier is applied downstream by unrealized_pnl, so entering
    # the per-contract figure produces a silently 100x wrong unrealized P&L.
    p_marks_set.add_argument(
        "--price",
        required=True,
        help=(
            "price per unit, excluding the contract multiplier, "
            "in the instrument's quote currency"
        ),
    )
    p_marks_set.add_argument(
        "--as-of", default=None, help="ISO-8601 timestamp; defaults to now (UTC)"
    )
    p_marks_set.set_defaults(fn=cmd_marks_set)

    p_snapshot = sub.add_parser("snapshot", help="broker statement figures")
    snap_sub = p_snapshot.add_subparsers(dest="snapshot_command", required=True)
    p_snap_add = snap_sub.add_parser("add", help="record a statement's equity and cash")
    p_snap_add.add_argument("--account", required=True)
    p_snap_add.add_argument("--as-of", required=True, help="ISO-8601 date or timestamp")
    p_snap_add.add_argument("--equity", required=True, help="total equity the broker reports")
    p_snap_add.add_argument("--cash", required=True, help="cash balance the broker reports")
    p_snap_add.add_argument("--note", default=None)
    p_snap_add.set_defaults(fn=cmd_snapshot_add)

    p_reconcile = sub.add_parser(
        "reconcile", help="compare the ledger against a statement snapshot"
    )
    p_reconcile.add_argument("--account", required=True)
    p_reconcile.add_argument("--as-of", default=None, help="ISO-8601; defaults to now")
    p_reconcile.add_argument("--tolerance", default=None, help="default 0.01")
    p_reconcile.set_defaults(fn=cmd_reconcile)

    args = parser.parse_args()
    # `import --commit` no longer requires --account at parse time: whether
    # it's needed depends on whether the parsed file has any row with no
    # account ref to route by, which isn't known until the file is read (see
    # cmd_import). Enforced there instead, before any database connection is
    # opened.

    # A malformed --account UUID is a genuine user-input mistake, same class as
    # a typo'd file path below — but it must be told apart from a domain
    # invariant violation (Fill.__post_init__, group_fills' id=None rejection,
    # instrument_natural_key, CorporateAction.__post_init__) that also happens
    # to raise ValueError. Parsing it here, before the try/except, means the
    # broad `except ValueError` below is never needed (and never added back) to
    # catch it.
    if getattr(args, "account", None):
        try:
            UUID(args.account)
        except ValueError as exc:
            print(f"error: --account is not a valid UUID: {exc}", file=sys.stderr)
            return 2

    # A typo'd file path is the most likely first mistake a user makes; a raw
    # traceback for it is unfriendly and can leak a full local path, so
    # OSError (FileNotFoundError et al.) gets a clean one-line message.
    # Everything else — including every domain invariant violation above, and
    # anything from the database layer — is deliberately left unwrapped, so a
    # real bug surfaces as a full traceback (with line numbers and the
    # offending row) instead of being disguised as a clean user error.
    try:
        return asyncio.run(args.fn(args))
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
