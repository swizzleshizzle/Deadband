"""db.create_pool pins the session timezone.

Deliberately mocks asyncpg instead of connecting: the database this suite runs
against is ALREADY `Etc/UTC`, so a live `SHOW TimeZone` assertion would pass
whether or not create_pool sets anything -- vacuous, and vacuous in exactly
the direction that hides the bug. Asserting on what create_pool ASKS FOR is
the only check here that goes red when the setting is removed.

Why it matters: a `timestamptz::date` cast resolves in the session's timezone.
Production runs `America/New_York`, so a timestamp stored at UTC midnight
casts to the previous day there and to the correct day here -- silently, with
a plausible result either way. Found when a repair script matched 0 rows
against production where it should have matched 33.
"""

from __future__ import annotations

import asyncpg
import pytest

from db.pool import create_pool


@pytest.fixture
def captured(monkeypatch):
    seen: dict = {}

    async def _fake_create_pool(dsn, **kwargs):
        seen["dsn"] = dsn
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(asyncpg, "create_pool", _fake_create_pool)
    return seen


async def test_create_pool_pins_the_session_timezone_to_utc(captured):
    await create_pool("postgresql://user@host/db")
    assert captured["server_settings"]["timezone"] == "UTC"


async def test_caller_server_settings_survive_the_pinning(captured):
    """api/deps.py passes `default_transaction_read_only` for the read pool.
    Pinning the timezone must not drop it -- that setting is the Postgres-level
    guarantee spec D3 rests on, and losing it would be far worse than the bug
    being fixed here."""
    await create_pool("postgresql://user@host/db",
                      server_settings={"default_transaction_read_only": "on"})
    settings = captured["server_settings"]
    assert settings["default_transaction_read_only"] == "on"
    assert settings["timezone"] == "UTC"


async def test_an_explicit_caller_timezone_still_wins(captured):
    """Merged so a caller CAN override deliberately. No caller does today; the
    point is that inheriting the server's zone by accident is what stops."""
    await create_pool("postgresql://user@host/db",
                      server_settings={"timezone": "America/New_York"})
    assert captured["server_settings"]["timezone"] == "America/New_York"
