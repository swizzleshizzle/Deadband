"""Deadband CLI. Preview before commit, always."""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from uuid import UUID

from db.accounts import UnknownAccountError, create_account, get_account, list_accounts
from db.importing import commit_batch, route_batch
from db.migrate import apply as apply_migrations
from db.pool import create_pool
from db.trades import list_trades, regroup_account
from importers.base import ImportBatch
from importers.registry import get_importer, list_importers


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
        external_refs = sorted(
            {f.external_ref for f in batch.fills if f.external_ref}
            | {c.external_ref for c in batch.cash if c.external_ref}
        )
        if len(external_refs) > 1:
            print(
                "  warning: this file mixes multiple account refs "
                f"({', '.join(external_refs)}); --commit routes each row to "
                "its own account automatically",
                file=sys.stderr,
            )
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
            plan = await route_batch(conn, importer.venue, batch)
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
                if account["venue"] != importer.venue:
                    print(
                        f"error: account {account_id} is a {account['venue']!r} account; "
                        f"refusing to commit a {importer.venue!r} import to it",
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
            for ref in plan.unknown_refs:
                print(f"  {ref}: no matching account", file=sys.stderr)

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
    p_import.set_defaults(fn=cmd_import)

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
