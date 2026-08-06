"""CLI tests that don't need a database — preview must never even ask for a
connection, and --commit must not be accepted without --account."""

from __future__ import annotations

import argparse

import pytest

import cli

# --- Final fix wave, item 3: `migrate` and `accounts add` must be reachable
# --- from argv, not just from a Python REPL. These tests only prove the
# --- argparse wiring — the DB-backed behaviour is tested in
# --- tests/db/test_cli.py, since these commands need a real connection. -----


def test_migrate_subcommand_routes_to_cmd_migrate(monkeypatch):
    """Fails if `migrate` isn't registered as a subcommand at all (argparse
    would reject it before cmd_migrate is ever reached) or if it's wired to
    the wrong handler."""
    calls = []

    async def fake_cmd_migrate(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(cli, "cmd_migrate", fake_cmd_migrate)
    monkeypatch.setattr("sys.argv", ["deadband", "migrate"])

    rc = cli.main()
    assert rc == 0
    assert len(calls) == 1


def test_accounts_add_parses_all_its_arguments(monkeypatch):
    """Fails if any flag is missing, misspelled, or not wired to
    cmd_accounts_add — the captured namespace would then be missing the
    field or `fn` would still be cmd_accounts."""
    captured = {}

    async def fake_cmd_accounts_add(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli, "cmd_accounts_add", fake_cmd_accounts_add)
    monkeypatch.setattr(
        "sys.argv",
        [
            "deadband",
            "accounts",
            "add",
            "--name",
            "Fidelity Brokerage",
            "--venue",
            "fidelity",
            "--account-type",
            "cash",
            "--external-ref",
            "X12345678",
            "--default-intent",
            "investment",
        ],
    )

    rc = cli.main()
    assert rc == 0
    args = captured["args"]
    assert args.name == "Fidelity Brokerage"
    assert args.venue == "fidelity"
    assert args.account_type == "cash"
    assert args.external_ref == "X12345678"
    assert args.default_intent == "investment"


def test_accounts_add_default_intent_defaults_to_trade(monkeypatch):
    """Fails if --default-intent has no default and argparse instead requires
    it or leaves the attribute unset."""
    captured = {}

    async def fake_cmd_accounts_add(args):
        captured["args"] = args
        return 0

    monkeypatch.setattr(cli, "cmd_accounts_add", fake_cmd_accounts_add)
    monkeypatch.setattr(
        "sys.argv",
        [
            "deadband",
            "accounts",
            "add",
            "--name",
            "Coinbase",
            "--venue",
            "coinbase",
            "--account-type",
            "wallet",
        ],
    )

    rc = cli.main()
    assert rc == 0
    args = captured["args"]
    assert args.default_intent == "trade"
    assert args.external_ref is None


def test_bare_accounts_still_routes_to_the_listing_handler(monkeypatch):
    """The existing bare `accounts` listing behaviour must survive adding the
    `add` subcommand. Fails if adding subparsers to `accounts` broke routing
    for the no-subcommand case."""
    calls = []

    async def fake_cmd_accounts(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(cli, "cmd_accounts", fake_cmd_accounts)
    monkeypatch.setattr("sys.argv", ["deadband", "accounts"])

    rc = cli.main()
    assert rc == 0
    assert len(calls) == 1


# --- Final fix wave, item 5: `except ValueError` used to swallow domain ----
# --- invariant violations (Fill.__post_init__, group_fills, --------------
# --- instrument_natural_key, CorporateAction.__post_init__) and disguise ---
# --- them as a clean one-line message with no traceback. ---------------------


def test_domain_valueerror_is_not_swallowed_by_the_cli_error_handler(monkeypatch):
    """Fails if `except (OSError, ValueError)` is restored around
    asyncio.run(args.fn(args)): main() would then return 2 and print a clean
    "error: ..." line instead of letting this exception propagate."""

    async def raises_domain_error(_args):
        raise ValueError("fill quantity must be positive, got 0")

    monkeypatch.setattr(cli, "cmd_trades", raises_domain_error)
    monkeypatch.setattr("sys.argv", ["deadband", "trades"])

    with pytest.raises(ValueError, match="fill quantity must be positive"):
        cli.main()


def test_malformed_account_uuid_prints_a_clean_error_not_a_traceback(monkeypatch, capsys):
    """A malformed --account is a genuine user-input mistake and must still
    produce a clean one-line message, even though the try/except around
    asyncio.run no longer catches ValueError. Proves the UUID is validated
    explicitly before that point. Fails if the pre-validation is removed: the
    malformed UUID would then raise inside cmd_trades' own `UUID(args.account)`
    call, propagate through asyncio.run uncaught (narrowed to OSError only),
    and this test would error out with an unhandled ValueError instead of
    reaching the assertions."""
    monkeypatch.setattr("sys.argv", ["deadband", "trades", "--account", "not-a-uuid"])

    rc = cli.main()
    assert rc == 2

    err = capsys.readouterr().err
    assert "error:" in err.lower()
    assert "Traceback" not in err


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


def test_commit_without_account_is_rejected(monkeypatch, capsys):
    """Task 4 amendment: whether --account is required depends on whether the
    parsed file has any row with no account ref to route by (a venue like
    Fidelity that carries its own per-row account number needs no --account
    at all) -- that can't be known until the file is read, so it's no longer
    an argparse-time guard (SystemExit before cmd_import even runs). Coinbase
    carries no per-row account ref, so it still needs --account; this is now
    enforced inside cmd_import, returning a non-zero code.

    The old version asserted only the return code, which is satisfiable even
    if the check were moved to run AFTER a database connection is opened —
    that regressed the "no DB connection" guarantee the original SystemExit
    version pinned structurally (a SystemExit from argparse is, by
    construction, before asyncio.run ever starts). Proven the same way
    test_preview_import_never_opens_a_database_connection above does: make
    create_pool blow up if called, and assert the command still returns
    non-zero without ever calling it."""

    async def boom(*_args, **_kwargs):
        raise AssertionError(
            "the --account-required check must run before any database "
            "connection is opened"
        )

    monkeypatch.setattr(cli, "create_pool", boom)
    monkeypatch.setattr(
        "sys.argv",
        ["deadband", "import", "coinbase", "tests/fixtures/coinbase/transactions.csv", "--commit"],
    )
    rc = cli.main()
    assert rc != 0
    assert "account" in capsys.readouterr().err.lower()


async def test_import_warns_when_a_file_mixes_multiple_account_refs(capsys):
    """The Fidelity fixture deliberately carries two account numbers
    (X12345678, X87654321). --commit routes each row to its own account
    automatically (see db.importing.route_batch); preview never opens a
    connection, so this loud, DB-free warning is what stands in for that
    report before a commit is attempted. Fails if the warning is missing, or
    if it's printed but doesn't name both refs."""
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


async def test_preview_names_an_account_whose_rows_are_entirely_unmapped(tmp_path, capsys):
    """The defect this task exists to fix: an account contributing ONLY
    unrecognised-action rows produces zero fills and zero cash, so a report
    derived from fills/cash can never see it -- exactly the account most in
    need of being flagged. Fails if the preview report is derived from
    batch.fills/batch.cash instead of batch.refs_seen (every account ref seen
    in the raw rows, mapped or not)."""
    header = (
        "Run Date,Account Number,Action,Symbol,Description,Quantity,"
        "Price,Commission,Fees,Amount"
    )
    rows = "\n".join(
        [
            header,
            "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
            "01/16/2026,A0000005,SOME BRAND NEW ACTION NOBODY MAPPED,AAA,DESC,,,,,123.45",
            "01/17/2026,A0000005,ANOTHER UNRECOGNISED ACTION,BBB,DESC,,,,,67.89",
        ]
    )
    file_path = tmp_path / "partial.csv"
    file_path.write_text(rows + "\n")

    args = argparse.Namespace(venue="fidelity", file=str(file_path), account=None, commit=False)
    rc = await cli.cmd_import(args)
    assert rc == 0

    err = capsys.readouterr().err
    assert "A0000005" in err


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


# --- Pool-leak fix: every command that opens a pool must close it even if ---
# --- its body raises. cmd_import and cmd_regroup already used try/finally; ---
# --- cmd_migrate, cmd_accounts, cmd_accounts_add and cmd_trades did not. ------


async def test_pool_is_closed_when_the_command_body_raises(monkeypatch):
    """One representative command (cmd_accounts) is enough to prove the
    try/finally shape: if it regresses back to a bare `pool = await
    create_pool() ... await pool.close()`, an exception from list_accounts
    skips the close and this test catches it. Fails today because cmd_accounts
    has no try/finally around the acquire block."""

    class FakeAcquireCM:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *exc_info):
            return False

    class FakePool:
        def __init__(self):
            self.closed = False

        def acquire(self):
            return FakeAcquireCM()

        async def close(self):
            self.closed = True

    fake_pool = FakePool()

    async def fake_create_pool(*_args, **_kwargs):
        return fake_pool

    async def raises(_conn):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    monkeypatch.setattr(cli, "list_accounts", raises)

    with pytest.raises(RuntimeError, match="boom"):
        await cli.cmd_accounts(argparse.Namespace())

    assert fake_pool.closed is True


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
