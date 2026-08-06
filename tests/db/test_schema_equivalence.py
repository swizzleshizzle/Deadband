"""A fresh database and a migrated one must end up structurally identical.

migrate.apply() re-runs an idempotent schema.sql, so `CREATE TABLE IF NOT
EXISTS` silently skips a table that already exists -- a new column added only
to schema.sql reaches fresh installs and never reaches existing ones. The
divergence is invisible: both databases work, they just disagree.

Builds both shapes in separate Postgres namespaces and compares them.
"""

import pathlib

import pytest

from tests.conftest import requires_db

DB_DIR = pathlib.Path(__file__).resolve().parents[2] / "db"
BASELINE = pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "schema_baseline_a1.sql"


async def _describe(conn, namespace: str) -> dict:
    """Columns and check constraints of every table in a namespace."""
    cols = await conn.fetch(
        """SELECT table_name, column_name, data_type, is_nullable, column_default
             FROM information_schema.columns
            WHERE table_schema = $1
         ORDER BY table_name, column_name""",
        namespace,
    )
    checks = await conn.fetch(
        """SELECT cc.check_clause, tc.table_name
             FROM information_schema.check_constraints cc
             JOIN information_schema.table_constraints tc
               ON tc.constraint_name = cc.constraint_name
              AND tc.constraint_schema = cc.constraint_schema
            WHERE cc.constraint_schema = $1
         ORDER BY tc.table_name, cc.check_clause""",
        namespace,
    )
    return {
        "columns": [tuple(r) for r in cols],
        "checks": sorted((r["table_name"], r["check_clause"]) for r in checks),
    }


async def _build(conn, namespace: str, sql_files: list[pathlib.Path]) -> None:
    await conn.execute(f'DROP SCHEMA IF EXISTS "{namespace}" CASCADE')
    await conn.execute(f'CREATE SCHEMA "{namespace}"')
    await conn.execute(f'SET search_path TO "{namespace}"')
    for path in sql_files:
        await conn.execute(path.read_text())
    await conn.execute("SET search_path TO public")


@requires_db
async def test_fresh_schema_matches_baseline_plus_migrations(conn):
    migrations = sorted((DB_DIR / "migrations").glob("*.sql"))

    await _build(conn, "eq_fresh", [DB_DIR / "schema.sql"])
    await _build(conn, "eq_migrated", [BASELINE, *migrations])

    fresh = await _describe(conn, "eq_fresh")
    migrated = await _describe(conn, "eq_migrated")

    assert fresh["columns"] == migrated["columns"], (
        "schema.sql and baseline+migrations disagree on columns -- a change was "
        "written to one and not the other"
    )
    assert fresh["checks"] == migrated["checks"], (
        "schema.sql and baseline+migrations disagree on CHECK constraints"
    )
