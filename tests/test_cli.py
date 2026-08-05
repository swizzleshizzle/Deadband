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


async def test_import_warns_when_a_file_mixes_multiple_account_refs(capsys):
    """The Fidelity fixture deliberately carries two account numbers
    (X12345678, X87654321), and routing to per-account ledgers isn't
    implemented — every row still lands under the single --account given. That
    makes a loud warning here the only thing standing between "silent merge"
    and "known limitation." Fails if the warning is missing, or if it's printed
    but doesn't name both refs."""
    args = argparse.Namespace(
        venue="fidelity",
        file="tests/fixtures/fidelity/activity.csv",
        account=None,
        commit=False,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    err = capsys.readouterr().err
    assert "X12345678" in err
    assert "X87654321" in err


async def test_import_does_not_warn_when_a_file_has_a_single_account_ref(capsys):
    """Negative case for the above: a single-account file must not trigger the
    multi-account warning. Fails if the check is inverted or off-by-one (e.g.
    firing on any non-empty external_ref set instead of len > 1)."""
    args = argparse.Namespace(
        venue="coinbase",
        file="tests/fixtures/coinbase/transactions.csv",
        account=None,
        commit=False,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    err = capsys.readouterr().err
    assert "account ref" not in err


def test_import_missing_file_prints_a_clean_error_not_a_traceback(monkeypatch, capsys):
    """A typo'd path is the most likely first user mistake. Fails if main()
    lets FileNotFoundError propagate — the test itself would error out with an
    unhandled traceback instead of reaching the assertions, and rc would never
    be 2."""
    monkeypatch.setattr(
        "sys.argv",
        ["deadband", "import", "coinbase", "tests/fixtures/coinbase/does-not-exist.csv"],
    )
    rc = cli.main()
    assert rc == 2

    err = capsys.readouterr().err
    assert "error:" in err.lower()
    assert "Traceback" not in err
