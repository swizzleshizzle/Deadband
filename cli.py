"""Deadband CLI. Preview before commit, always."""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from uuid import UUID

from db.accounts import list_accounts
from db.importing import commit_batch
from db.pool import create_pool
from db.trades import list_trades, regroup_account
from importers.registry import get_importer, list_importers


async def cmd_accounts(_args) -> int:
    pool = await create_pool()
    async with pool.acquire() as conn:
        for a in await list_accounts(conn):
            print(f"{a['id']}  {a['venue']:<10} {a['name']:<24} {a['external_ref'] or '-'}")
    await pool.close()
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
        print("\npreview only — rerun with --commit to write")
        return 0

    pool = await create_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            account_id = UUID(args.account)
            result = await commit_batch(conn, account_id, batch)
            written = await regroup_account(conn, account_id)
    await pool.close()

    print(
        f"inserted {result.fills_inserted} fills "
        f"({result.fills_skipped} already present), "
        f"{result.cash_inserted} cash movements, {written} trades regrouped"
    )
    return 0


async def cmd_regroup(args) -> int:
    pool = await create_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            written = await regroup_account(conn, UUID(args.account))
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

    sub.add_parser("accounts").set_defaults(fn=cmd_accounts)

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
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
