"""asyncpg pool lifecycle. The only place that opens database connections."""

from __future__ import annotations

import os

import asyncpg

# Every connection this process opens runs in UTC, whatever the server is
# configured with.
#
# This is not cosmetic. A `timestamptz::date` cast resolves in the SESSION's
# timezone, so an expiry stored at UTC midnight casts to the PREVIOUS day
# under a western zone -- silently, with no error and a perfectly plausible
# result. Found 2026-09-07: the production database runs `America/New_York`
# while the database every test runs against runs `Etc/UTC`, so a query of
# that shape passes the entire suite and is wrong against the real ledger. A
# repair script written against the tests' assumptions matched 0 rows where it
# should have matched 33.
#
# Pinning it here rather than fixing the server: this function is documented
# above as the only place connections are opened, so one line covers the API's
# two pools, the CLI and the tests at once, and keeps working if the ledger is
# ever restored onto a differently-configured server.
_SESSION_SETTINGS = {"timezone": "UTC"}


async def create_pool(dsn: str | None = None, **kwargs) -> asyncpg.Pool:
    resolved = dsn or os.environ.get("PG_DSN")
    if not resolved:
        raise RuntimeError("PG_DSN is not set and no dsn was provided")
    # Caller settings merge ON TOP, so an explicit override is still possible
    # -- but no current caller sets a timezone, and one that did would be
    # making a deliberate choice rather than inheriting the server's.
    settings = {**_SESSION_SETTINGS, **kwargs.pop("server_settings", {})}
    return await asyncpg.create_pool(
        resolved, min_size=1, max_size=5, server_settings=settings, **kwargs
    )
