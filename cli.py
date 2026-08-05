"""Deadband CLI. Preview before commit, always."""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from uuid import UUID

from db.accounts import UnknownAccountError, create_account, get_account, list_accounts
from db.importing import commit_batch
from db.migrate import apply as apply_migrations
from db.pool import create_pool
from db.trades import list_trades, regroup_account
from importers.registry import get_importer, list_importers


async def cmd_migrate(_args) -> int:
    pool = await create_pool()
    async with pool.acquire() as conn:
        # apply() unconditionally (re-)executes schema.sql, and db/migrations/
        # is currently empty, so `applied` is [] both when nothing was pending
        # AND on a virgin database that just had its entire schema created —
        # those are different outcomes and must not share one message. Check
        # for a table schema.sql creates before calling apply(), while it's
        # still meaningful to ask "did this exist already?".
        existed_before = await conn.fetchval("SELECT to_regclass('public.account') IS NOT NULL")
        applied = await apply_migrations(conn)
    await pool.close()
    if applied:
        print(f"applied {len(applied)} migration(s):")
        for name in applied:
            print(f"  {name}")
    elif not existed_before:
        print("schema applied; no pending migrations")
    else:
        print("already up to date")
    return 0


async def cmd_accounts(_args) -> int:
    pool = await create_pool()
    async with pool.acquire() as conn:
        for a in await list_accounts(conn):
            print(f"{a['id']}  {a['venue']:<10} {a['name']:<24} {a['external_ref'] or '-'}")
    await pool.close()
    return 0


async def cmd_accounts_add(args) -> int:
    pool = await create_pool()
    async with pool.acquire() as conn:
        account_id = await create_account(
            conn,
            name=args.name,
            venue=args.venue,
            account_type=args.account_type,
            default_intent=args.default_intent,
            external_ref=args.external_ref,
        )
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

    # A single export can carry rows for more than one venue account (Fidelity's
    # "Account" column, for instance). Full routing is out of scope here — every
    # row still lands in the one --account given — but silently merging two
    # accounts' history is exactly the kind of thing that must never happen
    # without at least a loud warning.
    external_refs = sorted(
        {f.external_ref for f in batch.fills if f.external_ref}
        | {c.external_ref for c in batch.cash if c.external_ref}
    )
    if len(external_refs) > 1:
        print(
            "  warning: this file mixes multiple account refs "
            f"({', '.join(external_refs)}); all rows will be committed to the "
            "single --account given",
            file=sys.stderr,
        )

    if not args.commit:
        print("\npreview only — rerun with --commit to write")
        return 0

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
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
            async with conn.transaction():
                result = await commit_batch(conn, account_id, batch)
                written = await regroup_account(conn, account_id)
    finally:
        # pool.close() waits for every checked-out connection to be released.
        # It must run after the `async with pool.acquire()` block has exited
        # (or after an early `return` inside it unwound out of that `with`) —
        # never from inside it while the connection returned by acquire() is
        # still held, or close() deadlocks waiting for a release that will
        # never come from a still-open acquire block.
        await pool.close()

    print(
        f"inserted {result.fills_inserted} fills "
        f"({result.fills_skipped} already present), "
        f"{result.cash_inserted} cash movements, {written} trades regrouped"
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
    async with pool.acquire() as conn:
        rows = await list_trades(conn, UUID(args.account) if args.account else None)
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
    p_import.add_argument("--account", help="account UUID (required with --commit)")
    p_import.add_argument("--commit", action="store_true", help="write to the database")
    p_import.set_defaults(fn=cmd_import)

    p_regroup = sub.add_parser("regroup")
    p_regroup.add_argument("--account", required=True)
    p_regroup.set_defaults(fn=cmd_regroup)

    p_trades = sub.add_parser("trades")
    p_trades.add_argument("--account")
    p_trades.set_defaults(fn=cmd_trades)

    args = parser.parse_args()
    if getattr(args, "commit", False) and not args.account:
        parser.error("--commit requires --account")

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
