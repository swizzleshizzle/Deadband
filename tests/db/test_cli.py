"""CLI tests that need a real database — specifically to exercise cli.cmd_import
itself, not a hand-rolled re-implementation of its transaction pattern."""

from __future__ import annotations

import argparse
import pathlib
from uuid import UUID, uuid4

import pytest

import cli
from db.accounts import create_account
from tests.conftest import requires_db

pytestmark = requires_db


class _FakeAcquireCtx:
    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn

    async def __aenter__(self):
        self._pool._checked_out = True
        return self._conn

    async def __aexit__(self, *exc_info):
        self._pool._checked_out = False
        return False


class _FakePool:
    """Stands in for the real asyncpg.Pool returned by db.pool.create_pool, so
    cmd_import runs against the test fixture's own connection (already inside a
    transaction that conftest rolls back at teardown) instead of opening a real
    pool from PG_DSN.

    close() has real semantics for whether a connection is currently checked
    out, unlike the original `pass`-bodied stand-in that could not model the
    real asyncpg.Pool.close() deadlock (it waits for every checked-out
    connection to be released) that cli.py's `await pool.close()` calls
    inside `async with pool.acquire() as conn:` used to trigger — that stub
    let those calls succeed silently instead of catching the bug.
    """

    def __init__(self, conn):
        self._conn = conn
        self._checked_out = False

    def acquire(self):
        return _FakeAcquireCtx(self, self._conn)

    async def close(self):
        if self._checked_out:
            raise AssertionError(
                "Pool.close() called while a connection is still acquired — "
                "this deadlocks against a real asyncpg.Pool, which waits for "
                "every checked-out connection to be released before close() "
                "returns"
            )


async def test_a_crash_during_regroup_leaves_no_fills_through_the_real_cli(conn, monkeypatch):
    """Fix round 1, item 3: the earlier version of this test re-implemented
    cmd_import's `async with conn.transaction():` wrapper inline instead of
    calling cmd_import itself, so it stayed green even if cli.py's own wrapper
    were deleted. This one drives the real cli.cmd_import, with regroup_account
    replaced by something that always raises, and checks the account's fills
    afterward. Fails if cli.py's `async with conn.transaction():` around
    commit_batch + regroup_account (cli.py, cmd_import) is removed: without it,
    commit_batch's inserts are ordinary statements on the connection with no
    surrounding transaction to undo them, so they would still be visible to
    this assertion instead of having been rolled back.

    Verified by temporarily dedenting cli.py's `async with conn.transaction():`
    line (so commit_batch and regroup_account run unwrapped) and re-running:
    the assertion below saw 5 instead of 0, i.e. it failed as expected, before
    the wrapper was restored.

    Task 4 amendment: the fixture spans two accounts (X12345678, X87654321),
    now routed automatically by db.importing.route_batch rather than by a
    single --account -- both must exist as real accounts (matching the
    fixture's account-number column) or the whole commit would instead be
    refused for an unrelated reason (an unknown account ref), never reaching
    regroup_account at all.
    """
    acc1 = await create_account(
        conn, name="T1", venue="fidelity", account_type="cash", external_ref="X12345678"
    )
    acc2 = await create_account(
        conn, name="T2", venue="fidelity", account_type="cash", external_ref="X87654321"
    )

    async def fake_create_pool(*_args, **_kwargs):
        return _FakePool(conn)

    async def always_raises(*_args, **_kwargs):
        raise RuntimeError("simulated regroup crash")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    monkeypatch.setattr(cli, "regroup_account", always_raises)

    args = argparse.Namespace(
        venue="fidelity",
        file="tests/fixtures/fidelity/activity.csv",
        account=None,
        commit=True,
    )

    with pytest.raises(RuntimeError, match="simulated regroup crash"):
        await cli.cmd_import(args)

    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc1) == 0
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc2) == 0


# --- Final fix wave, item 3: the CLI must be able to bootstrap a database and
# --- create an account — previously db.migrate.apply and
# --- db.accounts.create_account were called only from tests, so a fresh
# --- Postgres had no shipped way to obtain the account UUID --commit needs. --


async def test_cmd_migrate_reports_already_up_to_date(conn, monkeypatch, capsys):
    """conftest's `pool`/`conn` fixtures already apply the schema once before
    this test runs, so calling apply() again here has nothing left to do.
    Fails if cmd_migrate doesn't call apply() at all, or ignores its return
    value and always prints the same thing regardless."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_migrate(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "already up to date" in out
    # Pins placement, not just presence: the regroup warning belongs only to
    # the "migrations were just applied" branch. Printing it here too would
    # train the operator to ignore it.
    assert "regroup" not in out.lower()


async def test_cmd_migrate_warns_to_regroup_when_migrations_applied_to_an_existing_database(
    conn, monkeypatch, capsys
):
    """Migration 001 changes how realized_pnl is computed, but a migration
    cannot rewrite rows that already exist under the old convention — only a
    regroup can. `conn` already has a schema applied by the fixtures (i.e.
    this is not a virgin database), so forcing apply() to report something
    applied must produce the regroup warning. Fails if cmd_migrate stays
    silent about stale derived columns after applying migrations."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    async def fake_apply(_conn):
        return ["001_fake.sql"]

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    monkeypatch.setattr(cli, "apply_migrations", fake_apply)

    rc = await cli.cmd_migrate(argparse.Namespace())
    assert rc == 0
    assert "regroup" in capsys.readouterr().out.lower()


async def test_cmd_migrate_does_not_warn_to_regroup_on_a_virgin_database(
    conn, monkeypatch, capsys
):
    """A brand-new database has no pre-existing rows computed under the old
    fee convention -- there is nothing to regroup yet, so telling the
    operator to do so here would be misleading noise. Drops and recreates the
    public schema (rolled back by conftest's `conn` fixture at teardown, same
    as the existing virgin-database test above) to reach that state, then
    runs the real cmd_migrate. Fails if the regroup warning fires even though
    `existed_before` is False."""
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_migrate(argparse.Namespace())
    assert rc == 0
    assert "regroup" not in capsys.readouterr().out.lower()


async def test_cmd_migrate_reports_applied_migration_names(conn, monkeypatch, capsys):
    """Force apply() to report something was applied, proving cmd_migrate
    surfaces apply()'s real return value rather than a canned message. Fails
    if cmd_migrate ignores what apply() returns."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    async def fake_apply(_conn):
        return ["001_fake.sql", "002_fake.sql"]

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    monkeypatch.setattr(cli, "apply_migrations", fake_apply)

    rc = await cli.cmd_migrate(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "001_fake.sql" in out
    assert "002_fake.sql" in out


# --- Blocker pass, item 6: on a virgin database, apply()'s own schema.sql
# --- call creates every table from nothing — that must never be reported as
# --- "already up to date". Originally db/migrations/ was empty too, so
# --- apply() returned [] on a virgin db and cmd_migrate needed a separate
# --- "schema applied; no pending migrations" branch to distinguish that from
# --- a truly up-to-date database. Migration 001 (A-2 ledger completion) is
# --- now the first real migration, so on a virgin db it is pending
# --- immediately after schema.sql creates the tables, and apply() returns it
# --- as applied — exercising the `if applied:` branch instead. Either way,
# --- the one thing that must never happen is "already up to date" on a
# --- database that had zero tables a moment earlier.


async def test_cmd_migrate_reports_schema_created_on_a_virgin_database(conn, monkeypatch, capsys):
    """Drops and recreates the public schema on `conn` (rolled back by
    conftest's `conn` fixture at teardown, same as every other test here) to
    put it in the "never migrated" state a brand-new Postgres would be in,
    then runs the real cmd_migrate against it. Fails if cmd_migrate still says
    "already up to date": that is what it said before this fix, on a database
    that had zero tables a moment earlier."""
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_migrate(argparse.Namespace())
    assert rc == 0
    out = capsys.readouterr().out
    assert "already up to date" not in out
    assert "001_a2_ledger_completion.sql" in out


async def test_cmd_accounts_add_creates_an_account_and_prints_its_id(conn, monkeypatch, capsys):
    """Fails if cmd_accounts_add doesn't actually call create_account, or
    prints something other than the raw UUID (the CLI's only shipped way to
    obtain the id that import --commit requires)."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        name="Fidelity Brokerage",
        venue="fidelity",
        account_type="cash",
        external_ref="X12345678",
        default_intent="investment",
    )
    rc = await cli.cmd_accounts_add(args)
    assert rc == 0

    printed = capsys.readouterr().out.strip()
    account_id = UUID(printed)  # raises if the CLI printed anything else

    row = await conn.fetchrow("SELECT * FROM account WHERE id = $1", account_id)
    assert row["name"] == "Fidelity Brokerage"
    assert row["venue"] == "fidelity"
    assert row["account_type"] == "cash"
    assert row["external_ref"] == "X12345678"
    assert row["default_intent"] == "investment"


# --- Final fix wave, item 4: cmd_import never checked that --account's venue
# --- matches the importer, so `import coinbase cb.csv --account <a-fidelity-
# --- account> --commit` succeeded silently and permanently mis-attributed
# --- Coinbase fills to a Fidelity account, with no CLI path to undo it. -----


async def test_import_refuses_to_commit_to_an_account_of_a_different_venue(
    conn, monkeypatch, capsys
):
    """Fails if the venue check is missing (or backwards): rc would be 0 and
    the fidelity account would end up with committed coinbase fills instead
    of being refused.

    Blocker pass, item 2: this is also the test that must catch cli.py's
    `await pool.close()` deadlock in this exact branch (blocker item 1) — but
    only because _FakePool.close() above now has real "still checked out"
    semantics. Against the un-fixed cli.py (the mismatch branch calling
    `await pool.close()` from inside `async with pool.acquire() as conn:`),
    this test errors out with the fake pool's AssertionError instead of
    reaching `assert rc == 2`, which is exactly what a real asyncpg.Pool would
    do by hanging instead of erroring. Verified: temporarily restored the two
    inner `await pool.close()` calls cli.py had before the item-1 fix and
    reran this test — it failed with `AssertionError: Pool.close() called
    while a connection is still acquired ...` instead of passing."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="coinbase",
        file="tests/fixtures/coinbase/transactions.csv",
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 2

    err = capsys.readouterr().err
    assert "fidelity" in err
    assert "coinbase" in err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_import_commits_when_account_venue_matches(conn, monkeypatch, capsys):
    """Positive case for the venue check above: a matching venue must still
    commit normally. Fails if the check is inverted and rejects the correct
    case instead of the mismatched one."""
    acc = await create_account(conn, name="T", venue="coinbase", account_type="wallet")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="coinbase",
        file="tests/fixtures/coinbase/transactions.csv",
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 3


# --- Item 7: cmd_regroup must surface an unknown account the same clean way
# --- cmd_import already does, not as a raw ValueError('None is not a valid
# --- TradeIntent') traceback that never names the account. -----------------


async def test_regroup_unknown_account_prints_a_clean_error_not_a_traceback(
    conn, monkeypatch, capsys
):
    """Fails if cmd_regroup lets UnknownAccountError propagate uncaught: this
    test would then error out with an unhandled exception instead of reaching
    `assert rc == 2`, and nothing would be printed to stderr naming the
    account id."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    bogus = uuid4()
    args = argparse.Namespace(account=str(bogus))
    rc = await cli.cmd_regroup(args)
    assert rc == 2

    err = capsys.readouterr().err
    assert "error:" in err.lower()
    assert str(bogus) in err
    assert "Traceback" not in err


# --- Task 4: --commit routes rows by account through the real CLI -----------

_ROUTING_HEADER = (
    "Run Date,Account Number,Action,Symbol,Description,Quantity,Price,Commission,Fees,Amount"
)


def _write_routing_csv(tmp_path: pathlib.Path, *rows: str) -> str:
    path = tmp_path / "routed.csv"
    path.write_text("\n".join([_ROUTING_HEADER, *rows]) + "\n")
    return str(path)


async def test_commit_refuses_and_writes_nothing_when_a_row_routes_to_an_unknown_account(
    conn, monkeypatch, capsys, tmp_path
):
    """Partial commits are not acceptable -- a silently-skipped account looks
    like a successful import. One row routes to a known account, the other to
    an account that doesn't exist; the whole commit must be refused and
    NOTHING written, including the row that routed fine."""
    known = await create_account(
        conn, name="known", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    file_path = _write_routing_csv(
        tmp_path,
        "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
        "01/16/2026,A0000009,YOU BOUGHT,VTI,VANGUARD TOTAL STOCK "
        "MARKET ETF,1,100.00,0.00,0.00,-100.00",
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(venue="fidelity", file=file_path, account=None, commit=True)
    rc = await cli.cmd_import(args)

    assert rc != 0
    err = capsys.readouterr().err
    assert "A0000009" in err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", known) == 0


async def test_commit_refuses_and_writes_nothing_when_a_row_carries_money_and_is_unmapped(
    conn, monkeypatch, capsys, tmp_path
):
    """Same atomicity guarantee as the unknown-account case above, for
    ImportBatch.blocking: one row is a normal, known-account fill; the other
    is an unmapped action carrying real money (a non-zero Amount) that no
    rule matches. The whole commit must be refused and NOTHING written,
    including the row that classified fine -- a partial commit here would
    look like a successful import while quietly dropping money on the floor,
    which is the exact defect this task exists to make impossible."""
    known = await create_account(
        conn, name="known", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    file_path = _write_routing_csv(
        tmp_path,
        "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
        "01/16/2026,A0000001,MYSTERIOUS NEW ACTION,AAA,DESC,,,,,123.45",
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(venue="fidelity", file=file_path, account=None, commit=True)
    rc = await cli.cmd_import(args)

    assert rc != 0
    err = capsys.readouterr().err
    assert "MYSTERIOUS" in err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", known) == 0


async def test_ignored_account_is_skipped_while_its_siblings_import(
    conn, monkeypatch, capsys, tmp_path
):
    """An account with ignore_on_import=True must route SUCCESSFULLY and be
    skipped -- reported as skipped, not unknown -- while its siblings in the
    same file still commit normally. Without this, a deliberately-excluded
    account (e.g. a retirement plan with no instrument identity) would make
    every import of the file fail permanently."""
    active = await create_account(
        conn, name="active", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    plan_account = await create_account(
        conn,
        name="plan",
        venue="fidelity",
        account_type="cash",
        external_ref="A0000003",
        ignore_on_import=True,
    )
    file_path = _write_routing_csv(
        tmp_path,
        "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
        "01/16/2026,A0000003,YOU BOUGHT,VTI,VANGUARD TOTAL STOCK "
        "MARKET ETF,1,100.00,0.00,0.00,-100.00",
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(venue="fidelity", file=file_path, account=None, commit=True)
    rc = await cli.cmd_import(args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out.lower()
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", active) == 1
    assert (
        await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", plan_account) == 0
    )


async def test_commit_state_report_names_an_account_whose_rows_are_entirely_unmapped(
    conn, monkeypatch, capsys, tmp_path
):
    """Same defect as the preview-level test in tests/test_cli.py, on the
    --commit path's own state report: route_batch only ever sees refs that
    appear on a fill or cash movement, so an account contributing ONLY
    unrecognised-action rows never reaches it -- absent from by_account,
    unknown_refs and ignored_refs alike. It must still be named, from
    batch.refs_seen, or the commit reports success while a real account is
    silently missing. Not a refusal case -- there is nothing of this
    account's to write, so the known account must still commit normally."""
    known = await create_account(
        conn, name="known", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    # Task 5 amendment: this row must carry NO financial content (blank
    # Amount, not the 123.45 the pre-task-5 version of this test used) -- an
    # unmapped row that carries money now REFUSES the whole commit (see
    # importers/base.py's ImportBatch.blocking), which is a different, correct
    # outcome this test does not exist to cover. A blank Amount keeps this
    # test's actual point intact: an account whose rows are entirely
    # unmapped, but carry no money, must still be named while its known
    # sibling commits normally.
    file_path = _write_routing_csv(
        tmp_path,
        "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
        "01/16/2026,A0000005,SOME BRAND NEW ACTION NOBODY MAPPED,AAA,DESC,,,,,",
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(venue="fidelity", file=file_path, account=None, commit=True)
    rc = await cli.cmd_import(args)

    assert rc == 0
    combined = "".join(capsys.readouterr())
    assert "A0000005" in combined
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", known) == 1


# --- Fix round 1: --ignore-on-import needs a CLI path, not just a DB column -


async def test_cmd_accounts_add_ignore_on_import_flag_creates_a_skippable_account(
    conn, monkeypatch, capsys, tmp_path
):
    """--commit refuses on an unknown ref, which is exactly the trap
    ignore_on_import exists to escape -- but the escape hatch was only
    reachable from the test suite or hand-written SQL until `accounts add`
    could set it. Proves the flag end to end: create the account with
    --ignore-on-import, then import a file referencing it and confirm it
    routes as skipped, not unknown, while a sibling account still commits."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    add_args = argparse.Namespace(
        name="Retirement Plan",
        venue="fidelity",
        account_type="cash",
        external_ref="A0000003",
        default_intent="investment",
        ignore_on_import=True,
    )
    rc = await cli.cmd_accounts_add(add_args)
    assert rc == 0
    account_id = UUID(capsys.readouterr().out.strip())

    row = await conn.fetchrow("SELECT ignore_on_import FROM account WHERE id = $1", account_id)
    assert row["ignore_on_import"] is True

    file_path = _write_routing_csv(
        tmp_path,
        "01/16/2026,A0000003,YOU BOUGHT,VTI,VANGUARD TOTAL STOCK "
        "MARKET ETF,1,100.00,0.00,0.00,-100.00",
    )
    import_args = argparse.Namespace(venue="fidelity", file=file_path, account=None, commit=True)
    rc = await cli.cmd_import(import_args)

    assert rc == 0
    out = capsys.readouterr().out
    assert "skipped" in out.lower()
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", account_id) == 0


# --- C1: ignore_on_import must be an escape hatch from blocking, not just ---
# --- from routing. blocking used to be checked BEFORE route_batch ever ran,
# --- and carried no account attribution at all -- so a money-carrying
# --- unmapped row belonging to an ignore_on_import account refused the
# --- ENTIRE import, permanently, exactly the retirement-plan scenario the
# --- flag exists to escape.


async def test_ignored_accounts_money_carrying_unmapped_rows_do_not_block_the_import(
    conn, monkeypatch, capsys, tmp_path
):
    """The plan account's only row is an unrecognised action carrying money
    (a non-zero Amount) -- before the fix this refused the whole commit even
    though the account is registered ignore_on_import. Its row also
    contributes NOTHING to fills/cash (every action on it is unrecognised),
    so its ref never reaches route_batch through fills/cash at all -- this
    only passes if blocking's own ref is also considered when deciding
    ignored/unknown/routable, not just fills/cash refs."""
    active = await create_account(
        conn, name="active", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    await create_account(
        conn,
        name="plan",
        venue="fidelity",
        account_type="cash",
        external_ref="A0000003",
        ignore_on_import=True,
    )
    file_path = _write_routing_csv(
        tmp_path,
        "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
        "01/16/2026,A0000003,MYSTERIOUS PLAN ACTION,,DESC,,,,,123.45",
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(venue="fidelity", file=file_path, account=None, commit=True)
    rc = await cli.cmd_import(args)

    assert rc == 0, capsys.readouterr().err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", active) == 1


# --- Task 6: --check-duplicates is an explicit, opt-in probe on the preview --
# --- path. Preview's structural no-connection guarantee is proven separately
# --- in tests/test_cli.py's test_preview_import_never_opens_a_database_
# --- connection, which passes a Namespace with no check_duplicates attribute
# --- at all -- these tests instead exercise the flag itself, through a real
# --- (fake-pooled) connection.


async def test_check_duplicates_reports_an_existing_fill_and_writes_nothing(
    conn, monkeypatch, capsys, tmp_path
):
    """A fill already committed to the account must be reported as a
    duplicate by a preview run with --check-duplicates -- and that preview
    must still refuse to write (still prints "preview only", still leaves
    the fill table untouched)."""
    known = await create_account(
        conn, name="known", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    file_path = _write_routing_csv(
        tmp_path,
        "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    commit_args = argparse.Namespace(
        venue="fidelity", file=file_path, account=None, commit=True, check_duplicates=False
    )
    rc = await cli.cmd_import(commit_args)
    assert rc == 0
    before = await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", known)
    assert before == 1

    preview_args = argparse.Namespace(
        venue="fidelity", file=file_path, account=None, commit=False, check_duplicates=True
    )
    rc = await cli.cmd_import(preview_args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "preview only" in out
    assert "duplicate check: 1 fill(s), 0 cash movement(s) already present" in out

    after = await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", known)
    assert after == before == 1


async def test_check_duplicates_uses_explicit_account_for_unrouted_rows(
    conn, monkeypatch, capsys
):
    """Coinbase carries no per-row account ref, so a preview run with
    --check-duplicates must fall back to --account for those rows the same
    way --commit already does (cmd_import's `unrouted` handling)."""
    acc = await create_account(conn, name="T", venue="coinbase", account_type="wallet")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    commit_args = argparse.Namespace(
        venue="coinbase",
        file="tests/fixtures/coinbase/transactions.csv",
        account=str(acc),
        commit=True,
        check_duplicates=False,
    )
    rc = await cli.cmd_import(commit_args)
    assert rc == 0

    preview_args = argparse.Namespace(
        venue="coinbase",
        file="tests/fixtures/coinbase/transactions.csv",
        account=str(acc),
        commit=False,
        check_duplicates=True,
    )
    rc = await cli.cmd_import(preview_args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "preview only" in out
    assert "duplicate check: 3 fill(s), 2 cash movement(s) already present" in out
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 3
