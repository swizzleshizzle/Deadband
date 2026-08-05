"""Idempotent migration runner. Applies schema.sql, then db/migrations/*.sql in
name order, recording each in schema_migrations so reruns are no-ops.

Note: schema.sql relies on gen_random_uuid(), which is built into Postgres
core since version 13 — no pgcrypto extension is required or created here.
"""

from __future__ import annotations

import pathlib

import asyncpg

DB_DIR = pathlib.Path(__file__).parent
SCHEMA = DB_DIR / "schema.sql"
MIGRATIONS = DB_DIR / "migrations"


async def apply(conn: asyncpg.Connection) -> list[str]:
    """Apply pending migrations. Returns the names applied this run."""
    await conn.execute(SCHEMA.read_text())

    done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
    applied: list[str] = []

    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        async with conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
        applied.append(path.name)

    return applied
