"""CLI tests that need a real database — specifically to exercise cli.cmd_import
itself, not a hand-rolled re-implementation of its transaction pattern."""

from __future__ import annotations

import argparse
import pathlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio

import cli
from db.accounts import create_account
from db.corporate import add_action, list_actions
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.marks import latest_marks, set_mark
from db.positions import open_positions
from db.snapshots import add_snapshot, latest_snapshot
from db.trades import list_trades, regroup_account
from ledger.types import AssetClass, Direction, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db
from tests.db.conftest import _split, _symbol_change

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
    schema = await conn.fetchval("SELECT current_schema()")
    await conn.execute(f'DROP SCHEMA "{schema}" CASCADE; CREATE SCHEMA "{schema}";')

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
    """Drops and recreates the conn's own schema (since issue #15's fix, the
    per-session namespace, not public) on `conn` (rolled back by
    conftest's `conn` fixture at teardown, same as every other test here) to
    put it in the "never migrated" state a brand-new Postgres would be in,
    then runs the real cmd_migrate against it. Fails if cmd_migrate still says
    "already up to date": that is what it said before this fix, on a database
    that had zero tables a moment earlier."""
    schema = await conn.fetchval("SELECT current_schema()")
    await conn.execute(f'DROP SCHEMA "{schema}" CASCADE; CREATE SCHEMA "{schema}";')

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


# --- Amendment clusters (importer-blocking-verbs, Task 4). A Fidelity
# --- amendment is three rows -- the original, a CANCELLED TRADE reversing
# --- it, and a CORRECTED CONFIRM re-booking it -- whose net truth is ONE
# --- trade. The importer used to emit the original AND the correction as two
# --- separate buys; only the sibling cancel row (unmapped, money-carrying,
# --- so blocking) kept that phantom contract out of the ledger, which is a
# --- guard that vanishes the moment anyone teaches the classifier the
# --- CANCEL verb. This is the end-to-end proof that the netting reaches the
# --- committed rows, not just the parsed batch. ---------------------------


async def test_import_nets_an_amendment_cluster_and_reports_it(conn, monkeypatch, capsys):
    """Two assertions, and both matter.

    The netting must be SAID: rows that disappear silently are
    indistinguishable from rows the importer lost, and this one removes two
    money-carrying rows from a file the user handed over.

    And the ledger must end FLAT. The fixture's cluster is a buy that was
    cancelled and re-booked, plus the real sell that closed it -- one buy and
    one sell, so the contract is gone. Un-netted it is two buys and one sell,
    which leaves a phantom open long contract behind: asserting the position
    (not merely that the commit succeeded) is what distinguishes those.
    """
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    fixture = pathlib.Path(__file__).parents[1] / "fixtures" / "fidelity" / "amendment_cluster.csv"
    args = argparse.Namespace(
        venue="fidelity",
        file=str(fixture),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    # ONE readouterr(): it drains the capture, so reading it again for the
    # assertions below would find it empty.
    captured = capsys.readouterr()
    assert rc == 0, captured.out + captured.err
    # On STDERR specifically, not on "".join(captured). Joining the two
    # channels asserts the note exists SOMEWHERE, which is not the claim --
    # every batch warning goes to stderr (_preview_or_commit, cli.py) so that a
    # user piping stdout to a file still sees it, and a change that moved
    # this one note to stdout would silently pass a joined assertion while
    # breaking exactly that.
    assert "netted an amendment cluster" in captured.err

    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 2
    assert await open_positions(conn, acc) == (), (
        "one buy and one sell close the contract -- an un-netted import "
        "leaves a second, phantom buy open"
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
    assert "row(s) below block the commit" in err
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


async def test_snapshot_add_stores_the_figures(conn, an_account, monkeypatch, capsys):
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
    # Spec §7: "snapshot add writes one row and prints what it stored." A
    # silent success is what makes the overwrite path (gap #21 -- re-adding
    # the same as_of replaces a stored broker figure, with no history kept)
    # invisible, and it is the only chance the typist gets to notice a
    # fat-fingered figure before `reconcile` reports it as drift days later.
    # Both figures and the resolved as_of, so a version echoing only one of
    # them -- or echoing the argument instead of what was stored -- fails.
    out = capsys.readouterr().out
    assert "41203.18" in out
    assert "2110.00" in out
    assert "2026-07-31" in out
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


async def test_snapshot_add_refuses_a_non_finite_figure_without_opening_a_connection(
    conn, an_account, monkeypatch
):
    """Decimal("NaN") constructs successfully and would otherwise reach the
    database as a broker figure -- is_finite() is this codebase's own
    established guard against that (see cmd_marks_set's identical check).

    fake_create_pool RAISES rather than returning a stand-in pool, the same
    idiom as test_reconcile_refuses_a_negative_tolerance_without_opening_a_
    connection below: whether a figure is finite depends only on the
    arguments, never on the database, so the refusal must happen before a
    connection is opened. Merely returning 2 eventually would satisfy the old
    version of this test even if the check had drifted below `create_pool()`.
    The stored-row assertion stays as well -- never opening a connection
    implies never writing, but the two failures read differently."""

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_snapshot_add(
        _snapshot_args(
            account=str(an_account), as_of="2026-07-31",
            equity="NaN", cash="1", note=None,
        )
    )
    assert rc == 2
    assert await latest_snapshot(conn, an_account) is None


async def test_snapshot_add_refuses_an_unknown_account(conn, monkeypatch, capsys):
    """A well-formed but nonexistent account id used to reach
    `account_snapshot.account_id`'s foreign key and escape as a raw
    asyncpg.ForeignKeyViolationError traceback -- main() catches only OSError
    -- which is worse than either sibling command's behaviour for the same
    situation (docs/known-gaps.md gap #26). Refused cleanly with exit 2 now,
    the same get_account-then-check-None shape cmd_reconcile uses, and naming
    the offending id the way test_reconcile_refuses_an_unknown_account and
    test_regroup_unknown_account_prints_a_clean_error_not_a_traceback both
    require."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    bogus = uuid4()

    rc = await cli.cmd_snapshot_add(
        _snapshot_args(
            account=str(bogus), as_of="2026-07-31",
            equity="1", cash="1", note=None,
        )
    )
    assert rc == 2
    assert str(bogus) in capsys.readouterr().err
    assert await latest_snapshot(conn, bogus) is None


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

    # The fixture proves itself, the same way short_reconcilable_account below
    # does. Everything the UNRELIABLE tests assert rests on this position being
    # unvaluable, and that rests on two things nothing else here pins: that
    # regroup_account PRESERVES a trade whose `notes` are set (rather than
    # reaping it with the rest), and that the composite FK nulls
    # `opening_fill_id` when the opening fill is deleted, which is what makes
    # db/positions.py force quantity/cost_basis to None.
    #
    # If either changes, this call quietly yields a VALUABLE position: the
    # verdict becomes OK or DRIFT instead of UNRELIABLE and the tests built on
    # it fail with a bare verdict mismatch that names nothing -- or, worse,
    # unreliable_but_agreeing_account's numbers still add up and it passes for
    # the wrong reason. The protected trade keeps its own id through the
    # protection UPDATE, and db/positions.py substitutes that id for the
    # unreachable instrument's, so it is the exact key to find the row by.
    (orphaned,) = [
        p for p in await open_positions(conn, acc) if p.instrument_id == trade["id"]
    ]
    assert orphaned.unvaluable_reason is not None, (
        f"orphaned position for {symbol!r} came back VALUABLE "
        f"(direction={orphaned.direction!r}, quantity={orphaned.quantity!r}) -- "
        "the UNRELIABLE tests built on this fixture would prove nothing, since "
        "the position would be valued normally and the verdict would never be "
        "UNRELIABLE for the reason they claim"
    )


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


# --- Final review: a short position is a liability, not an asset --------------


@pytest_asyncio.fixture
async def short_reconcilable_account(conn):
    """A genuine SHORT position, opened with a SELL, priced against a mark.

    Named distinctly from `short_position_account` above (the cmd_positions
    fixture) and using its own symbol: a second fixture reusing that name
    would REDEFINE it at module scope and silently retarget every existing
    test that requests it -- which is exactly what happened on the first
    attempt here, reddening test_positions_values_a_short_in_the_right_
    direction with this fixture's numbers.

    Arithmetic: deposit 10000, then short 10 SHRC @ 50 -- the sale CREDITS
    500 (net_cash is already direction-aware), so computed cash = 10500.
    Marked at 60, the position is a liability worth -600, so computed equity =
    10500 - 600 = 9900. The snapshot is set to exactly (10500, 9900).

    Valued unsigned -- the bug this fixture exists to catch -- equity comes out
    10500 + 600 = 11100: wrong by 1200, twice the market value, and reported
    as DRIFT with the cash line agreeing to the cent, which reads as a pure
    equity discrepancy and sends the reader hunting a phantom.

    The fixture proves itself below rather than assuming: if this position
    landed in the UNVALUABLE bucket instead (`unvaluable_reason` set), it would
    never be turned into a `Position` at all, the verdict would be UNRELIABLE
    for an unrelated reason, and the test would prove nothing about signing.
    """
    acc = await create_account(conn, name="ShortSeller", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SHRC", quote_currency="USD"),
    )
    await _deposit(conn, acc, "10000")
    await insert_fills(
        conn, [_position_fill(acc, inst, side=Side.SELL, quantity="10", price="50", ref="shrc1")]
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("60"), _RECONCILE_AS_OF)
    await add_snapshot(
        conn, acc, _RECONCILE_AS_OF, cash_balance=Decimal("10500"), total_equity=Decimal("9900")
    )

    (position,) = await open_positions(conn, acc)
    assert position.direction is Direction.SHORT, (
        f"fixture is not short: direction={position.direction!r}"
    )
    assert position.unvaluable_reason is None, (
        "fixture landed in the unvaluable bucket "
        f"({position.unvaluable_reason!r}) -- it would never be valued at all, "
        "so a test built on it proves nothing about the direction sign"
    )
    # An unsigned MAGNITUDE, exactly as ledger/pnl.py:105 leaves it -- this is
    # why `direction` has to travel alongside it.
    assert position.quantity == Decimal("10")
    return acc


async def test_reconcile_values_a_short_position_as_a_liability(
    conn, short_reconcilable_account, monkeypatch, capsys
):
    """End to end, through the real command: a short must SUBTRACT its market
    value from equity. The sibling `positions` command has always passed
    `direction` through to unrealized_pnl; `reconcile`'s adapter dropped it,
    so every short was valued as though it were owned."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(short_reconcilable_account), as_of=None, tolerance=None)
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    out = captured.out
    assert "verdict: ok" in out.lower()

    # Parsed off the rendered line rather than string-matched, so the failure
    # message names the wrong number instead of just "not found".
    def _computed(label):
        line = next(ln for ln in out.splitlines() if ln.strip().startswith(label))
        return Decimal(line.split("computed", 1)[1].split()[0])

    # Cash agrees to the cent whether or not the sign is applied -- which is
    # exactly what made the unsigned bug read as a pure equity problem.
    assert _computed("cash:") == Decimal("10500")
    assert _computed("equity:") == Decimal("9900")  # 11100 if valued unsigned


# --- Final review: a bare-date --as-of, as README.md documents it -------------


async def test_reconcile_accepts_a_bare_date_as_of(
    conn, reconcilable_account, monkeypatch, capsys
):
    """README.md's own worked example passes a bare date to `snapshot add` on
    one line and to `reconcile` on the next. `cmd_snapshot_add` accepted it
    (date.fromisoformat -> midnight UTC) and `cmd_reconcile` did not:
    datetime.fromisoformat parsed "2026-08-02" to a NAIVE midnight, the tz
    guard fired, and the documented invocation exited 2. The two siblings now
    read the same string the same way."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    # A day AFTER the fixture's snapshot instant (2026-08-01T09:00Z): a bare
    # date becomes midnight, and midnight on the 1st precedes the snapshot,
    # which would refuse for the unrelated "no snapshot on or before" reason
    # and prove nothing about parsing.
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(reconcilable_account), as_of="2026-08-02", tolerance=None)
    )
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert "utc offset" not in captured.err.lower()
    # It really resolved to the stored statement, not to "now".
    assert "statement as of 2026-08-01t09:00:00+00:00" in captured.out.lower()


# --- Final review: the DRIFT verdict itself ----------------------------------


@pytest_asyncio.fixture
async def drifting_account(conn):
    """Everything valued, nothing unvaluable, and the numbers disagree.

    Arithmetic: deposit 1000; buy 5 DRFT @ 100 (fee 0) spends 500, so computed
    cash = 500. Marked at 100, market value = 5 * 100 * 1 = 500, so computed
    equity = 1000. The snapshot reports cash 500 (agreeing exactly) and equity
    900 -- a 100 equity difference, four orders of magnitude beyond the 0.01
    default tolerance. Cash deliberately agrees so the drift can only come
    from the equity comparison."""
    acc = await create_account(conn, name="Drifting", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="DRFT", quote_currency="USD"),
    )
    await _deposit(conn, acc, "1000")
    await insert_fills(
        conn, [_position_fill(acc, inst, side=Side.BUY, quantity="5", price="100", ref="dr1")]
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("100"), _RECONCILE_AS_OF)
    await add_snapshot(
        conn, acc, _RECONCILE_AS_OF, cash_balance=Decimal("500"), total_equity=Decimal("900")
    )
    return acc


async def test_reconcile_reports_plain_drift(conn, drifting_account, monkeypatch, capsys):
    """The branch's second-most-important outcome, and until now the only
    verdict no CLI test exercised: OK, UNRELIABLE and four refusals were
    covered, plain DRIFT was not. Combined with a verdict chain that ended in
    an unguarded `return 1` carrying the UNRELIABLE narration, deleting the
    DRIFT branch entirely relabelled every drift as "could not be priced" --
    a wrong explanation attached to a real number -- and the suite stayed
    green. Hence the stderr assertions: the exit code and the `verdict:` line
    are identical under that mutation; the narration is not."""

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(drifting_account), as_of=None, tolerance=None)
    )
    captured = capsys.readouterr()
    out, err = captured.out.lower(), captured.err.lower()

    assert rc == 1
    assert "verdict: drift" in out
    # Nothing was unvaluable here, so the exclusion notice must NOT appear --
    # this is a clean numeric disagreement, not an unattributable one.
    assert "cannot be priced" not in out
    assert "disagree outside tolerance" in err
    assert "could not be priced" not in err


# --- Review round 2: the --as-of refusal branches, in both commands ----------
#
# `snapshot add` and `cmd_reconcile` now share ONE parser (cli._parse_as_of),
# so two of these four exercise the same helper as the other two. They are kept
# per-command deliberately: what each test pins is that ITS command routes
# --as-of through the parser at all and turns a None into exit 2. A future edit
# that inlined the parse back into one command, or dropped the `if ... is None:
# return 2` on one side, would leave the helper's own behaviour untouched and
# would only ever be caught by the per-command pair.
#
# Both refusals were reachable and both printed a distinct message with nothing
# asserting on either -- deleting either branch left the suite green.
#
# fake_create_pool RAISES rather than returning a stand-in: whether a string
# parses depends only on the argument, never on the database, so the refusal has
# to land before a connection is opened. That also makes these tests independent
# of any fixture -- a bogus account id is fine, because nothing ever looks it up.


async def test_snapshot_add_refuses_a_naive_as_of_timestamp(conn, monkeypatch, capsys):
    """A timestamp with no offset silently implies a wall-clock zone nobody
    named, and would reach `as_of > now + tolerance` -- an offset-naive vs
    offset-aware comparison -- as an uncaught TypeError. A bare DATE is
    deliberately still accepted (it means midnight UTC), so the message has to
    say what is missing rather than "not a valid date"."""

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_snapshot_add(
        _snapshot_args(
            account=str(uuid4()), as_of="2026-07-31T12:00",
            equity="1", cash="1", note=None,
        )
    )
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "utc offset" in err
    # Not the unparseable message: the two branches must stay distinguishable
    # to a reader, since the fix for each is different.
    assert "not a valid date or timestamp" not in err


async def test_snapshot_add_refuses_an_unparseable_as_of(conn, monkeypatch, capsys):
    """Neither a date nor a timestamp. Distinct from the naive-timestamp
    refusal above: nothing here can be fixed by appending an offset."""

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_snapshot_add(
        _snapshot_args(
            account=str(uuid4()), as_of="not-a-date",
            equity="1", cash="1", note=None,
        )
    )
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "not a valid date or timestamp" in err
    assert "not-a-date" in err


async def test_reconcile_refuses_a_naive_as_of_timestamp(conn, monkeypatch, capsys):
    """Same string, same refusal, on the sibling command -- the two disagreeing
    about how to read one --as-of value is exactly the defect the shared parser
    exists to prevent (it happened once already, with a bare date). Here a naive
    timestamp would otherwise reach latest_snapshot's `as_of <= $2` bind and
    surface as an asyncpg error instead of a clean message."""

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(uuid4()), as_of="2026-07-31T12:00", tolerance=None)
    )
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "utc offset" in err
    assert "not a valid date or timestamp" not in err


async def test_reconcile_refuses_an_unparseable_as_of(conn, monkeypatch, capsys):
    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    rc = await cli.cmd_reconcile(
        _reconcile_args(account=str(uuid4()), as_of="not-a-date", tolerance=None)
    )
    assert rc == 2
    err = capsys.readouterr().err.lower()
    assert "not a valid date or timestamp" in err
    assert "not-a-date" in err


# --- Task 4: the proposal surface (spec 2026-08-17, §7-§8) -----------------
# `cmd_import` never stores a corporate action -- `importers/` only proposes
# (Tasks 1-3), and `cli.py`'s job here is to render those proposals as a
# `corporate add` command a human reads, edits and runs themselves (D2/D3).
# All CSV text below is fabricated (fictional tickers, CUSIPs and
# reorganisation references), following the exact History-dialect row shapes
# verified against the real exports in
# tests/fixtures/fidelity/real_shape_history.csv and its module docstring
# (tests/test_fidelity_history.py) -- this file's own fixture, not reused
# here, since these tests want a reverse split whose quantities are 1800 and
# 300 (ratio 1:6) to match cli.py's own --ratio docstring example, not that
# fixture's 51/17 (ratio 1:3).

_HISTORY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)


def _write_history_csv(tmp_path: pathlib.Path, *rows: str) -> str:
    """Same tmp_path-backed idiom as `_write_routing_csv` above, but with the
    History dialect's header (no Account/Account Number columns) -- the only
    dialect that carries a corporate-action row at all."""
    path = tmp_path / "history.csv"
    path.write_text("\n".join([_HISTORY_HEADER, *rows]) + "\n")
    return str(path)


# A reverse split as a #REOR FROM/TO pair (spec §5), 1800 old shares into 300
# new -- reduces to 1:6, matching cli.py's own --ratio docstring example
# ("a 1-for-6 reverse split ... takes 1,800 shares to 300"). The TO row's
# Description states "1 FOR 6" too, so the derived and stated ratios agree
# and ratio_source comes out 'derived+confirmed' (spec §6a).
_REVERSE_SPLIT_ROWS = (
    '03/10/2024,REVERSE SPLIT R/S FROM 99911Q101#REOR N9990000010001 '
    'NINTH FABRICATED WIDGETS CORP COM (POST REV SPLIT) (99911Q209) (Cash),"",'
    'NINTH FABRICATED WIDGETS CORP COM (POST REV SPLIT) ISIN #ZX0000000099 '
    'SEDOL #BZQ0001,Cash,"",300,"","","",0.00,1006.25,""',
    '03/10/2024,REVERSE SPLIT R/S TO 99911Q209#REOR N9990000010000 '
    'NINTH FABRICATED WIDGETS CORP COM ISIN #ZX0000000088 1 FOR 6 R/S INTO '
    'NINTH FABRICATED WIDGETS CORP (99911Q101) (Cash),"",'
    'NINTH FABRICATED WIDGETS CORP COM ISIN #ZX0000000088 SEDOL #BZQ0002 '
    '1 FOR 6 R/S INTO NINTH FABRICATED WIDGETS CORP,Cash,"","-1800","","","",'
    '0.00,1006.25,""',
)

# A single-row spinoff (spec §5: no negative leg) distributing 60 child
# shares. No #REOR token -- same shape real_shape_history.csv's own spinoff
# row has -- so it groups on the (ex-date, symbol) fallback key.
_SPINOFF_ROW_TEMPLATE = (
    '{run_date},DISTRIBUTION SPINOFF FROM:(ZXCO ) TENTH FABRICATED VENTURES '
    'INC NEW WTS EXP 12/31/2030 (Cash),ZXQWS,TENTH FABRICATED VENTURES INC '
    'NEW WTS EXP 12/31/2030,Cash,"",60,"","","",0.00,992.03,""'
)

# A cash-in-lieu-of-fractional-shares row (spec §7, D6): recognised, reported
# separately, never applied.
_CASH_IN_LIEU_ROW = (
    '07/10/2024,IN LIEU OF FRX SHARE LEU PAYOUT 99911Q101 NINTH FABRICATED '
    'WIDGETS CORP COM (Cash),ZXQO,NINTH FABRICATED WIDGETS CORP COM,Cash,"",'
    '0,"","","",0.11,1006.36,""'
)

# An ordinary BUY of 500 ZXQ shares, same row shape as
# real_shape_history.csv's own "YOU BOUGHT" row -- for the regression test
# below, which imports this in the SAME file as a spinoff on the same
# symbol, exactly like a real multi-year History export (original purchase
# and a later corporate action together).
_BUY_ROW = (
    '01/15/2024,YOU BOUGHT NINTH FABRICATED WIDGETS CORP (ZXQ) (Cash),ZXQ,'
    'NINTH FABRICATED WIDGETS CORP,Cash,12.50,500,"",0.03,"","-6250.03",'
    '10000.00,01/17/2024'
)
_SPINOFF_ROW_FOR_ZXQ_TEMPLATE = (
    '{run_date},DISTRIBUTION SPINOFF FROM:(ZXQ ) TENTH FABRICATED VENTURES '
    'INC NEW WTS EXP 12/31/2030 (Cash),ZXQWS,TENTH FABRICATED VENTURES INC '
    'NEW WTS EXP 12/31/2030,Cash,"",60,"","","",0.00,992.03,""'
)

# The same spinoff with NO "FROM:(TICKER )" clause -- the one shape that
# leaves cli.py with nothing but its elimination rule ("the account's sole
# LONG holding at the ex-date") to identify the parent by. Every real spinoff
# row observed does state its parent, so this is the degrade path rather than
# the common one; it is kept exercised because the elimination rule is still
# the fallback and an untested fallback is how the CUSIP shape stayed wrong
# for a whole branch.
_SPINOFF_ROW_WITHOUT_A_STATED_PARENT_TEMPLATE = (
    '{run_date},DISTRIBUTION SPINOFF TENTH FABRICATED VENTURES '
    'INC NEW WTS EXP 12/31/2030 (Cash),ZXQWS,TENTH FABRICATED VENTURES INC '
    'NEW WTS EXP 12/31/2030,Cash,"",60,"","","",0.00,992.03,""'
)

# Same 1800-old/300-new quantities as _REVERSE_SPLIT_ROWS (still reduces to
# 1:6), but the TO row's Description states "1 FOR 5" instead of "1 FOR 6" --
# a stated ratio that DISAGREES with the derived one (spec §6a's
# cash-in-lieu-remainder / misparse case). ratio_source must come out
# 'derived', never 'derived+confirmed', and approximate must be True.
_REVERSE_SPLIT_ROWS_MISMATCHED = (
    '03/10/2024,REVERSE SPLIT R/S FROM 99922P101#REOR N9990000020001 '
    'NINTH FABRICATED WIDGETS CORP COM (POST REV SPLIT) (99922P209) (Cash),"",'
    'NINTH FABRICATED WIDGETS CORP COM (POST REV SPLIT) ISIN #ZX0000000199 '
    'SEDOL #BZP0001,Cash,"",300,"","","",0.00,1006.25,""',
    '03/10/2024,REVERSE SPLIT R/S TO 99922P209#REOR N9990000020000 '
    'NINTH FABRICATED WIDGETS CORP COM ISIN #ZX0000000188 1 FOR 5 R/S INTO '
    'NINTH FABRICATED WIDGETS CORP (99922P101) (Cash),"",'
    'NINTH FABRICATED WIDGETS CORP COM ISIN #ZX0000000188 SEDOL #BZP0002 '
    '1 FOR 5 R/S INTO NINTH FABRICATED WIDGETS CORP,Cash,"","-1800","","","",'
    '0.00,1006.25,""',
)

# A three-row merger (spec §5: always exactly three legs -- one PAYOUT
# negative leg, two positive resulting legs), same shape as
# real_shape_history.csv's own merger. Two positive rows means
# _derive_quantity_ratio (importers/fidelity.py) can never resolve a ratio
# for it -- structural, not a parsing gap (spec §6a).
_MERGER_ROWS = (
    '05/12/2024,MERGER MER FROM 99922P101#REOR N9990000030002 ELEVENTH '
    'FABRICATED RESOURCES CORP COM ISIN #ZX0000000299 SEDOL #BZP0004 '
    '(99922P407) (Cash),"",ELEVENTH FABRICATED RESOURCES CORP COM ISIN '
    '#ZX0000000299 SEDOL #BZP0004,Cash,"",9,"","","",0.00,1006.25,""',
    '05/12/2024,MERGER MER FROM 99922P101#REOR N9990000030001 TWELFTH '
    'FABRICATED METALS INC COM ISIN #ZX0000000300 SEDOL #BZP0005 '
    '(99922P505) (Cash),"",TWELFTH FABRICATED METALS INC COM ISIN '
    '#ZX0000000300 SEDOL #BZP0005,Cash,"",4,"","","",0.00,1006.25,""',
    '05/12/2024,MERGER MER PAYOUT #REOR N9990000030000 NINTH FABRICATED '
    'WIDGETS CORP COM (99922P101) (Cash),"",NINTH FABRICATED WIDGETS CORP '
    'COM ISIN #ZX0000000188 SEDOL #BZP0002 *REORGANIZATION*,Cash,"","-26",'
    '"","","","-14.22",992.03,""',
)

# A row whose Action verb matches no rule and whose Amount is non-zero --
# money-carrying and unmapped, so it lands in ImportBatch.blocking (spec §7)
# and refuses the whole commit. Used to prove the EARLY, non-ledger
# proposals (reverse_split/name_change/merger) still reach the user even
# when an unrelated row in the SAME file refuses the commit.
_BLOCKING_ROW = (
    '08/01/2024,MYSTERIOUS NEW ACTION NOT A KNOWN VERB,ZXP,MYSTERIOUS '
    'DISBURSEMENT,Cash,"",0,"","","","500.00",1006.75,""'
)

# Task 3: a plain DISTRIBUTION (share distribution, proposed as a "split"),
# the exact row shape verified in tests/fixtures/fidelity/real_shape_history.csv
# and pinned by tests/test_fidelity_history.py's
# test_a_plain_distribution_is_proposed_as_a_split -- 40 ZXDS shares received,
# ex-date 03/06/2026. ZXDS is fabricated (see that fixture's own docstring).
_SHARE_DISTRIBUTION_ROW_FOR_ZXDS = (
    '03/06/2026,DISTRIBUTION ZXDS HOLDINGS SPON ADS EA... (ZXDS) (Cash),ZXDS,'
    'ZXDS HOLDINGS SPON ADS EACH REP 1 ORD SHS,Cash,,40,,,,168,3878.55,'
)


async def test_import_proposes_a_corporate_add_command(conn, monkeypatch, capsys, tmp_path):
    """The importer proposes and never stores: a corporate action silently
    restates history across every account holding the instrument, which is why
    `corporate add` previews by default and refuses duplicates."""
    acc = await create_account(conn, name="Hist", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, *_REVERSE_SPLIT_ROWS),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "corporate add" in out
    assert "--ratio 1:6" in out
    # _REVERSE_SPLIT_ROWS is crafted so the derived ratio (from the paired
    # quantities) and the stated ratio ("1 FOR 6" in the TO row's
    # Description) agree -- spec §6a's strongest evidence, ratio_source
    # 'derived+confirmed'. Collapsing that distinction into plain 'derived'
    # (the wording for "only one source existed") must redden this.
    assert "two independent sources agree" in out


async def test_import_flags_an_approximate_ratio_end_to_end(conn, monkeypatch, capsys, tmp_path):
    """A reverse split whose stated ratio disagrees with the derived one --
    the cash-in-lieu-remainder / misparse case spec §6a exists to catch --
    must reach the user as a loud flag, with BOTH candidate ratios in the
    section itself, and with the command offering NEITHER of them.

    All three assertions below failed on real data before the final fix
    wave, where every reverse split takes this path:

    * the strength sentence read "no independent confirmation was found in
      the venue's own text" -- printed directly beneath a flag saying the
      venue's own text contradicts the number;
    * the stated ratio existed only in a `batch.warnings` entry bound for
      stderr, so the one figure needed to adjudicate the disagreement was
      absent from the stdout section D5 makes the decision surface;
    * `--ratio` offered the quantities-derived pair, which reproduces the
      share count of the ONE lot in the file and is wrong for every other
      lot and holder. Pasting it would store a ratio nobody declared,
      across every account holding the instrument."""
    acc = await create_account(conn, name="Hist", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, *_REVERSE_SPLIT_ROWS_MISMATCHED),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "APPROXIMATE" in out
    assert "DISPUTED" in out
    # BOTH candidates, in the section on stdout -- 1:6 from the quantities
    # (1800 -> 300) and 1:5 from the TO row's own "1 FOR 5" text.
    assert "derived from the paired quantities: 1:6" in out
    assert "stated in the venue's own text: 1:5" in out
    # ...and the command offers neither of them.
    assert "--ratio <FILL IN>" in out
    assert "--ratio 1:6" not in out
    assert "--ratio 1:5" not in out
    assert "INCOMPLETE" in out
    # The two strength sentences that must never appear here: the
    # confirmed one (nothing was confirmed) and the single-source one
    # (a second source existed -- it disagreed).
    assert "two independent sources agree" not in out
    assert "nothing to cross-check it against" not in out


async def test_import_renders_a_merger_as_incomplete(conn, monkeypatch, capsys, tmp_path):
    """A merger's ratio is structurally absent (spec §6a) -- one of the two
    kinds `--ratio required=True` cannot be satisfied for. This must render
    as visibly incomplete, with the structural reason stated, never as a
    false ready-to-run command."""
    acc = await create_account(conn, name="Hist", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, *_MERGER_ROWS),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "\nmerger ex" in out
    assert "ratio: UNAVAILABLE" in out
    assert "INCOMPLETE" in out
    assert "--ratio <FILL IN>" in out
    assert "structural" in out.lower()
    assert "not a parsing gap" in out.lower()


async def test_a_refused_commit_still_shows_the_non_ledger_proposals(
    conn, monkeypatch, capsys, tmp_path
):
    """History-dialect rows route the whole file to one account, so an
    unrelated, unmapped, money-carrying row in the SAME file refuses the
    whole commit (spec §7's blocking policy) and writes nothing -- but
    reverse_split, name_change and merger proposals need no ledger read
    (only a spinoff's does), so they must still reach the user even though
    the commit was refused. The user has to fix the refusal and re-run
    regardless; there is no reason to withhold proposals that never
    depended on anything having been written."""
    acc = await create_account(conn, name="Hist", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, *_REVERSE_SPLIT_ROWS, _BLOCKING_ROW),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc != 0

    out = capsys.readouterr().out
    assert "corporate add" in out
    assert "--ratio 1:6" in out


async def test_the_proposal_prints_the_evidence_it_derived_from(
    conn, monkeypatch, capsys, tmp_path
):
    """A ratio is an inference. Printing the quantities beside it is the one
    moment a human can catch an inverted or distorted one before it is stored."""
    acc = await create_account(conn, name="Hist", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, *_REVERSE_SPLIT_ROWS),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0
    assert "1800" in capsys.readouterr().out


async def test_nothing_is_written_by_a_proposal(
    conn, account_with_1800, monkeypatch, capsys, tmp_path
):
    """Not even with --commit. The import commits fills and cash; corporate
    actions are proposed only.

    Ruling D: list_actions(conn) with no instrument_id returns every action
    in the (shared, persistent) test database, so this scopes the check to
    the instrument account_with_1800 itself created."""
    account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, *_REVERSE_SPLIT_ROWS),
        account=str(account_id),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0
    assert await list_actions(conn, instrument_id) == []


async def test_the_spinoff_ratio_is_completed_from_the_ledger(
    conn, account_with_1800, monkeypatch, capsys, tmp_path
):
    """Not derivable from the file -- the row carries only the child shares.
    account_with_1800 holds 1800 ZXCO shares (its only instrument), executed
    before this test's 2026-03-15 ex-date, so it is unambiguously the
    spinoff's parent: 60 child shares over 1800 parent shares reduces to
    1:30."""
    account_id, _instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(
            tmp_path, _SPINOFF_ROW_TEMPLATE.format(run_date="03/15/2026")
        ),
        account=str(account_id),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "corporate add" in out
    assert "--ratio 1:30" in out
    assert "1800" in out
    assert "UNAVAILABLE" not in out


async def test_the_spinoff_ratio_is_completed_from_this_same_imports_own_fill(
    conn, monkeypatch, capsys, tmp_path
):
    """A real multi-year History export commonly carries the original
    purchase and a later spinoff in the SAME file (see
    tests/fixtures/fidelity/real_shape_history.csv) -- not split across a
    pre-existing ledger fixture and a separate import, the way
    test_the_spinoff_ratio_is_completed_from_the_ledger sets it up. The
    parent holding must be read AFTER this import's own BUY is committed
    (read-your-own-writes inside the same transaction), not before -- 500
    ZXQ shares bought, 60 child shares distributed, reduces to 3:25."""
    acc = await create_account(conn, name="Hist", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(
            tmp_path, _BUY_ROW, _SPINOFF_ROW_FOR_ZXQ_TEMPLATE.format(run_date="06/20/2024")
        ),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "corporate add" in out
    assert "--ratio 3:25" in out
    assert "UNAVAILABLE" not in out


async def test_the_spinoff_ratio_is_left_blank_when_the_parent_is_undeterminable(
    conn, monkeypatch, capsys, tmp_path
):
    """The account importing this file has no prior fills at all, so there is
    no ledger holding to divide by -- spec §7's "ratio blank, with a note
    saying why", not a guess and not a crash. (The row names ZXCO as its
    parent, so the note names it too; "no long position" is the answer either
    way when the account holds nothing at all.)"""
    acc = await create_account(conn, name="NoHoldings", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(
            tmp_path, _SPINOFF_ROW_TEMPLATE.format(run_date="03/15/2026")
        ),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "INCOMPLETE" in out
    assert "--ratio 1:30" not in out
    assert "holds no LONG position" in out


async def test_the_spinoff_ratio_is_left_blank_when_the_account_holds_more_than_one_instrument(
    conn, monkeypatch, capsys, tmp_path
):
    """Ambiguous, not guessed at: with a row that does NOT state its parent,
    an account holding two instruments as of the ex-date has no single
    candidate (spec §7), so the ratio stays blank and the reason names both
    symbols rather than picking one.

    This is now the FALLBACK path -- see the test below it, where the row
    states its parent and the same two-holding account resolves exactly.
    Kept because the elimination rule is still what runs when the venue says
    nothing, and because gap #47's corrected text turns on it: elimination
    reports "ambiguous" on 100% of the real accounts, which is why the
    stated parent had to be captured at all."""
    acc = await create_account(conn, name="TwoHoldings", venue="fidelity", account_type="cash")
    inst_a = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXQ", quote_currency="USD"),
    )
    inst_b = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXR", quote_currency="USD"),
    )
    before_ex_date = datetime(2026, 1, 1, tzinfo=UTC)
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst_a,
                executed_at=before_ex_date,
                side=Side.BUY,
                quantity=Decimal("100"),
                price=Decimal("1"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="ambiguous-a",
                is_estimated=False,
            ),
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst_b,
                executed_at=before_ex_date,
                side=Side.BUY,
                quantity=Decimal("200"),
                price=Decimal("1"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="ambiguous-b",
                is_estimated=False,
            ),
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(
            tmp_path,
            _SPINOFF_ROW_WITHOUT_A_STATED_PARENT_TEMPLATE.format(run_date="03/15/2026"),
        ),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "ambiguous" in out.lower()
    assert "ZXQ" in out
    assert "ZXR" in out


async def test_a_stated_parent_resolves_a_spinoff_an_account_of_many_holdings_could_not(
    conn, monkeypatch, capsys, tmp_path
):
    """The same two-holding account as the test above, and the same ex-date
    -- the only difference is that the row STATES its parent
    ("DISTRIBUTION SPINOFF FROM:(ZXQ )"), which the importer now captures.
    Elimination reports "ambiguous"; the stated parent answers exactly:
    60 child shares against the 100 ZXQ held, reduced to 3:5.

    This is the whole of gap #47's correction, made falsifiable. Measured
    against the real exports, the elimination rule never once resolved --
    the account receiving the only real spinoff was long many instruments at
    its ex-date -- so every piece of machinery behind this (the EARLY/LATE
    split, the in-transaction print, read-your-own-writes) was correct and
    never fired.

    The ZXR holding is deliberately the LARGER one: a regression that picked
    "the biggest position" instead of the named one would produce 3:10, not
    3:5, and redden here rather than passing by coincidence."""
    acc = await create_account(conn, name="StatedParent", venue="fidelity", account_type="cash")
    inst_a = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXQ", quote_currency="USD"),
    )
    inst_b = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXR", quote_currency="USD"),
    )
    before_ex_date = datetime(2026, 1, 1, tzinfo=UTC)
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst_a,
                executed_at=before_ex_date,
                side=Side.BUY,
                quantity=Decimal("100"),
                price=Decimal("1"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="stated-parent-a",
                is_estimated=False,
            ),
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst_b,
                executed_at=before_ex_date,
                side=Side.BUY,
                quantity=Decimal("200"),
                price=Decimal("1"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="stated-parent-b",
                is_estimated=False,
            ),
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(
            tmp_path, _SPINOFF_ROW_FOR_ZXQ_TEMPLATE.format(run_date="03/15/2026")
        ),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "--ratio 3:5" in out
    assert "UNAVAILABLE" not in out
    assert "ambiguous" not in out.lower()
    # The note must say the parent was named rather than inferred -- the
    # two are different claims about how much this ratio can be trusted.
    assert "named by the venue's own row" in out


async def test_a_stated_parent_the_account_does_not_hold_refuses_rather_than_eliminating(
    conn, monkeypatch, capsys, tmp_path
):
    """The row names ZXQ; the account is long only ZXR. Falling back to the
    elimination rule here would divide by ZXR -- a security the spinoff has
    nothing to do with -- and produce a confident 3:10 that contradicts the
    venue's own row. Spec §7: report, never guess.

    The realistic cause is a parent purchase living in a file that has not
    been imported yet (real History exports are per-year), so the note says
    so rather than merely refusing."""
    acc = await create_account(conn, name="WrongParent", venue="fidelity", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXR", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst,
                executed_at=datetime(2026, 1, 1, tzinfo=UTC),
                side=Side.BUY,
                quantity=Decimal("200"),
                price=Decimal("1"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="wrong-parent-b",
                is_estimated=False,
            ),
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(
            tmp_path, _SPINOFF_ROW_FOR_ZXQ_TEMPLATE.format(run_date="03/15/2026")
        ),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "--ratio <FILL IN>" in out
    assert "names ZXQ as the parent" in out
    # The number elimination would have produced, and must not.
    assert "3:10" not in out


async def test_the_spinoff_ratio_is_left_blank_when_the_account_is_short(
    conn, monkeypatch, capsys, tmp_path
):
    """A spinoff is received on shares you are LONG. A net-short holding
    (here: a SELL with no offsetting BUY) must not qualify as the parent --
    `HAVING SUM(...) > 0`, not `<> 0` -- or a negative "holding" would
    produce a nonsensical ratio like 60:-100 that only cmd_corporate_add's
    own positivity check would catch, far downstream of where the mistake
    actually happened.

    The `HAVING SUM(...) > 0` under test is shared by both parent-selection
    paths (stated ticker and elimination), which is why the query filters
    rather than each path checking for itself -- this row states its parent,
    so it exercises the stated path."""
    acc = await create_account(conn, name="Short", venue="fidelity", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXQ", quote_currency="USD"),
    )
    before_ex_date = datetime(2026, 1, 1, tzinfo=UTC)
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst,
                executed_at=before_ex_date,
                side=Side.SELL,
                quantity=Decimal("100"),
                price=Decimal("1"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="short-only",
                is_estimated=False,
            ),
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(
            tmp_path, _SPINOFF_ROW_FOR_ZXQ_TEMPLATE.format(run_date="03/15/2026")
        ),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "holds no LONG position" in out
    assert "-100" not in out
    assert "--ratio <FILL IN>" in out


# --- Task 3: the split ratio completed from the ledger ---------------------


async def test_the_split_ratio_is_completed_from_the_holding(
    conn, monkeypatch, capsys, tmp_path
):
    """(held + received) : held, reduced. The row states only what was
    received; what it was received ON is in the ledger. Holding 60 and
    receiving 40 is 100:60 -> 5:3."""
    acc = await create_account(conn, name="Dist", venue="fidelity", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXDS", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(), account_id=acc, instrument_id=inst,
                executed_at=datetime(2026, 1, 5, tzinfo=UTC), side=Side.BUY,
                quantity=Decimal("60"), price=Decimal("4"), fee=Decimal("0"),
                fee_currency="USD", source=FillSource.MANUAL,
                venue_fill_id="dist-open", is_estimated=False,
            ),
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "ratio: 5:3" in out
    assert "--type split" in out
    assert "--symbol ZXDS" in out
    assert "<FILL IN>" not in out


async def test_the_split_ratio_is_left_blank_when_the_holding_is_absent(
    conn, monkeypatch, capsys, tmp_path
):
    """The year-file carrying the purchase has not been imported. Report and
    stop -- never substitute another instrument, and never guess a ratio."""
    acc = await create_account(conn, name="Empty", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "--ratio <FILL IN>" in out
    assert "holds no LONG position" in out


async def test_a_short_holding_does_not_qualify_for_a_split_ratio(
    conn, monkeypatch, capsys, tmp_path
):
    """Shares are distributed on shares you are LONG. HAVING SUM(...) > 0,
    not <> 0 -- a net-short holding would otherwise produce a nonsensical
    negative ratio that only cmd_corporate_add's positivity check would
    catch, far downstream of the mistake."""
    acc = await create_account(conn, name="Short", venue="fidelity", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXDS", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(), account_id=acc, instrument_id=inst,
                executed_at=datetime(2026, 1, 5, tzinfo=UTC), side=Side.SELL,
                quantity=Decimal("60"), price=Decimal("4"), fee=Decimal("0"),
                fee_currency="USD", source=FillSource.MANUAL,
                venue_fill_id="dist-short", is_estimated=False,
            ),
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "holds no LONG position" in out


# --- Final review: three ways the split completion could answer wrongly ----


async def test_the_split_note_prints_both_quantities(conn, monkeypatch, capsys, tmp_path):
    """F9. The note used to name only the symbol -- "{symbol} holding at the
    ex-date plus the shares this row delivered" -- while the sibling spinoff
    note printed both of its quantities.

    D2's whole posture is to force an INFORMED human decision before a
    corporate action restates history across every account holding the
    instrument. A reader who cannot see the two numbers the ratio came from
    cannot check it, and gap #53 (the ratio is computed from RAW fills, which
    a prior split makes wrong) is exactly the failure that only shows up when
    someone reads the held figure and recognises it."""
    acc = await create_account(conn, name="Both", venue="fidelity", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXDS", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(), account_id=acc, instrument_id=inst,
                executed_at=datetime(2026, 1, 5, tzinfo=UTC), side=Side.BUY,
                quantity=Decimal("60"), price=Decimal("4"), fee=Decimal("0"),
                fee_currency="USD", source=FillSource.MANUAL,
                venue_fill_id="dist-both", is_estimated=False,
            ),
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "ratio: 5:3" in out
    # Both halves of (held + received) : held, not just the reduced result.
    assert "40 share(s) delivered by this row" in out
    assert "60 ZXDS share(s) held at 2026-03-06" in out


async def test_two_instruments_sharing_a_symbol_make_the_split_ratio_ambiguous(
    conn, monkeypatch, capsys, tmp_path
):
    """F4. `instrument.symbol` is NOT unique -- only `natural_key` is
    (db/schema.sql) -- so narrowing _long_holdings_as_of by symbol can return
    several instruments, and GROUP BY i.id keeps them apart. The completion
    used to take rows[0] with no length check at all and print a confident
    ratio derived from whichever one the database happened to return first,
    while its sibling _complete_spinoff_ratio reported "ambiguous" for the
    same shape.

    A dual-listed equity is the concrete case: same ticker, two quote
    currencies, two natural keys, one symbol."""
    acc = await create_account(conn, name="Dual", venue="fidelity", account_type="cash")
    usd = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXDS", quote_currency="USD"),
    )
    cad = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXDS", quote_currency="CAD"),
    )
    assert usd != cad, "two distinct instruments, one symbol -- the premise of this test"
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(), account_id=acc, instrument_id=inst_id,
                executed_at=datetime(2026, 1, 5, tzinfo=UTC), side=Side.BUY,
                quantity=Decimal("60"), price=Decimal("4"), fee=Decimal("0"),
                fee_currency="USD", source=FillSource.MANUAL,
                venue_fill_id=f"dist-dual-{n}", is_estimated=False,
            )
            for n, inst_id in enumerate((usd, cad))
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "2 distinct instruments under the symbol ZXDS" in out
    assert "--ratio <FILL IN>" in out
    # And no ratio is offered anywhere: taking rows[0] would have printed
    # 5:3 off one of the two 60-share holdings.
    assert "ratio: 5:3" not in out


async def test_a_non_finite_holding_is_excluded_rather_than_reduced(
    conn, monkeypatch, capsys, tmp_path
):
    """F5. `fill.quantity`'s only CHECK is `quantity > 0` (db/schema.sql), and
    Postgres NUMERIC orders 'NaN' ABOVE every finite value, so a NaN quantity
    passes it -- migration 002_reject_non_finite_numerics.sql added the
    `< 'Infinity'` bound to contract_multiplier and price, never to
    fill.quantity. A NaN reaching _reduce_decimal_ratio does not produce a
    wrong ratio, it produces NO RETURN: bool(Decimal("NaN")) is True and
    NaN % x is NaN, so the Euclidean loop spins forever -- inside cmd_import's
    open commit transaction, holding its locks.

    The UPDATE below is the point of the test, not a shortcut around a
    validator: it demonstrates that the CHECK constraint accepts the value.
    If a future migration adds the `< 'Infinity'` bound to fill.quantity, this
    UPDATE starts raising and this test fails loudly, which is the correct
    signal that the seam guard is now belt-and-braces."""
    acc = await create_account(conn, name="NaN", venue="fidelity", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXDS", quote_currency="USD"),
    )
    fill_id = uuid4()
    await insert_fills(
        conn,
        [
            Fill(
                id=fill_id, account_id=acc, instrument_id=inst,
                executed_at=datetime(2026, 1, 5, tzinfo=UTC), side=Side.BUY,
                quantity=Decimal("60"), price=Decimal("4"), fee=Decimal("0"),
                fee_currency="USD", source=FillSource.MANUAL,
                venue_fill_id="dist-nan", is_estimated=False,
            ),
        ],
    )
    await conn.execute(
        "UPDATE fill SET quantity = 'NaN'::numeric WHERE id = $1", fill_id
    )
    assert await conn.fetchval("SELECT quantity > 0 FROM fill WHERE id = $1", fill_id), (
        "the premise: the CHECK admits NaN because NaN sorts above every "
        "finite value"
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    # Reaching this line at all is most of the assertion -- un-guarded, this
    # call never returns.
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "holds no LONG position" in out, (
        "a holding that is not a number is reported as no holding, never as "
        "a ratio"
    )


async def test_a_split_command_is_not_told_to_fill_in_a_symbol_it_already_has(
    conn, monkeypatch, capsys, tmp_path
):
    """T3-a. The INCOMPLETE reminder was printed unconditionally as "fill in
    --ratio (and --symbol)". For a share distribution the rendered command
    already carries the row's own stated ticker, so that sentence tells a
    human to overwrite a correct value on a command they are about to run
    against a real ledger.

    This runs the holding-absent path, which is where a split reaches the
    reminder at all."""
    acc = await create_account(conn, name="NoSym", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "INCOMPLETE -- fill in --ratio before running:" in out
    assert "(and --symbol)" not in out
    # The command it introduces really does carry the ticker already.
    assert "--symbol ZXDS" in out


async def test_cash_in_lieu_is_reported_as_unapplied(conn, monkeypatch, capsys, tmp_path):
    """It moves real cash and the ledger does not reflect it. Saying so is the
    whole mitigation."""
    acc = await create_account(conn, name="Hist", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _CASH_IN_LIEU_ROW),
        account=str(acc),
        commit=True,
    )
    rc = await cli.cmd_import(args)
    assert rc == 0
    assert "not applied" in capsys.readouterr().out.lower()


# --- `deadband corporate` (spec 2026-08-15, §5-§6) -------------------------
# The engine (ledger/corporate.py's adjust_fills) and the storage layer
# (db/corporate.py) are both already tested. What is new here is the WIRING:
# that a stored action reaches adjust_fills and that its result reaches the
# materialised `trade` rows positions are read from. `_split` and
# `account_with_1800` come from tests/db/conftest.py rather than being
# redefined here -- the fixture's single BUY of 1800 is executed 2026-02-01,
# deliberately BEFORE the 2026-03-02 ex-date these tests use, so the split
# actually applies to it.


def _corporate_args(**kw):
    """Same hand-built argparse.Namespace pattern as `_args` (marks) and
    `_snapshot_args` (snapshots) above, named distinctly from both so it cannot
    silently widen either.

    Every flag the three `corporate` subparsers register is present, defaulted
    the way argparse would default it, so a handler reading a flag this call
    didn't pass sees the parser's own default rather than an AttributeError the
    real CLI could never produce. An unrecognised keyword is a hard error
    instead of a silently-added attribute: a typo'd `ex_date=` would otherwise
    leave the real flag at None and the test would pass for the wrong reason.
    """
    defaults = {
        "type": None,
        "symbol": None,
        "ex_date": None,
        "ratio": None,
        "resulting_symbol": None,
        "basis_allocation": None,
        "note": None,
        "id": None,
        "commit": False,
    }
    unknown = set(kw) - set(defaults)
    if unknown:
        raise TypeError(f"no such corporate flag(s): {', '.join(sorted(unknown))}")
    return argparse.Namespace(**{**defaults, **kw})


async def test_corporate_add_previews_without_writing(conn, account_with_1800, monkeypatch, capsys):
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="reverse_split", symbol="ZXCO", ex_date="2026-03-02", ratio="1:6", commit=False
        )
    )
    assert rc == 0
    assert await list_actions(conn, instrument_id) == []
    out = capsys.readouterr().out
    assert "preview only" in out
    # The adjusted quantity, not just a row count: a preview that prints "1
    # fill affected" without saying what it becomes cannot catch an inverted
    # ratio, which is the mistake the spec singles out.
    assert "300" in out


async def test_corporate_add_commits_and_regroups(conn, account_with_1800, monkeypatch):
    """Positions come from materialised trade rows, so an add that does not
    regroup leaves them silently stale."""
    account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="reverse_split", symbol="ZXCO", ex_date="2026-03-02", ratio="1:6", commit=True
        )
    )
    assert rc == 0
    assert len(await list_actions(conn, instrument_id)) == 1
    (position,) = await open_positions(conn, account_id)
    # 1800 * 1/6. An inverted --ratio would make this 10800 and every
    # individual step would still look plausible.
    assert position.quantity == Decimal(300)


async def test_corporate_add_refuses_a_duplicate_without_writing(
    conn, account_with_1800, monkeypatch, capsys
):
    """The same 1:6 split entered twice is a 1:36 restatement that looks
    plausible at every individual step."""
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    args = _corporate_args(
        type="reverse_split", symbol="ZXCO", ex_date="2026-03-02", ratio="1:6", commit=True
    )
    assert await cli.cmd_corporate_add(args) == 0
    assert await cli.cmd_corporate_add(args) == 2
    assert len(await list_actions(conn, instrument_id)) == 1
    assert "already" in capsys.readouterr().err.lower()


@pytest.mark.parametrize(
    ("action_type", "flag", "value", "named"),
    [
        ("split", "resulting_symbol", "ZXCB", "--resulting-symbol"),
        ("reverse_split", "resulting_symbol", "ZXCB", "--resulting-symbol"),
        ("split", "basis_allocation", "0.25", "--basis-allocation"),
        ("reverse_split", "basis_allocation", "0.25", "--basis-allocation"),
    ],
)
async def test_corporate_add_refuses_a_flag_the_type_does_not_use(
    conn, account_with_1800, monkeypatch, capsys, action_type, flag, value, named
):
    """A flag the type does not use is not harmless decoration.

    `--type split --resulting-symbol ZXCB` was accepted and STORED, and
    `actions_with_ids_for_instruments` (db/corporate.py) matches on
    `resulting_instrument_id` as well as `instrument_id` -- so that row joined
    ZXCB's action set, entered `_ordered_actions`' dependency graph, and could
    raise `ValueError: circular corporate-action dependency` out of
    `adjust_fills`, inside `regroup_account`, for every account holding either
    instrument, on every regroup including `import --commit`, naming neither the
    offending action nor how to remove it.

    Flag-level, so it must refuse before any connection is opened -- ZXCB is
    never created here, and a version that resolved symbols first would refuse
    for the wrong reason.
    """
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type=action_type, symbol="ZXCO", ex_date="2026-03-02", ratio="1:6",
            commit=True, **{flag: value},
        )
    )
    assert rc == 2
    err = capsys.readouterr().err
    # The offending flag and the type are both named: "error: bad flags" would
    # leave the user to guess which of the two to drop.
    assert named in err
    assert f"--type {action_type}" in err
    assert "does not use" in err
    assert await list_actions(conn, instrument_id) == []


async def test_corporate_add_refuses_a_malformed_ratio_without_writing(
    conn, account_with_1800, monkeypatch
):
    """Whether a ratio parses depends only on the argument, never on the
    database, so this too must be refused before a connection is opened."""
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="reverse_split", symbol="ZXCO", ex_date="2026-03-02",
            ratio="one-to-six", commit=True,
        )
    )
    assert rc == 2
    assert await list_actions(conn, instrument_id) == []


@pytest.mark.parametrize("bad_ratio", ["1:NaN", "Infinity:6", "1:0", "-1:6", "1:6:2", "1:six"])
async def test_corporate_add_refuses_a_ratio_component_that_is_not_positive_and_finite(
    conn, account_with_1800, monkeypatch, bad_ratio
):
    """Decimal("NaN") and Decimal("Infinity") CONSTRUCT successfully and slip
    past the InvalidOperation catch entirely -- is_finite() is this codebase's
    established guard (see cmd_marks_set). A zero or negative component would
    reach CorporateAction.__post_init__ and surface as an uncaught ValueError
    traceback rather than a clean refusal."""
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="reverse_split", symbol="ZXCO", ex_date="2026-03-02",
            ratio=bad_ratio, commit=True,
        )
    )
    assert rc == 2
    assert await list_actions(conn, instrument_id) == []


async def test_corporate_add_refuses_an_unparseable_ex_date(conn, account_with_1800, monkeypatch):
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="reverse_split", symbol="ZXCO", ex_date="not-a-date", ratio="1:6", commit=True
        )
    )
    assert rc == 2
    assert await list_actions(conn, instrument_id) == []


async def test_corporate_add_commits_a_symbol_change(conn, account_with_1800, zxcb, monkeypatch):
    """The refusal added in 8292e9e (docs/known-gaps.md gap #39) comes off here.
    Before Tasks 1-3, this stored an action whose effect was reported under the
    OLD instrument -- db/positions.py resolved a position's instrument from the
    raw opening fill, which the adjustment never rewrote."""
    account_id, _instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="symbol_change", symbol="ZXCO", ex_date="2026-03-02", ratio="1:1",
            resulting_symbol="ZXCB", commit=True,
        )
    )
    assert rc == 0
    (position,) = await open_positions(conn, account_id)
    assert position.symbol == "ZXCB"
    assert position.quantity == Decimal(1800)


async def test_corporate_add_commits_a_spinoff(conn, account_with_1800, zxcb, monkeypatch):
    """1800 shares, 1:10 spinoff, 37.5% of basis allocated -> the parent keeps
    reporting 1800 (spinoffs only rescale basis, not quantity) and a new 180
    share ZXCB position appears, persisted as a `derived_fill` row (Task 3)."""
    account_id, _instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="spinoff", symbol="ZXCO", ex_date="2026-03-02", ratio="1:10",
            resulting_symbol="ZXCB", basis_allocation="0.375", commit=True,
        )
    )
    assert rc == 0
    positions = {p.symbol: p for p in await open_positions(conn, account_id)}
    assert positions.keys() == {"ZXCO", "ZXCB"}
    assert positions["ZXCO"].quantity == Decimal(1800)
    assert positions["ZXCB"].quantity == Decimal(180)


async def test_corporate_add_previews_a_spinoffs_new_position_without_writing(
    conn, account_with_1800, zxcb, monkeypatch, capsys
):
    """`_print_effect`'s `for fill in preview.created:` loop (cli.py) is the
    only thing that ever renders the child a spinoff preview is about to
    create. `preview_effect`'s `created` list is pinned by
    tests/db/test_corporate_actions.py's
    test_preview_of_an_added_spinoff_reports_the_child_it_creates, but nothing
    asserted that rendering ever reached a user before this test existed --
    `corporate add` previews by default, so a preview that silently omitted
    the position it is about to create was exactly the failure Task 3 existed
    to remove, and only the inner half (preview_effect itself) was pinned.

    The input that would make this fail: deleting the `for fill in
    preview.created:` loop at the bottom of `_print_effect` (cli.py). That
    change leaves `preview.fills_changed` and the parent's basis reduction
    still printed -- this test's assertions on "new:", "180.00" and "0.1875"
    are what catch the child going unmentioned; a version of this test that
    only checked `rc == 0` or "preview only" would stay green.
    """
    account_id, instrument_id = account_with_1800
    # account_with_1800 only inserts the fill -- positions are read from
    # materialised `trade` rows (db/positions.py), not fills directly, so
    # without this the account has no position at all yet and "nothing
    # changed" below would hold vacuously regardless of what --commit=False
    # did. Regrouping first gives a real ZXCO position to prove untouched.
    await regroup_account(conn, account_id)

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="spinoff", symbol="ZXCO", ex_date="2026-03-02", ratio="1:10",
            resulting_symbol="ZXCB", basis_allocation="0.375", commit=False,
        )
    )
    assert rc == 0

    out = capsys.readouterr().out
    assert "preview only" in out
    # 1800 * 1/10 shares at (1800 * 0.05 * 0.375) / 180 -- same figures
    # test_preview_of_an_added_spinoff_reports_the_child_it_creates pins on
    # `preview.created` directly. Checked against imports/ (`grep -rn
    # "0\.1875" imports/` and `grep -rnw "180" imports/`, both empty): neither
    # is a real broker figure this repo could be mistaken for.
    assert "new: 180.00 @ 0.1875" in out

    # Nothing written: no action stored, and the pre-existing ZXCO position is
    # unchanged with no ZXCB position minted alongside it.
    assert await list_actions(conn, instrument_id) == []
    positions = {p.symbol: p for p in await open_positions(conn, account_id)}
    assert positions.keys() == {"ZXCO"}
    assert positions["ZXCO"].quantity == Decimal(1800)


async def test_a_mark_on_the_new_symbol_prices_the_position_after_a_symbol_change(
    conn, account_with_1800, zxcb, monkeypatch, capsys
):
    """Spec §8, and Half A's whole user-visible payoff. Marks are looked up by
    `position.instrument_id`, which since this branch comes from
    `COALESCE(t.effective_instrument_id, f.instrument_id)` (db/positions.py) --
    so a mark set on the instrument the position moved TO is what prices it.
    That was sound by inspection and asserted nowhere.

    A mark is deliberately set on BOTH instruments, at different prices. A test
    that marked only ZXCB could not tell "priced by the new symbol" from "priced
    by whatever mark happens to exist"; with both set, the unrealized figure
    names which one was used.

    The inputs that would make this fail:
      * dropping the COALESCE, so the position resolves back to ZXCO -- the row
        then prints 1674.00 (the ZXCO mark) instead of 702.00;
      * dropping `effective_instrument_id` from the trade upsert's DO UPDATE SET
        or its INSERT column list -- the position resolves to ZXCO the same way;
      * `latest_marks` keyed on the opening fill's instrument rather than the
        position's -- same 1674.00.
    """
    account_id, instrument_id = account_with_1800
    await add_action(conn, _symbol_change(instrument_id, zxcb))
    await regroup_account(conn, account_id)
    now = datetime.now(UTC)
    await set_mark(conn, zxcb, Decimal("0.44"), now)
    # The decoy: a mark on the instrument the position is no longer reported
    # under. Nothing must reach for it.
    await set_mark(conn, instrument_id, Decimal("0.98"), now)

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_positions(_positions_args(account=str(account_id)))
    assert rc == 0, capsys.readouterr().err
    out = capsys.readouterr().out

    assert "ZXCB" in out
    assert "ZXCO" not in out
    # The mark column carries the price and its date, so an unpriced row would
    # render "--" here instead.
    assert "0.44 @" in out
    assert "702.00" in out    # (0.44 - 0.05) * 1800 * 1
    assert "1674.00" not in out  # what the ZXCO decoy mark would have produced


async def test_trades_and_positions_agree_on_the_symbol_after_a_symbol_change(
    conn, account_with_1800, zxcb, monkeypatch, capsys
):
    """Spec §8. The two commands read different columns for the same fact:
    `deadband trades` prints `trade.primary_underlying`, written by
    regroup_account from the ADJUSTED fill's instrument, while `deadband
    positions` prints `instrument.symbol` resolved through
    `COALESCE(t.effective_instrument_id, f.instrument_id)`. Before this branch
    those disagreed after a symbol change -- trades said ZXCB, positions said
    ZXCO -- which is the exact split gap #38 recorded.

    Compares the two rendered symbol fields to each OTHER, not each to a
    hardcoded "ZXCB" separately: two independent assertions can both be edited
    to a new expected value and stay green while the commands disagree. This
    one cannot pass unless they match.

    The input that would make it fail: reverting db/positions.py's COALESCE to
    `f.instrument_id`. `trades` still prints ZXCB (primary_underlying comes from
    the adjusted fill), `positions` prints ZXCO, and the equality below breaks.
    """
    account_id, instrument_id = account_with_1800
    await add_action(conn, _symbol_change(instrument_id, zxcb))
    await regroup_account(conn, account_id)

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    assert await cli.cmd_trades(argparse.Namespace(account=str(account_id))) == 0
    trades_out = capsys.readouterr().out
    assert await cli.cmd_positions(_positions_args(account=str(account_id))) == 0
    positions_out = capsys.readouterr().out

    # One fill, so exactly one row from each command. `trades` renders
    # "<date>  <symbol> <direction> ...", `positions` renders
    # "<symbol> <account> ...".
    (trades_row,) = trades_out.splitlines()
    (positions_row,) = positions_out.splitlines()
    assert trades_row.split()[1] == positions_row.split()[0] == "ZXCB"


async def test_corporate_add_refuses_a_merger_with_no_resulting_symbol(
    conn, account_with_1800, monkeypatch, capsys
):
    """RESTORED from PR #10, which deleted it as vacuous once merger was refused
    outright. The guard it covers (cli.py's `_RESULTING_INSTRUMENT_TYPES` check)
    is live code again now that the type-level refusal is gone. Flag-level, so
    it must refuse before any connection is opened -- ZXCB is never created
    here, and a version that resolved symbols first would refuse for the wrong
    reason."""
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="merger", symbol="ZXCO", ex_date="2026-03-02", ratio="1:6", commit=True
        )
    )
    assert rc == 2
    assert "resulting" in capsys.readouterr().err.lower()
    assert await list_actions(conn, instrument_id) == []


async def test_corporate_add_refuses_a_spinoff_with_no_basis_allocation(
    conn, account_with_1800, monkeypatch, capsys
):
    """RESTORED from PR #10. Refuses in stage 1, before a connection is opened
    -- which is why ZXCB never needs to exist for this test."""
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="spinoff", symbol="ZXCO", ex_date="2026-03-02", ratio="1:1",
            resulting_symbol="ZXCB", commit=True,
        )
    )
    assert rc == 2
    assert "basis" in capsys.readouterr().err.lower()
    assert await list_actions(conn, instrument_id) == []


@pytest.mark.parametrize("allocation", ["NaN", "abc"])
async def test_corporate_add_refuses_a_basis_allocation_that_is_not_a_finite_number(
    conn, account_with_1800, monkeypatch, capsys, allocation
):
    """RESTORED from PR #10. InvalidOperation is NOT a ValueError subclass, and
    an ordering comparison against Decimal('NaN') raises rather than returning
    False -- so the is_finite() guard is load-bearing, not decorative. Also
    stage 1: whether a basis allocation parses does not depend on the
    database."""
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="spinoff", symbol="ZXCO", ex_date="2026-03-02", ratio="1:10",
            resulting_symbol="ZXCB", basis_allocation=allocation, commit=True,
        )
    )
    assert rc == 2
    assert await list_actions(conn, instrument_id) == []


async def test_corporate_add_refuses_an_unknown_symbol(
    conn, account_with_1800, monkeypatch, capsys
):
    """Follows test_marks_set_refuses_an_ambiguous_symbol_without_writing:
    db.marks.resolve_instrument_by_symbol's OWN message must survive into
    stderr, not just some exit code. A `print` that dropped `{exc}` -- or a
    handler that returned 2 for an entirely unrelated reason -- would satisfy
    a bare `rc == 2` and tell the user nothing about which symbol failed."""
    _account_id, instrument_id = account_with_1800

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="reverse_split", symbol="NOSUCH", ex_date="2026-03-02", ratio="1:6", commit=True
        )
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "no instrument with symbol" in err
    assert "NOSUCH" in err
    # Scoped to the one instrument this test's fixture created, never an
    # unqualified count: the test database is shared and `instrument` rows are
    # global.
    assert await list_actions(conn, instrument_id) == []


async def test_corporate_add_allows_an_instrument_with_no_fills(conn, monkeypatch, capsys):
    """Spec §6's last row: an action on an instrument nobody holds is a
    legitimately pre-recorded future action, so it is ALLOWED and reports that
    nothing is affected -- it must not be mistaken for an error, and it must
    still be stored."""
    instrument_id = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="split", symbol="ZXCO", ex_date="2026-03-02", ratio="3:1", commit=True
        )
    )
    assert rc == 0
    assert len(await list_actions(conn, instrument_id)) == 1
    assert "no fills affected" in capsys.readouterr().out


async def test_corporate_list_shows_stored_actions(conn, account_with_1800, monkeypatch, capsys):
    _account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _split(instrument_id))

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_list(_corporate_args(symbol="ZXCO"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "reverse_split" in out
    # The id is what `corporate remove` takes, so a listing that omits it
    # cannot do the job spec §5 gives it.
    assert str(action_id) in out


async def test_corporate_remove_undoes_the_adjustment(conn, account_with_1800, monkeypatch):
    account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)
    # Guard: if the split never reached the position in the first place, the
    # 1800 asserted below would be true whether or not remove did anything.
    (adjusted,) = await open_positions(conn, account_id)
    assert adjusted.quantity == Decimal(300)

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_remove(_corporate_args(id=str(action_id), commit=True))
    assert rc == 0
    assert await list_actions(conn, instrument_id) == []
    (position,) = await open_positions(conn, account_id)
    assert position.quantity == Decimal(1800)


async def test_corporate_remove_previews_without_deleting(
    conn, account_with_1800, monkeypatch, capsys
):
    """`remove` mirrors `add`: preview by default, write only with --commit."""
    account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_remove(_corporate_args(id=str(action_id), commit=False))
    assert rc == 0
    assert "preview only" in capsys.readouterr().out
    assert len(await list_actions(conn, instrument_id)) == 1
    (position,) = await open_positions(conn, account_id)
    assert position.quantity == Decimal(300)


async def test_corporate_remove_refuses_an_unknown_id(conn, account_with_1800, monkeypatch, capsys):
    """A bare `rc == 2` would hold for a handler that refused for an unrelated
    reason, and would not notice a `remove` that deleted the wrong row on its
    way to reporting failure. So: the refusal names the id that was not found,
    and a real stored action -- one this test created, on a different id --
    is still there afterwards, with the position it adjusts untouched."""
    account_id, instrument_id = account_with_1800
    kept = await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)
    missing = uuid4()

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_remove(_corporate_args(id=str(missing), commit=True))
    assert rc == 2
    assert str(missing) in capsys.readouterr().err
    assert [r["id"] for r in await list_actions(conn, instrument_id)] == [kept]
    (position,) = await open_positions(conn, account_id)
    assert position.quantity == Decimal(300)


async def test_corporate_remove_refuses_a_malformed_id(conn, monkeypatch):
    """main()'s UUID guard covers --account only, so a mistyped positional id
    would otherwise surface as a raw ValueError traceback."""

    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must refuse before opening a connection")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_remove(_corporate_args(id="not-a-uuid", commit=True))
    assert rc == 2


# Deliberately BEFORE the 2026-03-02 ex-date every corporate test here uses.
# `_position_fill` above executes at 2026-08-01, which is AFTER it, so a
# fixture built on that helper unmodified would put both fills on the wrong
# side of the split and every assertion below would pass vacuously -- the same
# trap tests/db/conftest.py's own `_T0` comment describes.
_PRE_EX_DATE = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def two_accounts_holding_zxco(conn):
    """TWO accounts, each holding a BUY on the SAME fabricated ZXCO equity.

    `account_with_1800` (tests/db/conftest.py) is a single account, which is
    why the mutation "regroup only the first holding account" survived the
    whole gate: with one holder, `account_ids[:1]` and `account_ids` are the
    same list. This is the fixture that tells them apart.

    The two quantities are DIFFERENT (1800 and 600 -> 300 and 100 after a 1:6
    reverse split) so the assertion binds each account to its own figure and
    cannot be satisfied by regrouping one account twice.
    """
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    accounts = []
    for name, quantity, ref in (("CorpA", "1800", "zx2a"), ("CorpB", "600", "zx2b")):
        acc = await create_account(conn, name=name, venue="manual", account_type="cash")
        fill = replace(
            _position_fill(acc, inst, side=Side.BUY, quantity=quantity, price="0.05", ref=ref),
            executed_at=_PRE_EX_DATE,
        )
        await insert_fills(conn, [fill])
        accounts.append(acc)
    return accounts[0], accounts[1], inst


async def test_corporate_add_commits_and_regroups_every_holding_account(
    conn, two_accounts_holding_zxco, monkeypatch
):
    """Spec decision C7. A corporate action is GLOBAL -- the corporate_action
    table deliberately has no account_id, because a split affects every holder
    -- and positions come from materialised trade rows, so an add that regroups
    only some holders leaves the rest reporting pre-split quantities with
    nothing about the position saying so.

    The account order `SELECT DISTINCT account_id` returns is not defined, so
    this must hold whichever account comes first: each expected quantity is
    bound to its own account, and an unregrouped account has no trade rows at
    all, which fails the single-position unpack rather than passing quietly.
    """
    acc_a, acc_b, instrument_id = two_accounts_holding_zxco

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)

    rc = await cli.cmd_corporate_add(
        _corporate_args(
            type="reverse_split", symbol="ZXCO", ex_date="2026-03-02", ratio="1:6", commit=True
        )
    )
    assert rc == 0
    (position_a,) = await open_positions(conn, acc_a)
    (position_b,) = await open_positions(conn, acc_b)
    assert (position_a.quantity, position_b.quantity) == (Decimal(300), Decimal(100))


# --- the `corporate` subparser wiring -------------------------------------
# Every test above hands its handler a namespace built by `_corporate_args`,
# which hardcodes the dest names. A flag registered under a dest no handler
# reads -- or a subcommand missing its set_defaults(fn=...) -- would ship with
# all of them green. These two drive the real parser through cli.main() with a
# monkeypatched sys.argv, the same way
# test_marks_set_requires_exactly_one_of_symbol_or_natural_key above does.


@pytest.mark.parametrize(
    ("argv", "handler"),
    [
        (
            [
                "deadband", "corporate", "add", "--type", "reverse_split",
                "--symbol", "ZXCO", "--ex-date", "2026-03-02", "--ratio", "1:6",
            ],
            "cmd_corporate_add",
        ),
        (["deadband", "corporate", "list", "--symbol", "ZXCO"], "cmd_corporate_list"),
        (
            ["deadband", "corporate", "remove", "3f1b2c9e-0000-4000-8000-000000000001"],
            "cmd_corporate_remove",
        ),
    ],
)
def test_corporate_parser_routes_each_subcommand_to_its_handler(monkeypatch, argv, handler):
    """main() resolves `fn` at parse time, so a subcommand whose
    set_defaults(fn=...) was omitted or pointed at the wrong handler fails
    here. No database is involved: the handler itself is replaced."""
    called = []

    async def spy(args):
        called.append(args)
        return 0

    monkeypatch.setattr(cli, handler, spy)
    monkeypatch.setattr("sys.argv", argv)
    assert cli.main() == 0
    assert len(called) == 1


def test_corporate_add_parser_maps_every_flag_to_the_dest_its_handler_reads(monkeypatch):
    """One invocation carrying every flag `corporate add` registers, asserted
    against the exact attribute names cmd_corporate_add reads. A renamed flag,
    a missing `default=None`, or a --commit that did not store True would be
    invisible to every namespace-built test in this file.

    `--type spinoff` is used because it is the only type that exercises BOTH
    --resulting-symbol and --basis-allocation in one invocation. This test
    replaces the handler with a spy, so it pins the parser's flag-to-dest
    mapping and nothing about what the handler does with it."""
    captured = []

    async def spy(args):
        captured.append(args)
        return 0

    monkeypatch.setattr(cli, "cmd_corporate_add", spy)
    monkeypatch.setattr(
        "sys.argv",
        [
            "deadband", "corporate", "add", "--type", "spinoff", "--symbol", "ZXCO",
            "--ex-date", "2026-03-02", "--ratio", "1:1", "--resulting-symbol", "ZXCB",
            "--basis-allocation", "0.25", "--note", "a spinoff", "--commit",
        ],
    )
    assert cli.main() == 0
    (args,) = captured
    assert (args.type, args.symbol, args.ex_date, args.ratio) == (
        "spinoff", "ZXCO", "2026-03-02", "1:1",
    )
    assert (args.resulting_symbol, args.basis_allocation, args.note) == (
        "ZXCB", "0.25", "a spinoff",
    )
    assert args.commit is True
