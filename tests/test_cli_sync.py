"""`deadband sync coinbase` — fetch, preview, commit, through the same
preview/commit path `import` uses. No database in these tests; see
tests/db/test_cli.py for the commit-path coverage that needs one.
"""

from __future__ import annotations

import argparse
import json

import pytest


def _args(*, venue, commit, account=None, start=None, end=None):
    """Small namespace helper, same pattern tests/test_cli.py's hand-built
    argparse.Namespace(...) calls use -- a real argparse.Namespace built by
    hand rather than through parser.parse_args()."""
    return argparse.Namespace(venue=venue, account=account, start=start, end=end, commit=commit)


async def test_sync_without_commit_writes_nothing(monkeypatch, capsys):
    """Three-phase discipline: fetch and preview must not touch the DB.

    The brief's draft of this test monkeypatched a `cli._connect` seam that
    does not exist in this codebase. The actual seam every other DB-free CLI
    test in tests/test_cli.py pins is `cli.create_pool` -- e.g.
    test_preview_import_never_opens_a_database_connection and
    test_commit_without_account_is_rejected both make create_pool raise if
    called at all. Using that real seam here instead."""
    import cli

    async def fake_fetch(creds, **kw):
        return json.dumps({"fills": [], "cursor": ""})

    async def boom(*_a, **_k):
        raise AssertionError("sync previewed but opened a DB connection")

    monkeypatch.setattr(cli, "fetch_all_fills", fake_fetch)
    monkeypatch.setattr(cli, "create_pool", boom)
    monkeypatch.setenv("COINBASE_API_KEY", "k")
    monkeypatch.setenv("COINBASE_API_SECRET", "pem")

    rc = await cli.cmd_sync(_args(venue="coinbase", commit=False))
    assert rc == 0
    assert "preview" in capsys.readouterr().out.lower()


async def test_sync_reports_absent_credentials_as_an_error_not_zero_fills(monkeypatch, capsys):
    """Fail loud: swallowing the credentials RuntimeError and printing "0
    fills found" would look identical to a legitimately empty, authenticated
    fetch -- spec §10 gap 5. Fails if cmd_sync catches RuntimeError and
    returns/prints a success-shaped result instead of exiting non-zero."""
    import cli

    monkeypatch.delenv("COINBASE_API_KEY", raising=False)
    monkeypatch.delenv("COINBASE_API_SECRET", raising=False)

    with pytest.raises(SystemExit):
        await cli.cmd_sync(_args(venue="coinbase", commit=False))

    assert "COINBASE_API_KEY" in capsys.readouterr().err


def test_sync_venue_is_wired_to_cmd_sync_in_argparse(monkeypatch):
    """Fails if `sync` isn't registered as a subcommand at all (argparse
    would reject it before cmd_sync is ever reached) or if it's wired to the
    wrong handler -- same shape as test_cli.py's
    test_migrate_subcommand_routes_to_cmd_migrate."""
    import cli

    calls = []

    async def fake_cmd_sync(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(cli, "cmd_sync", fake_cmd_sync)
    monkeypatch.setattr(
        "sys.argv",
        ["deadband", "sync", "coinbase", "--account", "11111111-1111-1111-1111-111111111111"],
    )

    rc = cli.main()
    assert rc == 0
    assert len(calls) == 1
    assert calls[0].venue == "coinbase"


def test_sync_rejects_an_unknown_venue_at_argparse_time(monkeypatch, capsys):
    """Fails if `choices=["coinbase"]` is dropped from the `venue` positional
    -- an unregistered venue would then reach cmd_sync's own `raise
    ValueError` instead of being rejected by argparse before cmd_sync (or
    even asyncio.run) is ever reached."""
    import cli

    monkeypatch.setattr("sys.argv", ["deadband", "sync", "kraken", "--account", "x"])

    with pytest.raises(SystemExit) as exc_info:
        cli.main()

    assert exc_info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
