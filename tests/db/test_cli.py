"""CLI tests that need a real database — specifically to exercise cli.cmd_import
itself, not a hand-rolled re-implementation of its transaction pattern."""

from __future__ import annotations

import argparse
from uuid import UUID

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
    """
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_args, **_kwargs):
        return _FakePool(conn)

    async def always_raises(*_args, **_kwargs):
        raise RuntimeError("simulated regroup crash")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    monkeypatch.setattr(cli, "regroup_account", always_raises)

    args = argparse.Namespace(
        venue="fidelity",
        file="tests/fixtures/fidelity/activity.csv",
        account=str(acc),
        commit=True,
    )

    with pytest.raises(RuntimeError, match="simulated regroup crash"):
        await cli.cmd_import(args)

    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


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
    assert "already up to date" in capsys.readouterr().out


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


# --- Blocker pass, item 6: db/migrations/ is empty, so apply() returns []
# --- both when nothing was pending AND on a virgin database that just had
# --- its entire schema created by apply()'s own schema.sql call — those are
# --- different outcomes and must not share the "already up to date" message.


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
    assert "schema applied" in out


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
