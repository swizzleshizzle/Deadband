"""CLI tests that need a real database — specifically to exercise cli.cmd_import
itself, not a hand-rolled re-implementation of its transaction pattern."""

from __future__ import annotations

import argparse

import pytest

import cli
from db.accounts import create_account
from tests.conftest import requires_db

pytestmark = requires_db


class _FakeAcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc_info):
        return False


class _FakePool:
    """Stands in for the real asyncpg.Pool returned by db.pool.create_pool, so
    cmd_import runs against the test fixture's own connection (already inside a
    transaction that conftest rolls back at teardown) instead of opening a real
    pool from PG_DSN."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquireCtx(self._conn)

    async def close(self):
        pass


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
