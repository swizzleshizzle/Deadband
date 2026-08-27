"""Fixtures for the read-only API tests (spec: 2026-08-19-read-only-api-design).

The app's connection source is overridden with the rollback-per-test `conn`
from tests/conftest.py, so handlers see seeded, uncommitted data and nothing
persists -- the same pattern tests/db/test_cli.py uses for the CLI."""

from __future__ import annotations

import httpx
import pytest_asyncio

from api.app import create_app

# Invented login for the test session only -- never a real tailnet identity.
# This repo is public and a pre-commit hook enforces a deny-list on real
# logins, so every login anywhere in this suite must use the .invalid TLD.
# The env var name and header name are spelled out literally (matching
# tests/api/test_identity.py) rather than imported from api.identity, since
# those are private module constants there and this fixture only needs the
# two strings the deployment contract already fixes.
TEST_TRUSTED_LOGIN = "apitest@example.invalid"
_TRUSTED_LOGINS_ENV = "DEADBAND_TRUSTED_LOGINS"
_IDENTITY_HEADER = "Tailscale-User-Login"


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
async def api_app(conn, monkeypatch):
    # require_trusted_identity reads this from the environment on every call
    # (see api/identity.py), so setting it here -- rather than patching the
    # dependency away -- exercises the real allowlist check on every write
    # request the `client` fixture below makes.
    monkeypatch.setenv(_TRUSTED_LOGINS_ENV, TEST_TRUSTED_LOGIN)
    app = create_app(enable_writes=True)
    app.state.pool = _FixturePool(conn)
    # Same rollback-per-test connection backs both pools, so a write
    # endpoint's changes are visible to a read endpoint in the same test and
    # vanish afterward -- there is no real second database to point at here.
    app.state.write_pool = _FixturePool(conn)
    return app


@pytest_asyncio.fixture
async def client(api_app):
    transport = httpx.ASGITransport(app=api_app)
    # Every request carries a trusted identity by default, matching the
    # proxy's real behavior of injecting this header on every proxied
    # request: existing write-route tests exercise fills/imports logic, not
    # the identity dependency (that's tests/api/test_identity.py and
    # tests/api/test_write_identity.py), so they should not each need to
    # pass the header explicitly.
    headers = {_IDENTITY_HEADER: TEST_TRUSTED_LOGIN}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as c:
        yield c


@pytest_asyncio.fixture
async def anonymous_client(api_app):
    """Like `client`, but carries NO identity header -- the shape of a request
    that reached this process without going through the authenticating proxy
    at all.

    A dedicated fixture rather than a one-off `httpx.AsyncClient` inside a
    single test: `client`'s default header made every existing write test
    identity-blind (a swallowed exception or a duplicate route registration
    would have passed just as well as real enforcement), and the fix is an
    opt-out fixture, not a one-off workaround -- its absence is exactly what
    left that gap open. Shares `api_app`, so it sees the same
    DEADBAND_TRUSTED_LOGINS value and the same rollback-per-test connection
    as `client`."""
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
