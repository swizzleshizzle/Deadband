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
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.marks import latest_marks, set_mark
from db.snapshots import add_snapshot, latest_snapshot
from db.trades import list_trades, regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
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
    err = capsys.readouterr().err
    assert "natural-key" in err
    # Not just the CLI's own hint -- db.marks.resolve_instrument_by_symbol's
    # actual message (naming every candidate) must survive into stderr too.
    # A print that dropped `{exc}` and kept only the static hint would still
    # satisfy the "natural-key" check above while silently deleting the
    # candidate list a user actually needs to disambiguate.
    assert "equity:DUPE:USD" in err
    assert "equity:DUPE:EUR" in err
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


def test_marks_set_price_help_states_the_unit(monkeypatch, capsys):
    """Final-review finding (M1): `--price` was the only argument in this
    parser with no help= at all, and its unit is not guessable. For an option
    the correct input is the per-share premium (2.50), not the per-contract
    cost (250) -- entering the latter silently produces a 100x wrong
    unrealized P&L, which is Important 3's failure reached through user
    input rather than through a dropped multiplier."""
    monkeypatch.setattr("sys.argv", ["deadband", "marks", "set", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    help_text = capsys.readouterr().out
    assert "multiplier" in help_text
    assert "quote currency" in help_text


async def test_marks_set_defaults_as_of_to_now_when_omitted(
    conn, monkeypatch, an_instrument_named_zxco, capsys
):
    """The clock lives in the CLI, not in db/marks.py -- confirms cmd_marks_set
    itself supplies datetime.now(UTC) when --as-of is absent, rather than
    passing None through to set_mark (which would crash on
    naive.tzinfo is None).

    Must assert the STORED as_of, not just the price: a version of this test
    that checked only the recorded price passed even when the reviewer
    replaced the default with datetime(1970, 1, 1, tzinfo=UTC) -- it pinned
    nothing about "the clock lives in the CLI" at all. A generous one-minute
    window is plenty to distinguish "now" from any stale hardcoded stand-in
    without becoming flaky."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    before = datetime.now(UTC)
    rc = await cli.cmd_marks_set(_args(symbol="ZXCO", price="5"))
    assert rc == 0, capsys.readouterr().err
    price, stored_as_of = (await latest_marks(conn, [an_instrument_named_zxco]))[
        an_instrument_named_zxco
    ]
    assert price == Decimal("5")
    assert abs(stored_as_of - before) < timedelta(minutes=1)


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


async def test_marks_set_refuses_a_naive_as_of_cleanly_not_a_traceback(
    conn, monkeypatch, an_instrument_named_zxco, capsys
):
    """Review finding (Critical 1): a timezone-less --as-of used to reach the
    `as_of > now + tolerance` comparison first, raising an uncaught
    `TypeError: can't compare offset-naive and offset-aware datetimes` --
    never the clean ValueError path set_mark provides for exactly this case,
    since set_mark was never even reached. Typing a timestamp with no offset
    is an ordinary fat-finger; it must produce rc == 2 and a one-line stderr
    message, not a stack trace."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_marks_set(
        _args(symbol="ZXCO", price="1", as_of="2026-08-08T12:00:00")
    )
    assert rc == 2
    assert "offset" in capsys.readouterr().err.lower()
    assert await latest_marks(conn, [an_instrument_named_zxco]) == {}


async def test_marks_set_refuses_an_unparseable_price_cleanly_not_a_traceback(
    conn, monkeypatch, an_instrument_named_zxco, capsys
):
    """Review finding (Important 3): decimal.InvalidOperation does not
    descend from ValueError, so `Decimal("abc")` used to crash uncaught."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_marks_set(_args(symbol="ZXCO", price="abc"))
    assert rc == 2
    assert "not a valid number" in capsys.readouterr().err
    assert await latest_marks(conn, [an_instrument_named_zxco]) == {}


@pytest.mark.parametrize("bad_price", ["NaN", "Infinity", "-Infinity"])
async def test_marks_set_refuses_a_non_finite_price(
    conn, monkeypatch, an_instrument_named_zxco, capsys, bad_price
):
    """Review finding (Important 4): Decimal("NaN")/Decimal("Infinity")
    construct successfully and used to reach the database, where
    mark_price_chk refused them as an uncaught asyncpg.CheckViolationError.
    is_finite() is this codebase's own established guard for exactly this
    (see importers/fidelity.py, importers/coinbase_api.py)."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_marks_set(_args(symbol="ZXCO", price=bad_price))
    assert rc == 2
    assert "finite" in capsys.readouterr().err.lower()
    assert await latest_marks(conn, [an_instrument_named_zxco]) == {}


async def test_marks_set_accepts_an_as_of_just_inside_the_future_tolerance(
    conn, monkeypatch, an_instrument_named_zxco, capsys
):
    """Minor finding: pin the tolerance boundary itself, not just an
    extreme (365-day) future date far past any plausible window. 90 seconds
    is comfortably inside the 2-minute tolerance with margin against test
    execution time."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    as_of = (datetime.now(UTC) + timedelta(seconds=90)).isoformat()
    rc = await cli.cmd_marks_set(_args(symbol="ZXCO", price="11", as_of=as_of))
    assert rc == 0, capsys.readouterr().err
    assert (await latest_marks(conn, [an_instrument_named_zxco]))[an_instrument_named_zxco][
        0
    ] == Decimal("11")


async def test_marks_set_refuses_an_as_of_just_outside_the_future_tolerance(
    conn, monkeypatch, an_instrument_named_zxco, capsys
):
    """Negative twin of the test above: 150 seconds out is comfortably past
    the 2-minute tolerance, pinning the refusing side of the same boundary
    rather than relying solely on an extreme future date."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    as_of = (datetime.now(UTC) + timedelta(seconds=150)).isoformat()
    rc = await cli.cmd_marks_set(_args(symbol="ZXCO", price="13", as_of=as_of))
    assert rc == 2
    assert "future" in capsys.readouterr().err.lower()
    assert await latest_marks(conn, [an_instrument_named_zxco]) == {}


# --- cmd_positions -----------------------------------------------------------


def _position_fill(acc, inst, *, side, quantity, price, ref, estimated=False):
    """Same shape as tests/db/test_positions.py's own `_fill` helper --
    reused conceptually, not imported, since that module keeps it private
    and this file already owns its Fill-building conventions (see the marks
    fixtures above)."""
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=ref,
        is_estimated=estimated,
    )


async def _second_open_trade_on(conn, acc, inst, *, direction, quantity, basis, ref):
    """Add a SECOND open trade on an instrument that already has one.

    regroup_account never produces this shape -- it merges every open fill on
    one instrument into a single trade -- so the fill is inserted and the
    trade row written directly, the same way the `spread_account` fixture
    above reaches into trade state that regroup would never leave behind.

    It is nonetheless a real database state: an unregrouped import, a manual
    trade (`grouping_mode = 'manual'`), or a partially applied regroup all
    leave two open trades on one instrument. `aggregate_positions` groups by
    (account, instrument), not instrument alone, so these two land in one
    position because `acc` (passed in by every caller below) is the SAME
    account both times -- not because the instrument alone is enough. That is
    the only way to reach the compound arithmetic (a summed quantity, a
    weighted basis across both) the fabricated-figure and precision findings
    are about.

    The fill is real and anchors `opening_fill_id`, so the LEFT JOIN in
    db/positions.py resolves the instrument normally -- this is NOT the
    orphaned-trade path.
    """
    fill = _position_fill(
        acc, inst, side=Side.BUY, quantity=quantity, price=basis, ref=ref
    )
    await insert_fills(conn, [fill])
    await conn.execute(
        """
        INSERT INTO trade (account_id, direction, status, opening_fill_id,
                           opened_at, open_quantity, open_cost_basis,
                           grouping_mode)
        VALUES ($1, $2, 'open', $3, $4, $5, $6, 'manual')
        """,
        acc,
        direction,
        fill.id,
        fill.executed_at,
        Decimal(quantity),
        Decimal(basis),
    )


@pytest_asyncio.fixture
async def marked_position_account(conn):
    """One account, one open long of 10 ZXCO at an average cost of 18.20,
    marked fresh (now) -- (24.50 - 18.20) * 10 * 1 == 63.00, the exact figure
    test_positions_shows_unrealized_where_a_mark_exists pins."""
    acc = await create_account(conn, name="Marked", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [_position_fill(acc, inst, side=Side.BUY, quantity="10", price="18.20", ref="pos-mk1")],
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("24.50"), datetime.now(UTC))
    return acc


@pytest_asyncio.fixture
async def unmarked_account(conn):
    """One open position with no mark recorded at all -- latest_marks must
    report it absent, not zero, and cmd_positions must render a placeholder,
    never "0.00" (which mark_price_chk permits as a genuine price)."""
    acc = await create_account(conn, name="Unmarked", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="NOMK", quote_currency="USD"),
    )
    # price/quantity chosen so no rendered field's decimal point is preceded
    # by a '0' digit (e.g. "50.00...") -- that would make the "0.00" absence
    # assertion pass or fail by accident of formatting rather than substance.
    await insert_fills(
        conn,
        [_position_fill(acc, inst, side=Side.BUY, quantity="3", price="17", ref="pos-um1")],
    )
    await regroup_account(conn, acc)
    return acc


@pytest_asyncio.fixture
async def spread_account(conn):
    """One open position whose trade is a SPREAD -- unrealized_pnl() raises
    NotImplementedError for this direction, so it must be listed with its
    reason, never priced and never dropped.

    The auto-grouper never produces Direction.SPREAD (see
    tests/test_grouping_properties.py's own assertion of that), so there is
    no ordinary import path that creates one here; the trade's direction is
    flipped directly after an ordinary regroup, the same way
    tests/db/test_positions.py's orphaned-trade fixtures reach into trade
    state regroup_account itself would never leave a trade in."""
    acc = await create_account(conn, name="Spread", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SPRD", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [_position_fill(acc, inst, side=Side.BUY, quantity="5", price="10", ref="pos-sp1")],
    )
    await regroup_account(conn, acc)
    await conn.execute("UPDATE trade SET direction = 'spread' WHERE account_id = $1", acc)
    return acc


@pytest_asyncio.fixture
async def stale_mark_account(conn):
    """One open position marked well over a month before this suite's other
    fixtures' clock (2026-08-xx) -- old enough that rendering it identically
    to a fresh mark would be misleading."""
    acc = await create_account(conn, name="Stale", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="STLE", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [_position_fill(acc, inst, side=Side.BUY, quantity="4", price="12", ref="pos-st1")],
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("15"), datetime(2026, 7, 1, tzinfo=UTC))
    return acc


@pytest_asyncio.fixture
async def mixed_direction_account(conn):
    """One instrument holding an open long of 10 @ 20 and an open short of
    4 @ 50 -- the exact shape the final review captured rendering as
    `ZXCO 14 28.571428...`. 14 is the sum of magnitudes: not the net (6), not
    either leg, not gross exposure in any direction; 28.57 averages a long
    basis with a short one."""
    acc = await create_account(conn, name="Mixed", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="MIXD", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [_position_fill(acc, inst, side=Side.BUY, quantity="10", price="20", ref="pos-mx1")],
    )
    await regroup_account(conn, acc)
    await _second_open_trade_on(
        conn, acc, inst, direction="short", quantity="4", basis="50", ref="pos-mx2"
    )
    # Marked, deliberately: the row must refuse to price itself because of
    # the mixed direction, not merely because no mark happens to exist.
    await set_mark(conn, inst, Decimal("30"), datetime.now(UTC))
    return acc


@pytest_asyncio.fixture
async def repeating_basis_account(conn):
    """Two open longs, 1 @ 10 and 2 @ 20, marked at 25.

    The weighted average basis is 50/3, which does not terminate, so the
    ctx.prec = 50 division in aggregate_positions produced a 50-digit cost
    basis and a 28-digit unrealized straight out of `str()`."""
    acc = await create_account(conn, name="Repeating", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="XACC", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [_position_fill(acc, inst, side=Side.BUY, quantity="1", price="10", ref="pos-rp1")],
    )
    await regroup_account(conn, acc)
    await _second_open_trade_on(
        conn, acc, inst, direction="long", quantity="2", basis="20", ref="pos-rp2"
    )
    await set_mark(conn, inst, Decimal("25"), datetime.now(UTC))
    return acc


@pytest_asyncio.fixture
async def zero_marked_account(conn):
    """A position marked at a GENUINE zero. `mark_price_chk` permits price 0
    (a worthless expiring option, a delisted shell), so "no mark" and "marked
    at zero" are different facts that must not render alike.

    Same 3 @ 17 shape as `unmarked_account`, so the two tests differ in
    exactly one thing: whether a mark exists."""
    acc = await create_account(conn, name="ZeroMark", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZERO", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [_position_fill(acc, inst, side=Side.BUY, quantity="3", price="17", ref="pos-z1")],
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("0"), datetime.now(UTC))
    return acc


@pytest_asyncio.fixture
async def option_and_equity_account(conn):
    """One account holding a 21-character OCC option contract (2 @ 2.50,
    multiplier 100, marked 3.10) and an ordinary equity (5 @ 12, marked 18).

    Both halves are load-bearing. The option pins the contract multiplier
    end-to-end -- (3.10 - 2.50) * 2 * 100 == 120.00, versus 1.20 if the
    multiplier is dropped anywhere in the chain. The equity beside it pins
    the column layout: at the old `{:<10}` symbol width the 21-character
    contract pushed every later field on its own row out of alignment with
    its neighbour's."""
    acc = await create_account(conn, name="OptEq", venue="manual", account_type="cash")
    opt = await upsert_instrument(
        conn,
        Instrument(
            id=None,
            asset_class=AssetClass.OPTION,
            # 21 characters, the OCC maximum: 6-char padded root, 6-digit
            # expiry, right, 8-digit strike.
            symbol="ZXCO  261218C00050000",
            quote_currency="USD",
            underlying="ZXCO",
            strike=Decimal("50"),
            expiry=datetime(2026, 12, 18, tzinfo=UTC).date(),
            option_right="call",
            contract_multiplier=Decimal("100"),
        ),
    )
    eq = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="EQTY", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            _position_fill(acc, opt, side=Side.BUY, quantity="2", price="2.50", ref="pos-op1"),
            _position_fill(acc, eq, side=Side.BUY, quantity="5", price="12", ref="pos-eq1"),
        ],
    )
    await regroup_account(conn, acc)
    await set_mark(conn, opt, Decimal("3.10"), datetime.now(UTC))
    await set_mark(conn, eq, Decimal("18"), datetime.now(UTC))
    return acc


@pytest_asyncio.fixture
async def short_position_account(conn):
    """An open SHORT of 10 @ 20, marked at 15 -- a gain of +50 for a short,
    and a loss of -50 if the direction is dropped on the way to
    unrealized_pnl. Opened with a SELL and no prior long, which is how
    group_fills detects a short (tests/test_grouping.py)."""
    acc = await create_account(conn, name="Short", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SHRT", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [_position_fill(acc, inst, side=Side.SELL, quantity="10", price="20", ref="pos-sh1")],
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("15"), datetime.now(UTC))
    return acc


@pytest_asyncio.fixture
async def estimated_position_account(conn):
    """A marked position whose only fill is flagged estimated -- 4 @ 11,
    marked 14, so the P&L is a real 12.00 computed from a guessed price."""
    acc = await create_account(conn, name="Est", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ESTM", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            _position_fill(
                acc, inst, side=Side.BUY, quantity="4", price="11", ref="pos-es1",
                estimated=True,
            )
        ],
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("14"), datetime.now(UTC))
    return acc


def _positions_args(*, account=None):
    """Namespace helper for cmd_positions. Kept separate from this file's
    `_args` (marks' symbol/price/as_of shape) rather than overloading it --
    `_args` is a bare module-level function name resolved at call time, so a
    second definition later in this module would silently replace the first
    for every earlier test too."""
    return argparse.Namespace(account=account)


async def test_positions_shows_unrealized_where_a_mark_exists(
    conn, monkeypatch, marked_position_account, capsys
):
    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(marked_position_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "ZXCO" in out
    assert "63.00" in out  # (24.50 - 18.20) * 10 * 1
    # This fill is NOT estimated, so the "~" marker must be absent. Paired
    # with test_positions_an_estimated_position_is_flagged below: without a
    # negative case, printing the marker unconditionally would be green.
    assert "~" not in out


async def test_positions_an_unmarked_one_shows_a_placeholder_not_a_zero(
    conn, monkeypatch, unmarked_account, capsys
):
    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(unmarked_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    # The whole row, field by field: the position is real (a quantity and a
    # basis are shown) and only the two mark-dependent columns are blank.
    # Asserting the exact fields rather than `"--" in out` keeps this from
    # passing on a row that blanked everything, which is what the
    # unvaluable-position branch does and is a different outcome entirely.
    assert out.split() == ["NOMK", "Unmarked", "3.00", "17.00", "--", "--"]
    # Still the real point of the test, and still not vacuous: a placeholder
    # must be visibly different from a genuine zero price, which
    # mark_price_chk permits and which renders "0.00" (see
    # test_positions_a_genuine_zero_mark_is_not_a_placeholder). The fixture's
    # 3 and 17 are chosen so no other field can contain the substring --
    # a quantity of 10 would render "10.00" and satisfy this by accident.
    assert "0.00" not in out


async def test_positions_a_genuine_zero_mark_is_not_a_placeholder(
    conn, monkeypatch, zero_marked_account, capsys
):
    """Positive twin of the test above. `mark_price_chk` permits price 0, so
    "unmarked" and "marked at zero" are different facts about a position and
    a reader must be able to tell them apart. Rendering both as "--" would
    hide a total loss; rendering both as "0.00" would invent one."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(zero_marked_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "--" not in out
    fields = out.split()
    assert fields[0] == "ZERO"
    assert fields[1] == "ZeroMark"
    assert fields[4] == "0.00"  # the mark itself, at a real zero
    assert fields[5].startswith("@")  # ...still carrying its as_of date
    assert fields[-1] == "-51.00"  # (0 - 17) * 3 * 1


async def test_positions_an_unvaluable_one_shows_no_quantity_or_basis(
    conn, monkeypatch, mixed_direction_account, capsys
):
    """Final-review finding (Important 1). A long 10 @ 20 and a short 4 @ 50
    on one instrument used to render `MIXD 14 28.571428...`: 14 is the sum of
    magnitudes -- not the net 6, not either leg, not gross exposure in any
    direction -- and 28.57 averages a long basis with a short one. The
    `n/a (mixed direction)` disclaimer sits four fields to the right, where it
    reads as "we can't price this", not "the 14 is meaningless too".

    The row must still be LISTED, with its reason. Blanking the two fabricated
    columns is the opposite of hiding it."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(mixed_direction_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out

    assert out.split()[:5] == ["MIXD", "Mixed", "--", "--", "--"]
    assert "mixed direction" in out
    assert "14" not in out  # the summed magnitudes
    assert "28.57" not in out  # the long/short blended basis
    assert "30" not in out  # nor the mark, which is real but unusable here


async def test_positions_bounds_a_repeating_cost_basis_for_display(
    conn, monkeypatch, repeating_basis_account, capsys
):
    """Final-review finding (Important 2). 1 @ 10 plus 2 @ 20 weights to 50/3,
    which does not terminate, so the ctx.prec = 50 division reached `str()` as
    a 50-digit cost basis and a 28-digit unrealized -- correct numbers, but
    asserting a precision the inputs never had and wrapping the branch's
    headline row off a standard terminal.

    Quantized at the render site only: the stored `open_cost_basis` and what
    `aggregate_positions` computes are untouched."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(repeating_basis_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    (line,) = out.splitlines()

    fields = line.split()
    assert fields[0] == "XACC"
    assert fields[1] == "Repeating"
    assert fields[2] == "3.00"
    assert fields[3] == "16.66666667"  # 50/3, bounded -- not 50 digits of it
    assert fields[-1] == "25.00"  # (25 - 50/3) * 3, not 28 digits of it
    assert "16.666666666" not in line
    # The whole point of the bound: the row fits a standard terminal.
    assert len(line) <= 100, line


async def test_positions_applies_the_contract_multiplier_end_to_end(
    conn, monkeypatch, option_and_equity_account, capsys
):
    """Final-review finding (Important 3): every fixture on this branch was an
    equity, so `multiplier=Decimal(1)` in either db/positions.py or
    ledger/positions.py survived all 464 tests. On this option contract that
    mutation turns 120.00 into 1.20 -- the 100x error that matters most.

    Also finding M3: the 21-character OCC symbol must not shift its own row's
    columns out of line with the equity row beside it."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(option_and_equity_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 2
    eq_line = next(line for line in lines if line.startswith("EQTY"))
    opt_line = next(line for line in lines if line.startswith("ZXCO  261218C00050000"))

    assert opt_line.split()[-1] == "120.00"  # (3.10 - 2.50) * 2 * 100
    assert eq_line.split()[-1] == "30.00"  # (18 - 12) * 5 * 1
    # M3: same column, both rows. At the old 10-wide symbol field the
    # 21-character contract pushed its own row 11 characters right.
    assert opt_line.index("120.00") == eq_line.index("30.00")


async def test_positions_values_a_short_in_the_right_direction(
    conn, monkeypatch, short_position_account, capsys
):
    """Final-review finding (Important 4): SHORT never reached unrealized_pnl
    through this branch, so hardcoding Direction.LONG at the call site was
    green. ledger/pnl.py gates the formula; nothing gated the wiring.

    Short 10 @ 20 marked at 15 is a gain of +50, and exactly -50 if the
    direction is dropped -- so the assertion is on the whole field, not a
    substring ("50.00" is inside "-50.00")."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(short_position_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert out.split()[0] == "SHRT"
    assert out.split()[-1] == "50.00"


async def test_positions_an_estimated_position_is_flagged(
    conn, monkeypatch, estimated_position_account, capsys
):
    """Final-review finding (Important 4): `is_estimated=True` appeared
    nowhere below the pure layer, so both db/positions.py's read and the "~"
    marker were ungated -- deleting the marker outright was green. It is the
    only signal separating a P&L computed from a real fill from one computed
    against a guessed price."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(estimated_position_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "~" in out
    # Attached to the symbol, not floating loose somewhere in the row.
    assert out.split()[:2] == ["ESTM", "~"]
    assert out.split()[-1] == "12.00"  # (14 - 11) * 4 * 1


async def test_positions_an_unvaluable_one_is_listed_with_its_reason(
    conn, monkeypatch, spread_account, capsys
):
    """A position omitted from a position listing is the silent-loss shape
    this codebase keeps rediscovering."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(spread_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "spread" in out


async def test_positions_the_marks_age_is_shown(conn, monkeypatch, stale_mark_account, capsys):
    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(stale_mark_account)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "2026-07-01" in out or "d ago" in out


@pytest_asyncio.fixture
async def two_accounts_one_instrument(conn):
    """A taxable and a retirement account, each holding the SAME instrument
    -- one long, one short -- so the end-to-end path (not just the pure
    layer or the SQL) is proven to emit two priceable rows rather than one
    blended, unvaluable one."""
    acc_tax = await create_account(conn, name="Taxable", venue="manual", account_type="cash")
    acc_ret = await create_account(conn, name="Retirement", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SHRD", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [_position_fill(acc_tax, inst, side=Side.BUY, quantity="10", price="20", ref="sh-tax")],
    )
    await regroup_account(conn, acc_tax)
    await insert_fills(
        conn,
        [_position_fill(acc_ret, inst, side=Side.SELL, quantity="4", price="50", ref="sh-ret")],
    )
    await regroup_account(conn, acc_ret)
    return acc_tax, acc_ret


async def test_positions_unscoped_shows_one_row_per_account_not_a_blend(
    conn, monkeypatch, two_accounts_one_instrument, capsys
):
    """The behaviour this task exists for, exercised through the actual CLI
    command with no `--account` filter -- the exact case the owner flagged:
    without it, one instrument's holdings across every account used to merge
    into a single row, manufacturing a 'mixed direction' position (long here,
    short there) that exists nowhere in reality. Now each account's holding
    is its own row, and both price normally."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=None))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    # The test database is shared and persistent, so an unscoped call can see
    # other committed data -- filtered to this fixture's own symbol, the same
    # pattern as tests/db/test_positions.py's
    # test_two_orphaned_trades_in_different_accounts_do_not_merge_when_unscoped.
    # A bare `len(lines) == 2` would only pass by accident of the database
    # happening to hold nothing else right now.
    shrd_lines = [line for line in out.splitlines() if line.startswith("SHRD")]

    assert len(shrd_lines) == 2
    assert not any("mixed direction" in line for line in shrd_lines)
    tax_line = next(line for line in shrd_lines if "Taxable" in line)
    ret_line = next(line for line in shrd_lines if "Retirement" in line)
    assert tax_line.split()[2] == "10.00"  # quantity, not blended with the other leg
    assert ret_line.split()[2] == "4.00"


# --- cmd_snapshot_add ---------------------------------------------------------


@pytest_asyncio.fixture
async def an_account(conn):
    """A single account this test file owns, isolated by the rolled-back
    `conn` transaction from tests/conftest.py -- it never persists."""
    return await create_account(conn, name="Snap", venue="manual", account_type="cash")


def _snapshot_args(*, account, as_of, equity, cash, note=None):
    """Same pattern as `_args` above (marks-specific): a real
    argparse.Namespace built by hand, not through parser.parse_args(). Named
    distinctly from `_args` so it cannot collide with -- or silently widen --
    the marks-only helper defined above; redefining that one would silently
    break every marks test below it."""
    return argparse.Namespace(account=account, as_of=as_of, equity=equity, cash=cash, note=note)


async def test_snapshot_add_stores_the_figures(conn, an_account, monkeypatch):
    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_snapshot_add(
        _snapshot_args(
            account=str(an_account), as_of="2026-07-31",
            equity="41203.18", cash="2110.00", note=None,
        )
    )
    assert rc == 0
    row = await latest_snapshot(conn, an_account)
    assert row["total_equity"] == Decimal("41203.18")
    # "figures" is plural -- cash must be pinned too, not just equity.
    assert row["cash_balance"] == Decimal("2110.00")
    # A bare date becomes midnight UTC: pins the "clock lives in the CLI"
    # rule the same way test_marks_set_defaults_as_of_to_now_when_omitted
    # pins its own clock rule -- a version that stored the date at local
    # midnight, or dropped the offset, would fail this exact comparison.
    assert row["as_of"] == datetime(2026, 7, 31, tzinfo=UTC)


async def test_snapshot_add_refuses_a_future_as_of_without_writing(
    conn, an_account, monkeypatch, capsys
):
    """Same reasoning as marks_set's identical guard: latest_snapshot treats
    the newest as_of as current, so a fat-fingered year must be refused
    before anything is written, not merely mis-recorded."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_snapshot_add(
        _snapshot_args(
            account=str(an_account), as_of="2099-01-01",
            equity="1", cash="1", note=None,
        )
    )
    assert rc == 2
    assert "future" in capsys.readouterr().err.lower()
    assert await latest_snapshot(conn, an_account) is None


async def test_snapshot_add_refuses_a_non_finite_figure_without_writing(
    conn, an_account, monkeypatch
):
    """Decimal("NaN") constructs successfully and would otherwise reach the
    database as a broker figure -- is_finite() is this codebase's own
    established guard against that (see cmd_marks_set's identical check)."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_snapshot_add(
        _snapshot_args(
            account=str(an_account), as_of="2026-07-31",
            equity="NaN", cash="1", note=None,
        )
    )
    assert rc == 2
    assert await latest_snapshot(conn, an_account) is None


# --- cmd_reconcile -------------------------------------------------------------
#
# The plan's own self-review flags this as the task most likely to run long on
# fixtures, so these four reuse the account-state recipe already established
# above and in tests/db/test_positions.py (create_account, upsert_instrument,
# insert_fills, regroup_account, add_snapshot) rather than inventing a fifth
# harness. There is no tests/db/conftest.py, so nothing here can be imported
# from test_positions.py -- `_add_orphaned_position` below replicates that
# file's `_make_orphaned_trade` recipe rather than importing it.

_RECONCILE_AS_OF = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


async def _add_orphaned_position(conn, acc, *, symbol, ref):
    """Same recipe as tests/db/test_positions.py's `_make_orphaned_trade`:
    open one ordinary trade on a fresh instrument, then protect it (notes set
    so regroup preserves it, its opening fill deleted, regrouped again). The
    composite FK nulls opening_fill_id and the protection UPDATE nulls
    open_quantity/open_cost_basis, while status stays 'open' -- an
    unreachable-instrument position that still holds exposure.

    Load-bearing for the direction-vs-unvaluable_reason mutation gate: the
    resulting position keeps its ORIGINAL single direction (a plain BUY, so
    Direction.SPREAD never enters the mix and the group never disagrees on
    direction) while unvaluable_reason gets set from the null quantity --
    exactly the "direction is set AND still unvaluable" case
    ledger/positions.py's OpenPosition docstring warns about."""
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency="USD"),
    )
    fill = _position_fill(acc, inst, side=Side.BUY, quantity="5", price="10", ref=ref)
    await insert_fills(conn, [fill])
    await regroup_account(conn, acc)
    trade = next(t for t in await list_trades(conn, acc) if t["opening_fill_id"] == fill.id)
    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", trade["id"])
    await conn.execute("DELETE FROM fill WHERE id = $1", fill.id)
    await regroup_account(conn, acc)


async def _deposit(conn, acc, amount, *, currency="USD"):
    """Cash in, via cash_movement directly (no importer in play here) -- the
    same table db/cash.py's account_cash reads, inserted straight rather than
    through the importer path since these fixtures never go near a CSV.
    `currency` defaults to USD and is only ever overridden by the
    mixed-currency fixture below -- every other caller stays single-currency."""
    await conn.execute(
        """
        INSERT INTO cash_movement (account_id, occurred_at, kind, amount, currency)
        VALUES ($1, $2, 'deposit', $3, $4)
        """,
        acc, _RECONCILE_AS_OF, Decimal(amount), currency,
    )


@pytest_asyncio.fixture
async def reconcilable_account(conn):
    """One priced position, one deposit, and a snapshot set to match the
    computed totals exactly.

    Arithmetic: deposit 10000; buy 10 RCON @ 50 (fee 0) spends 500, so
    computed cash = 10000 - 500 = 9500. Marked at 60, market value =
    10 * 60 * 1 = 600, so computed equity = 9500 + 600 = 10100. The snapshot
    below is set to exactly (9500, 10100) -- reconcile's own arithmetic has
    to reproduce those two figures for the OK verdict to hold, not just agree
    with itself."""
    acc = await create_account(conn, name="Reconcilable", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="RCON", quote_currency="USD"),
    )
    await _deposit(conn, acc, "10000")
    await insert_fills(
        conn, [_position_fill(acc, inst, side=Side.BUY, quantity="10", price="50", ref="rc1")]
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("60"), _RECONCILE_AS_OF)
    await add_snapshot(
        conn, acc, _RECONCILE_AS_OF, cash_balance=Decimal("9500"), total_equity=Decimal("10100")
    )
    return acc


@pytest_asyncio.fixture
async def unreliable_but_agreeing_account(conn):
    """One priced position plus one orphaned (unvaluable) position, with the
    snapshot set to match what reconcile computes from the priced position
    and cash ALONE -- the orphaned position's quantity/cost_basis are forced
    to None by the protection path (see _add_orphaned_position), so it
    contributes nothing to either side of the arithmetic and the numbers
    agree even though the account is not fully priceable.

    Arithmetic: deposit 1000; buy 5 URLA @ 100 (fee 0) spends 500, so
    computed cash = 1000 - 500 = 500. Marked at 100, market value =
    5 * 100 * 1 = 500, so computed equity = 500 + 500 = 1000. Snapshot set to
    exactly (500, 1000)."""
    acc = await create_account(
        conn, name="UnreliableAgree", venue="manual", account_type="cash"
    )
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="URLA", quote_currency="USD"),
    )
    await _deposit(conn, acc, "1000")
    await insert_fills(
        conn, [_position_fill(acc, inst, side=Side.BUY, quantity="5", price="100", ref="ura1")]
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("100"), _RECONCILE_AS_OF)
    await _add_orphaned_position(conn, acc, symbol="URLB", ref="urb1")
    await add_snapshot(
        conn, acc, _RECONCILE_AS_OF, cash_balance=Decimal("500"), total_equity=Decimal("1000")
    )
    return acc


@pytest_asyncio.fixture
async def unreliable_account(conn):
    """One priced position plus one orphaned (unvaluable) position, with the
    snapshot's reported equity set FAR from what reconcile computes -- as if
    the broker statement includes real value for the position the ledger
    cannot price, which the computed side necessarily excludes.

    Arithmetic: deposit 1000; buy 2 URDA @ 100 (fee 0) spends 200, so
    computed cash = 1000 - 200 = 800 (cash agrees with the snapshot below).
    Marked at 100, market value = 2 * 100 * 1 = 200, so computed equity =
    800 + 200 = 1000. The snapshot's reported equity is set to 6000 instead
    of 1000 -- a -5000 equity_difference that is the EXPECTED shape of an
    excluded position, not a defect, which is exactly what the rendered
    output has to say."""
    acc = await create_account(
        conn, name="UnreliableDrift", venue="manual", account_type="cash"
    )
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="URDA", quote_currency="USD"),
    )
    await _deposit(conn, acc, "1000")
    await insert_fills(
        conn, [_position_fill(acc, inst, side=Side.BUY, quantity="2", price="100", ref="urd1")]
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("100"), _RECONCILE_AS_OF)
    await _add_orphaned_position(conn, acc, symbol="URDB", ref="urdb1")
    await add_snapshot(
        conn, acc, _RECONCILE_AS_OF, cash_balance=Decimal("800"), total_equity=Decimal("6000")
    )
    return acc


@pytest_asyncio.fixture
async def mixed_currency_account(conn):
    """Two cash movements on the same account in different currencies (USD
    and EUR) -- same shape as tests/db/test_cash.py's own
    `mixed_currency_account`, replicated here rather than imported (no
    tests/db/conftest.py to share it through, and this file already owns its
    own cash_movement-inserting `_deposit`), plus a snapshot: without one,
    cmd_reconcile's own step 2 refusal (no snapshot) would fire first and
    account_cash -- and the MixedCurrencyError it raises -- would never be
    reached at all."""
    acc = await create_account(conn, name="MixedCurrency", venue="manual", account_type="cash")
    await _deposit(conn, acc, "100", currency="USD")
    await _deposit(conn, acc, "50", currency="EUR")
    await add_snapshot(
        conn, acc, _RECONCILE_AS_OF, cash_balance=Decimal("0"), total_equity=Decimal("0")
    )
    return acc


def _reconcile_args(*, account, as_of, tolerance):
    """Same pattern as `_args`/`_snapshot_args` above: a real
    argparse.Namespace built by hand, not through parser.parse_args(). Named
    distinctly from both so it cannot collide with -- or silently widen --
    either of those two helpers."""
    return argparse.Namespace(account=account, as_of=as_of, tolerance=tolerance)


async def test_reconcile_agrees_and_exits_zero(conn, reconcilable_account, monkeypatch, capsys):
    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(reconcilable_account), as_of=None, tolerance=None)
    )
    assert rc == 0
    assert "ok" in capsys.readouterr().out.lower()


async def test_reconcile_refuses_an_account_with_no_snapshot(
    conn, an_account, monkeypatch, capsys
):
    """Reporting zero drift against nothing is the silent-success shape."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(an_account), as_of=None, tolerance=None)
    )
    assert rc == 2
    assert "snapshot" in capsys.readouterr().err.lower()


async def test_an_unvaluable_position_never_exits_zero_even_when_numbers_agree(
    conn, unreliable_but_agreeing_account, monkeypatch, capsys
):
    """The whole point of the verdict. A caller reading is_within_tolerance
    alone would print a clean pass here."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(_reconcile_args(
        account=str(unreliable_but_agreeing_account), as_of=None, tolerance=None))
    out = capsys.readouterr().out
    assert rc == 1
    assert "unreliable" in out.lower()


async def test_an_unreliable_run_explains_why_its_drift_looks_large(
    conn, unreliable_account, monkeypatch, capsys
):
    """computed_equity excludes the unvalued position, so the drift reads as a
    big negative number that is expected. Saying so is the difference between a
    useful report and a phantom hunt."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    await cli.cmd_reconcile(
        _reconcile_args(account=str(unreliable_account), as_of=None, tolerance=None)
    )
    out = capsys.readouterr().out.lower()
    assert "excluded" in out or "not included" in out


async def test_reconcile_refuses_an_unknown_account(conn, monkeypatch, capsys):
    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    bogus = uuid4()
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(bogus), as_of=None, tolerance=None)
    )
    assert rc == 2
    # Names the actual problem account, not just a bare "error: refused" --
    # the same standard test_regroup_unknown_account_prints_a_clean_error_
    # not_a_traceback holds cmd_regroup to for the identical situation.
    assert str(bogus) in capsys.readouterr().err


# --- Fix round 1: statement clock vs. ledger clock ---------------------------


async def test_reconcile_labels_the_statement_and_ledger_clocks_separately(
    conn, reconcilable_account, monkeypatch, capsys
):
    """account_cash, open_positions and latest_marks all read CURRENT ledger
    state -- open_positions and latest_marks don't even take an `as_of`
    parameter -- while the report's other clock is the STATEMENT's date
    (snapshot.as_of). A single "as of <statement date>" header above numbers
    that are actually current would misrepresent any ordinary trading since
    the statement as drift "as of" a date before any of it happened: the same
    phantom-hunt shape the brief requires for the unvaluable-exclusion case.
    `reconcilable_account` pins its snapshot/fill/mark clock to a fixed past
    instant (_RECONCILE_AS_OF); `ledger as of` is real wall-clock time at
    test run, which is a different instant -- so the two labelled lines must
    both appear, and must not carry the same timestamp."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(reconcilable_account), as_of=None, tolerance=None)
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    out = captured.out

    assert "statement as of 2026-08-01t09:00:00+00:00" in out.lower()
    assert "ledger as of" in out.lower()

    statement_line = next(line for line in out.splitlines() if "statement as of" in line.lower())
    ledger_line = next(line for line in out.splitlines() if "ledger as of" in line.lower())
    statement_ts = statement_line.split("as of", 1)[1].strip()
    ledger_ts = ledger_line.split("as of", 1)[1].strip()
    assert statement_ts != ledger_ts


# --- Fix round 1: a negative --tolerance ---------------------------------------


async def test_reconcile_refuses_a_negative_tolerance_without_opening_a_connection(
    conn, monkeypatch, capsys
):
    """is_finite() rejects NaN/Infinity but not a negative number. A negative
    tolerance makes `abs(difference) <= tolerance` unsatisfiable, so EVERY
    account -- even a perfectly reconciled one -- would report DRIFT: a
    confidently wrong verdict from a silently accepted bad input. Refused
    before the pool is ever opened, same as every other argument guard --
    fake_create_pool raises if cmd_reconcile ever reaches it, so this also
    proves the check runs early rather than merely returning 2 eventually."""

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(uuid4()), as_of=None, tolerance="-1")
    )
    assert rc == 2
    assert "tolerance" in capsys.readouterr().err.lower()


# --- Fix round 1: MixedCurrencyError was implemented but unpinned ------------


async def test_reconcile_refuses_a_mixed_currency_account(
    conn, mixed_currency_account, monkeypatch, capsys
):
    """v1 does not model FX -- db/cash.py's account_cash already refuses a
    mixed-currency account by raising MixedCurrencyError; cmd_reconcile must
    catch it and print a clean, currency-naming error instead of letting it
    propagate as an uncaught traceback."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(mixed_currency_account), as_of=None, tolerance=None)
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "USD" in err and "EUR" in err
