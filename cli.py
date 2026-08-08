"""Deadband CLI. Preview before commit, always."""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import UTC, datetime
from uuid import UUID

from db.accounts import UnknownAccountError, create_account, get_account, list_accounts
from db.importing import commit_batch, probe_duplicates, route_batch
from db.migrate import apply as apply_migrations
from db.pool import create_pool
from db.trades import list_trades, regroup_account
from importers.base import ImportBatch
from importers.registry import get_importer, list_importers
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
    return await _preview_or_commit(importer.account_venue, batch, args)


async def _preview_or_commit(venue: str, batch: ImportBatch, args) -> int:
    """The three-phase body every entry point (`import`, `sync`) shares.

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

            fills_inserted = fills_skipped = cash_inserted = trades_regrouped = 0
            async with conn.transaction():
                for account_id, sub_batch in targets.items():
                    result = await commit_batch(conn, account_id, sub_batch)
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
        raise SystemExit(2)

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
    return await _preview_or_commit(importer.account_venue, batch, args)


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
