"""CLI tests that don't need a database — preview must never even ask for a
connection, and --commit must not be accepted without --account."""

from __future__ import annotations

import argparse

import pytest

import cli


async def test_preview_import_never_opens_a_database_connection(monkeypatch, capsys):
    """The strongest proof that a preview run writes nothing is structural: it
    must never even acquire a database connection. Verified by making
    create_pool blow up if called — trusting post-hoc row counts instead would
    only catch a regression if PG_DSN happened to point at the test database,
    which it must not be relied upon to do. Fails if cmd_import calls
    commit_batch/create_pool regardless of args.commit."""

    async def boom(*_args, **_kwargs):
        raise AssertionError("preview run must not open a database connection")

    monkeypatch.setattr(cli, "create_pool", boom)

    args = argparse.Namespace(
        venue="coinbase",
        file="tests/fixtures/coinbase/transactions.csv",
        account=None,
        commit=False,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "parsed 3 fills, 2 cash movements" in out
    assert "preview only" in out


def test_commit_without_account_is_rejected(monkeypatch):
    """main() must refuse --commit without --account before ever touching asyncio
    or the database. Fails if that guard is removed or weakened."""
    monkeypatch.setattr(
        "sys.argv",
        ["deadband", "import", "coinbase", "tests/fixtures/coinbase/transactions.csv", "--commit"],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert exc_info.value.code != 0
