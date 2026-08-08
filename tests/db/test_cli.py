"""CLI tests that need a real database — specifically to exercise cli.cmd_import
itself, not a hand-rolled re-implementation of its transaction pattern."""

from __future__ import annotations

import argparse
import pathlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

import cli
from db.accounts import create_account
from db.instruments import upsert_instrument
from db.marks import latest_marks
from ledger.types import AssetClass, Instrument
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


def _coinbase_fixture_without_the_unmapped_convert_row(tmp_path: pathlib.Path) -> str:
    """The shipped fixture (tests/fixtures/coinbase/transactions.csv, which
    must not be modified -- see the fix-wave constraints) carries an
    unmapped "Convert" row that carries real money. Since I4 wired Coinbase's
    blocking policy, --commit against the real fixture now correctly refuses
    -- see test_the_shipped_fixtures_unmapped_convert_row_blocks_the_commit
    in tests/test_coinbase.py. The tests below exist to pin OTHER behaviour
    (the venue-mismatch guard, --check-duplicates' account fallback) and
    would otherwise be blocked by an unrelated row; this writes a trimmed
    copy (real fixture minus its last, Convert, line) to tmp_path so they
    keep exercising what they were written for."""
    lines = pathlib.Path("tests/fixtures/coinbase/transactions.csv").read_text().splitlines()
    assert lines[-1].startswith("2026-03-15T16:45:00Z,Convert,"), (
        "the shipped fixture's shape changed -- update this trim"
    )
    trimmed = tmp_path / "transactions_no_convert.csv"
    trimmed.write_text("\n".join(lines[:-1]) + "\n")
    return str(trimmed)


# --- Final fix wave, item 4: cmd_import never checked that --account's venue
# --- matches the importer, so `import coinbase cb.csv --account <a-fidelity-
# --- account> --commit` succeeded silently and permanently mis-attributed
# --- Coinbase fills to a Fidelity account, with no CLI path to undo it. -----


async def test_import_refuses_to_commit_to_an_account_of_a_different_venue(
    conn, monkeypatch, capsys, tmp_path
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
        file=_coinbase_fixture_without_the_unmapped_convert_row(tmp_path),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 2

    err = capsys.readouterr().err
    assert "fidelity" in err
    assert "coinbase" in err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_import_commits_when_account_venue_matches(conn, monkeypatch, capsys, tmp_path):
    """Positive case for the venue check above: a matching venue must still
    commit normally. Fails if the check is inverted and rejects the correct
    case instead of the mismatched one.

    §10 gap 6, closed 2026-08-08: used to assert 3 committed fills (this
    trimmed fixture's two Buys and one Sell). Coinbase fills come only from
    the API now, so this venue-matching commit inserts zero fills -- the
    trade rows are reported and skipped, not blocked (there's no unmapped
    or blocking row left once the Convert line is trimmed) -- and the two
    cash rows (Deposit, Rewards Income) still commit normally."""
    acc = await create_account(conn, name="T", venue="coinbase", account_type="wallet")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="coinbase",
        file=_coinbase_fixture_without_the_unmapped_convert_row(tmp_path),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0
    assert (
        await conn.fetchval("SELECT count(*) FROM cash_movement WHERE account_id = $1", acc) == 2
    )


# --- Task 5: `deadband sync coinbase` must reuse this exact venue-match/
# --- routing check, not a parallel copy of it -- and the check must not
# --- mistake the PARSING importer's own identity ("coinbase-api") for the
# --- venue accounts are registered under ("coinbase"). Before cmd_sync
# --- passed "coinbase" (not get_importer("coinbase-api").venue) into the
# --- shared preview/commit body, this would refuse every real account with
# --- "account ... is a 'coinbase' account; refusing to commit a
# --- 'coinbase-api' import to it" -- sync could never commit anything, ever,
# --- against any account a user could actually create.
# ---
# --- Review follow-up: that fix relocated the literal "coinbase" from
# --- cmd_sync's own body into a caller-supplied argument, which still left
# --- four places (argparse choices, a now-dead venue check, get_importer's
# --- name, and the _preview_or_commit call) that had to agree with nothing
# --- forcing them to. Importer.account_venue (importers/base.py) makes the
# --- importer itself the one source of truth: cmd_sync now passes
# --- importer.account_venue, never a literal. The two tests below pin that
# --- routing/matching genuinely reads account_venue rather than a
# --- resurrected literal -- see the mutation note on the second test. ------


async def test_sync_commits_fills_to_a_real_coinbase_venue_account(conn, monkeypatch, capsys):
    """End-to-end proof that `sync coinbase --commit` reaches a real account:
    fetch (faked) -> parse -> the shared preview/commit body -> insert. Fails
    if cmd_sync compares the account's venue against the coinbase-api
    importer's own `.venue` ("coinbase-api") instead of the plain "coinbase"
    accounts are actually registered under -- that mismatch would print the
    "refusing to commit" error below and rc would be 2, with zero fills
    inserted, for every account this test (or a real user) could construct."""
    acc = await create_account(conn, name="T", venue="coinbase", account_type="wallet")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    async def fake_fetch(creds, **kw):
        import json

        return json.dumps(
            {
                "fills": [
                    {
                        "trade_id": "sync-t1",
                        "order_id": "sync-o1",
                        "trade_time": "2026-06-01T00:00:00Z",
                        "price": "100.00",
                        "size": "2",
                        "size_in_quote": False,
                        "commission": "0.10",
                        "product_id": "BTC-USD",
                        "side": "BUY",
                    }
                ],
                "cursor": "",
            }
        )

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    monkeypatch.setattr(cli, "fetch_all_fills", fake_fetch)
    monkeypatch.setenv("COINBASE_API_KEY", "k")
    monkeypatch.setenv("COINBASE_API_SECRET", "pem")

    args = argparse.Namespace(
        venue="coinbase", account=str(acc), start=None, end=None, commit=True
    )
    rc = await cli.cmd_sync(args)

    err = capsys.readouterr().err
    assert rc == 0, err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 1
    # I2: the fill must record where it actually came from. commit_batch's
    # `source` used to default to "csv" and cli.py's shared commit path never
    # overrode it, so every API-synced fill claimed CSV provenance -- and
    # fill.source is the ONLY column that can answer "which of my Coinbase
    # fills came from the retired CSV path?", which is exactly what the
    # mixed-provenance refusal below and any future reconciliation must ask.
    # This test previously asserted the row existed and never looked at it.
    assert (
        await conn.fetchval("SELECT source FROM fill WHERE account_id = $1", acc) == "api"
    )


async def test_sync_refuses_to_commit_to_an_account_of_a_different_venue(
    conn, monkeypatch, capsys
):
    """Negative twin of the test above: account_venue routing must still
    REFUSE a genuine mismatch, not just permit the matching case. Fails if
    the account-venue check is ever dropped entirely rather than merely
    switched to the wrong attribute -- rc would be 0 and the fidelity
    account would end up with a Coinbase fill committed to it, with no CLI
    path to undo it.

    Mutation gate for the review finding this pair of tests exists to close:
    with `CoinbaseAPIImporter.account_venue` temporarily set back to
    "coinbase-api" (the importer's own identity, the pre-fix behaviour),
    this test still passes (a mismatch is still refused -- for the wrong
    reason, "fidelity" != "coinbase-api" instead of "fidelity" !=
    "coinbase", but still refused) while
    test_sync_commits_fills_to_a_real_coinbase_venue_account above goes red
    (rc == 2, zero fills, "account ... is a 'coinbase' account; refusing to
    commit a 'coinbase-api' import to it" -- the exact pre-fix failure).
    Verified by hand: with account_venue reverted, this test passed and the
    positive twin failed with rc == 2 and zero fills, exactly as described."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    async def fake_fetch(creds, **kw):
        import json

        return json.dumps(
            {
                "fills": [
                    {
                        "trade_id": "sync-mismatch-t1",
                        "order_id": "sync-mismatch-o1",
                        "trade_time": "2026-06-01T00:00:00Z",
                        "price": "100.00",
                        "size": "2",
                        "size_in_quote": False,
                        "commission": "0.10",
                        "product_id": "BTC-USD",
                        "side": "BUY",
                    }
                ],
                "cursor": "",
            }
        )

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    monkeypatch.setattr(cli, "fetch_all_fills", fake_fetch)
    monkeypatch.setenv("COINBASE_API_KEY", "k")
    monkeypatch.setenv("COINBASE_API_SECRET", "pem")

    args = argparse.Namespace(
        venue="coinbase", account=str(acc), start=None, end=None, commit=True
    )
    rc = await cli.cmd_sync(args)

    err = capsys.readouterr().err
    assert rc == 2
    assert "fidelity" in err
    assert "coinbase" in err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


# --- I3: an account holding pre-cut-over CSV fills (content_hash-keyed, no
# --- venue_fill_id) must never take a venue_fill_id-keyed batch on top. The
# --- two partial unique indexes are disjoint BY CONSTRUCTION --
# --- fill_venue_id_uniq is WHERE venue_fill_id IS NOT NULL,
# --- fill_content_hash_uniq is WHERE content_hash IS NOT NULL -- so the same
# --- trade arriving by both paths is invisible to both indexes, both rows
# --- land, both feed regroup_account, and position and realized P&L double.
# --- Nothing else in the system would notice. -------------------------------


async def _sync_args_and_fetch(monkeypatch, conn, acc, *, trade_id):
    """Wire cmd_sync to this test's connection and a one-fill fake fetch."""
    import json

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    async def fake_fetch(creds, **kw):
        return json.dumps(
            {
                "fills": [
                    {
                        "trade_id": trade_id,
                        "order_id": "o-" + trade_id,
                        "trade_time": "2026-06-01T00:00:00Z",
                        "price": "100.00",
                        "size": "2",
                        "size_in_quote": False,
                        "commission": "0.10",
                        "product_id": "BTC-USD",
                        "side": "BUY",
                    }
                ],
                "cursor": "",
            }
        )

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    monkeypatch.setattr(cli, "fetch_all_fills", fake_fetch)
    monkeypatch.setenv("COINBASE_API_KEY", "k")
    monkeypatch.setenv("COINBASE_API_SECRET", "pem")
    return argparse.Namespace(
        venue="coinbase", account=str(acc), start=None, end=None, commit=True
    )


async def test_sync_refuses_an_account_that_already_holds_csv_keyed_fills(
    conn, monkeypatch, capsys
):
    """The account already has one CSV-imported Coinbase fill -- exactly the
    shape commit_batch produces for a fill with no venue id: content_hash
    SET, venue_fill_id NULL. `sync --commit` must refuse rather than add a
    second, independently-keyed row for what may be the same trade.

    Committed through cmd_import (the real CSV path) rather than by an
    INSERT, so the pre-existing row's key shape is whatever the production
    code actually writes, not whatever this test assumes it writes.
    """
    acc = await create_account(conn, name="T", venue="coinbase", account_type="wallet")

    from datetime import UTC, datetime
    from decimal import Decimal

    from db.importing import commit_batch
    from importers.base import CanonicalFill, ImportBatch
    from ledger.types import AssetClass, Instrument, Side

    legacy = CanonicalFill(
        instrument=Instrument(
            id=None,
            asset_class=AssetClass.CRYPTO_SPOT,
            symbol="BTC",
            quote_currency="USD",
        ),
        executed_at=datetime(2026, 6, 1, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("2"),
        price=Decimal("100.00"),
        fee=Decimal("0.10"),
        fee_currency="USD",
    )
    await commit_batch(conn, acc, ImportBatch(fills=(legacy,)), source="csv")
    assert (
        await conn.fetchval(
            "SELECT count(*) FROM fill WHERE account_id = $1 AND content_hash IS NOT NULL "
            "AND venue_fill_id IS NULL",
            acc,
        )
        == 1
    )

    args = await _sync_args_and_fetch(monkeypatch, conn, acc, trade_id="mixed-t1")
    rc = await cli.cmd_sync(args)

    err = capsys.readouterr().err
    assert rc == 2, err
    assert "content_hash" in err
    assert str(acc) in err
    # Refused means WROTE NOTHING: the legacy fill is still the only one.
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 1


async def test_sync_stays_silent_on_an_account_with_no_csv_keyed_fills(
    conn, monkeypatch, capsys
):
    """The negative twin, and the one that matters most: a guard that refused
    unconditionally would satisfy the test above while breaking `sync` for
    every clean account -- which is every account anyone would create today.

    Two consecutive syncs, not one: the second proves the guard does not
    start firing once the account holds API-keyed fills of its own. Those
    rows have venue_fill_id SET and content_hash NULL, so they must not match
    the `content_hash IS NOT NULL AND venue_fill_id IS NULL` predicate -- a
    guard written with the two conditions swapped, or with either dropped,
    would pass on the first sync and refuse on the second.
    """
    acc = await create_account(conn, name="T", venue="coinbase", account_type="wallet")

    args = await _sync_args_and_fetch(monkeypatch, conn, acc, trade_id="clean-t1")
    rc = await cli.cmd_sync(args)
    assert rc == 0, capsys.readouterr().err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 1

    args = await _sync_args_and_fetch(monkeypatch, conn, acc, trade_id="clean-t2")
    rc = await cli.cmd_sync(args)
    assert rc == 0, capsys.readouterr().err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 2


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


# --- Finding F: an UNREGISTERED account contributing only non-financial ----
# --- unmapped rows was invisible to classification, not just unreported. ---
#
# route_batch built its ref set from fills, cash, and blocking only --
# batch.refs_seen (every ref seen in the raw rows) never fed it. An account
# whose rows are ALL unmapped and non-financial therefore never reached
# route_batch's account lookup at all: not unknown_refs, not ignored_refs,
# nothing -- silently absent from the whole classification and from
# cli.py's report. The external reviewer proposed making this REFUSE the
# commit; that was rejected -- it reintroduces the over-block trap A2-6
# exists to avoid (one stray boilerplate row on an unregistered account
# refusing every import forever). The fix instead completes the
# CLASSIFICATION (reported as "unknown") without touching refusal, which
# stays keyed on money alone.


async def test_an_unregistered_account_with_only_non_financial_rows_is_reported_unknown_not_refused(
    conn, monkeypatch, capsys, tmp_path
):
    """Two accounts in one file: A0000001 is registered and gets a normal
    fill. A0000009 is NOT registered, and its only row is an unrecognised
    action with no quantity and no amount -- no fill, no cash, no blocking
    reason, so before the fix it was invisible to route_batch entirely.
    After the fix it must be reported as unknown (not silently absent, not
    the generic "0 row(s) mapped" message that doesn't say whether the
    account even exists) -- and the commit must still succeed, since nothing
    about A0000009's row carries any money at stake."""
    known = await create_account(
        conn, name="known", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    file_path = _write_routing_csv(
        tmp_path,
        "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
        "01/16/2026,A0000009,SOME BRAND NEW ACTION NOBODY MAPPED,AAA,DESC,,,,,",
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(venue="fidelity", file=file_path, account=None, commit=True)
    rc = await cli.cmd_import(args)

    assert rc == 0, capsys.readouterr().err
    err = capsys.readouterr().err
    assert "A0000009" in err
    assert "no matching account" in err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", known) == 1


async def test_an_unregistered_account_with_a_money_carrying_row_still_refuses_the_commit(
    conn, monkeypatch, capsys, tmp_path
):
    """The other direction, pinned end to end: A0000009 is still
    unregistered, but this time its unmapped row DOES carry money (a real
    Amount). The commit must still refuse, and write NOTHING -- not even the
    known account's otherwise-good row -- exactly as before this fix."""
    known = await create_account(
        conn, name="known", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    file_path = _write_routing_csv(
        tmp_path,
        "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
        "01/16/2026,A0000009,SOME BRAND NEW ACTION NOBODY MAPPED,AAA,DESC,,,,,999.00",
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(venue="fidelity", file=file_path, account=None, commit=True)
    rc = await cli.cmd_import(args)

    assert rc != 0
    err = capsys.readouterr().err
    assert "unmapped row(s) carry money" in err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", known) == 0


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
    conn, monkeypatch, capsys, tmp_path
):
    """Coinbase carries no per-row account ref, so a preview run with
    --check-duplicates must fall back to --account for those rows the same
    way --commit already does (cmd_import's `unrouted` handling).

    §10 gap 6, closed 2026-08-08: used to assert "3 fill(s) ... already
    present" and 3 committed fills. Coinbase fills come only from the API
    now, so the prior --commit of this trimmed fixture inserted zero fills
    -- there is nothing for the duplicate probe to find on the fill side.
    The cash side is unaffected and still the thing worth pinning here: 2
    cash movements were already committed, so the probe must still report
    them as already present."""
    acc = await create_account(conn, name="T", venue="coinbase", account_type="wallet")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    fixture_path = _coinbase_fixture_without_the_unmapped_convert_row(tmp_path)
    commit_args = argparse.Namespace(
        venue="coinbase",
        file=fixture_path,
        account=str(acc),
        commit=True,
        check_duplicates=False,
    )
    rc = await cli.cmd_import(commit_args)
    assert rc == 0

    preview_args = argparse.Namespace(
        venue="coinbase",
        file=fixture_path,
        account=str(acc),
        commit=False,
        check_duplicates=True,
    )
    rc = await cli.cmd_import(preview_args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "preview only" in out
    assert "duplicate check: 0 fill(s), 2 cash movement(s) already present" in out
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


# --- Finding C: --check-duplicates could print "0 duplicates" for a file it -
# --- never actually validated -- an unknown account ref was simply omitted --
# --- from the probe, the same shape --commit refuses outright for. ----------


async def test_check_duplicates_refuses_a_bare_count_when_a_row_routes_to_an_unknown_account(
    conn, monkeypatch, capsys, tmp_path
):
    """Before the fix, a row whose account ref matches no registered account
    was simply never probed (route_batch's by_account has nothing for it),
    and --check-duplicates printed a plausible-looking "duplicate check: 0
    fill(s), 0 cash movement(s)" -- as if the whole file had been checked --
    while --commit against the identical file refuses outright. The probe
    must not disagree with --commit about whether the file is even routable:
    it must refuse (non-zero, no bare count) exactly where --commit would."""
    file_path = _write_routing_csv(
        tmp_path,
        "01/15/2026,A0000009,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity", file=file_path, account=None, commit=False, check_duplicates=True
    )
    rc = await cli.cmd_import(args)
    assert rc != 0

    captured = capsys.readouterr()
    assert "A0000009" in captured.err
    assert "duplicate check:" not in captured.out, (
        "must not print a count that silently omits the unprobed row"
    )


# --- A2 part2b2, Task 4: `deadband marks set` -------------------------------
#
# Task 3's review flagged a gap latest_marks/set_mark themselves cannot close:
# nothing rejected a future-dated mark, and the clock is deliberately absent
# from db/marks.py and ledger/. This CLI command is the only layer that can
# see "now", so the future-date guard lives here (cli.cmd_marks_set), not in
# the db layer.


def _equity(symbol: str, quote_currency: str = "USD") -> Instrument:
    return Instrument(
        id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency=quote_currency
    )


@pytest_asyncio.fixture
async def an_instrument_named_zxco(conn):
    """A single instrument this test file owns, isolated by the rolled-back
    `conn` transaction from tests/conftest.py -- it never persists."""
    return await upsert_instrument(conn, _equity("ZXCO"))


@pytest_asyncio.fixture
async def two_same_symbol(conn):
    """Two instruments sharing a symbol but not a natural_key -- the same
    ticker quoted in two different currencies."""
    a = await upsert_instrument(conn, _equity("DUPE", "USD"))
    b = await upsert_instrument(conn, _equity("DUPE", "EUR"))
    return a, b


def _args(*, symbol=None, natural_key=None, price, as_of=None):
    """Small namespace helper, same pattern tests/test_cli_sync.py's `_args`
    and tests/test_cli.py's hand-built argparse.Namespace(...) calls use -- a
    real argparse.Namespace built by hand rather than through
    parser.parse_args()."""
    return argparse.Namespace(symbol=symbol, natural_key=natural_key, price=price, as_of=as_of)


async def test_marks_set_records_a_price(conn, monkeypatch, an_instrument_named_zxco, capsys):
    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_marks_set(
        _args(symbol="ZXCO", price="24.50", as_of="2026-08-08T12:00:00+00:00")
    )
    assert rc == 0, capsys.readouterr().err
    assert (await latest_marks(conn, [an_instrument_named_zxco]))[an_instrument_named_zxco][
        0
    ] == Decimal("24.50")


async def test_marks_set_refuses_an_ambiguous_symbol_without_writing(
    conn, monkeypatch, two_same_symbol, capsys
):
    """The refusal must happen before any write -- a partially applied mark
    is worse than none."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_marks_set(_args(symbol="DUPE", price="1"))
    assert rc == 2
    assert "natural-key" in capsys.readouterr().err
    for iid in two_same_symbol:
        assert await latest_marks(conn, [iid]) == {}


def test_marks_set_requires_exactly_one_of_symbol_or_natural_key(monkeypatch):
    """Real argparse-level assertion, driven the way every other DB-free
    parser test in tests/test_cli.py is: monkeypatch sys.argv and call
    cli.main() directly -- there is no cli.main_with_argv helper in this
    codebase."""
    monkeypatch.setattr("sys.argv", ["deadband", "marks", "set", "--price", "1"])
    with pytest.raises(SystemExit):
        cli.main()


async def test_marks_set_defaults_as_of_to_now_when_omitted(
    conn, monkeypatch, an_instrument_named_zxco, capsys
):
    """The clock lives in the CLI, not in db/marks.py -- confirms cmd_marks_set
    itself supplies datetime.now(UTC) when --as-of is absent, rather than
    passing None through to set_mark (which would crash on
    naive.tzinfo is None)."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_marks_set(_args(symbol="ZXCO", price="5"))
    assert rc == 0, capsys.readouterr().err
    assert (await latest_marks(conn, [an_instrument_named_zxco]))[an_instrument_named_zxco][
        0
    ] == Decimal("5")


async def test_marks_set_accepts_an_as_of_slightly_in_the_past_or_now(
    conn, monkeypatch, an_instrument_named_zxco, capsys
):
    """Pins the accepting side of the future-date guard's boundary: a mark
    dated a few seconds ago (well inside the tolerance, and never actually in
    the future) must not be refused."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    as_of = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    rc = await cli.cmd_marks_set(_args(symbol="ZXCO", price="7", as_of=as_of))
    assert rc == 0, capsys.readouterr().err
    assert (await latest_marks(conn, [an_instrument_named_zxco]))[an_instrument_named_zxco][
        0
    ] == Decimal("7")


async def test_marks_set_refuses_a_clearly_future_dated_as_of_without_writing(
    conn, monkeypatch, an_instrument_named_zxco, capsys
):
    """Task 3 review follow-up: nothing rejected a future-dated mark, and
    latest_marks treats the newest as_of as the current price -- a
    fat-fingered year or a bad backfill would otherwise silently become
    today's price with no signal at all. One year out is far past any
    plausible clock-skew tolerance, so this pins the refusing side
    unambiguously."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    as_of = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    rc = await cli.cmd_marks_set(_args(symbol="ZXCO", price="9", as_of=as_of))
    assert rc == 2
    err = capsys.readouterr().err
    assert "future" in err.lower()
    assert await latest_marks(conn, [an_instrument_named_zxco]) == {}
