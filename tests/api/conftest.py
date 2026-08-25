"""Fixtures for the read-only API tests (spec: 2026-08-19-read-only-api-design).

The app's connection source is overridden with the rollback-per-test `conn`
from tests/conftest.py, so handlers see seeded, uncommitted data and nothing
persists -- the same pattern tests/db/test_cli.py uses for the CLI."""

from __future__ import annotations

import httpx
import pytest_asyncio

from api.app import create_app


class _FixturePool:
    """Stands in for the app's asyncpg pool: acquire() hands out the test's
    own transaction-wrapped connection; close() is a no-op."""

    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Acquire(self._conn)

    async def close(self) -> None:
        return None


class _Acquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


@pytest_asyncio.fixture
async def api_app(conn):
    app = create_app()
    app.state.pool = _FixturePool(conn)
    # Same rollback-per-test connection backs both pools, so a write
    # endpoint's changes are visible to a read endpoint in the same test and
    # vanish afterward -- there is no real second database to point at here.
    app.state.write_pool = _FixturePool(conn)
    return app


@pytest_asyncio.fixture
async def client(api_app):
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def assert_no_json_floats(payload, path="$"):
    """Spec D4 structurally: NUMERIC never becomes a JSON float. Counts are
    ints and money is strings; a float anywhere is a serialization bug."""
    if isinstance(payload, float):
        raise AssertionError(f"float at {path}: {payload!r}")
    if isinstance(payload, dict):
        for k, v in payload.items():
            assert_no_json_floats(v, f"{path}.{k}")
    elif isinstance(payload, list):
        for i, v in enumerate(payload):
            assert_no_json_floats(v, f"{path}[{i}]")
