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
    """Columns, check constraints, trigger definitions (with their backing
    function bodies), and named foreign key / primary key / unique constraints
    of every table in a namespace.

    information_schema.triggers returns one row per event (INSERT/UPDATE/...)
    for a single trigger, which makes it awkward to compare directly. pg_trigger
    joined to pg_get_triggerdef()/pg_get_functiondef() instead gives one row per
    real trigger, carrying its full definition and the body of the function it
    calls -- so a trigger added to schema.sql but not to a migration (or vice
    versa) shows up here even though it has no informal check the way column and
    CHECK DDL syntax does.

    information_schema.check_constraints only surfaces contype = 'c' -- CHECK
    constraints. It says nothing about foreign keys, primary keys, or unique
    constraints, so a divergence there (a differently-named FK, a PK left as
    the old composite shape on one side, a missing UNIQUE) was previously
    invisible to this test. pg_constraint joined the same way as the trigger
    query, with pg_get_constraintdef() for the full definition (the same
    approach used above for triggers via pg_get_triggerdef()), closes that
    gap: contype IN ('f', 'p', 'u') covers foreign keys, primary keys, and
    unique constraints, keyed by table name and constraint name.
    """
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
    triggers = await conn.fetch(
        """SELECT c.relname AS table_name,
                  t.tgname AS trigger_name,
                  pg_get_triggerdef(t.oid) AS trigger_def,
                  pg_get_functiondef(t.tgfoid) AS function_def
             FROM pg_trigger t
             JOIN pg_class c ON c.oid = t.tgrelid
             JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1
              AND NOT t.tgisinternal
         ORDER BY c.relname, t.tgname""",
        namespace,
    )
    constraints = await conn.fetch(
        """SELECT c.relname AS table_name,
                  con.conname AS constraint_name,
                  con.contype AS constraint_type,
                  pg_get_constraintdef(con.oid) AS definition
             FROM pg_constraint con
             JOIN pg_class c ON c.oid = con.conrelid
             JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = $1
              AND con.contype IN ('f', 'p', 'u')
         ORDER BY c.relname, con.conname""",
        namespace,
    )
    # pg_get_triggerdef()/pg_get_functiondef()/pg_get_constraintdef() unconditionally
    # schema-qualify the trigger's/constraint's own relation and any relation it
    # references -- e.g. "... ON eq_fresh.fill ..." / "CREATE OR REPLACE FUNCTION
    # eq_fresh.f()" / "FOREIGN KEY (...) REFERENCES eq_fresh.derived_fill(...)" --
    # regardless of search_path (verified directly against Postgres 16; this is not
    # a search_path bug to fix, it is how ruleutils always deparses these). Left
    # alone, the namespace name itself would make eq_fresh and eq_migrated compare
    # unequal forever, even for byte-identical definitions. Strip the namespace's
    # own name back out before comparing.
    prefix = f"{namespace}."
    return {
        "columns": [tuple(r) for r in cols],
        "checks": sorted((r["table_name"], r["check_clause"]) for r in checks),
        "triggers": [
            (
                r["table_name"],
                r["trigger_name"],
                r["trigger_def"].replace(prefix, ""),
                r["function_def"].replace(prefix, ""),
            )
            for r in triggers
        ],
        "constraints": sorted(
            (
                r["table_name"],
                r["constraint_name"],
                r["constraint_type"],
                r["definition"].replace(prefix, ""),
            )
            for r in constraints
        ),
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

    # Guards against a describe/build bug (wrong namespace, a swallowed schema
    # creation failure, ...) that makes both sides empty: [] == [] would pass the
    # assertions below vacuously, proving nothing. This project has shipped ten
    # assertions that could not fail; this is the guard against adding an eleventh.
    assert fresh["columns"] or fresh["checks"] or fresh["triggers"] or fresh["constraints"], (
        "eq_fresh produced no columns, checks, triggers, or constraints -- _describe "
        "or the schema build likely failed silently, which would make the "
        "comparisons below vacuously true (empty compared to empty)"
    )

    assert fresh["columns"] == migrated["columns"], (
        "schema.sql and baseline+migrations disagree on columns -- a change was "
        "written to one and not the other"
    )
    assert fresh["checks"] == migrated["checks"], (
        "schema.sql and baseline+migrations disagree on CHECK constraints"
    )
    assert fresh["triggers"] == migrated["triggers"], (
        "schema.sql and baseline+migrations disagree on triggers or their backing "
        "function bodies -- a change was written to one and not the other"
    )
    assert fresh["constraints"] == migrated["constraints"], (
        "schema.sql and baseline+migrations disagree on foreign key, primary key, "
        "or unique constraints -- a change was written to one and not the other"
    )
