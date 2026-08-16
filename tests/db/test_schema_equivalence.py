"""A fresh database and a migrated one must end up structurally identical.

migrate.apply() re-runs an idempotent schema.sql, so `CREATE TABLE IF NOT
EXISTS` silently skips a table that already exists -- a new column added only
to schema.sql reaches fresh installs and never reaches existing ones. The
divergence is invisible: both databases work, they just disagree.

Builds both shapes in separate Postgres namespaces and compares them.
"""

import pathlib

import asyncpg

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


@requires_db
async def test_schema_sql_then_migrations_upgrades_a_pre_existing_database(conn):
    """THE UPGRADE PATH. Neither test above can see this class of defect.

    migrate.apply() executes schema.sql FIRST and the migrations afterwards
    (db/migrate.py). On an EXISTING database every `CREATE TABLE IF NOT EXISTS`
    in schema.sql is skipped whole, so a column declared only inline inside one
    of them does not exist when a later statement in the same file names it --
    an index over it, or an ALTER TABLE ... ADD CONSTRAINT referencing it. That
    raises, asyncpg's implicit transaction rolls the entire file back, and
    apply() dies before the migration that would have added the column ever
    runs. Every pre-existing database is then permanently un-upgradable.

    test_apply_is_idempotent (tests/db/test_migrations.py) cannot see it: it
    runs against a database already at 003. test_fresh_schema_matches_baseline_
    plus_migrations above cannot see it either: its migrated side is
    BASELINE + migrations in a CLEAN namespace and never runs schema.sql against
    a table that already exists.

    So build the third shape -- the only one that reproduces a real upgrade --
    and compare it to the fresh one. Verified to fail (UndefinedColumnError,
    `column "opening_derived_fill_id" does not exist`) with the ADD COLUMN IF NOT
    EXISTS statements removed from db/schema.sql, which is the state this branch
    shipped in before this test existed.

    The input that would make this fail: any future migration that adds a column
    to an existing table, mirrors it into schema.sql only inside the table's
    `CREATE TABLE IF NOT EXISTS`, and then references it anywhere else in
    schema.sql.
    """
    migrations = sorted((DB_DIR / "migrations").glob("*.sql"))
    # The pre-003 state, named rather than sliced: a new 004 must extend the
    # "already applied" prefix here, not silently shift what [:2] means.
    already_applied = {"001_a2_ledger_completion.sql", "002_reject_non_finite_numerics.sql"}
    pre_existing = [BASELINE, *(m for m in migrations if m.name in already_applied)]
    assert len(pre_existing) == len(already_applied) + 1, "a named pre-003 migration is missing"

    try:
        await _build(conn, "eq_fresh", [DB_DIR / "schema.sql"])
        # apply()'s real order, against a database that already has its tables.
        await _build(conn, "eq_upgraded", [*pre_existing, DB_DIR / "schema.sql", *migrations])

        fresh = await _describe(conn, "eq_fresh")
        upgraded = await _describe(conn, "eq_upgraded")

        # Same non-vacuity guard as the test above: [] == [] proves nothing.
        assert upgraded["columns"] and upgraded["constraints"], (
            "eq_upgraded produced no columns or constraints -- the build failed "
            "silently, which would make the comparisons below vacuously true"
        )

        assert fresh["columns"] == upgraded["columns"], (
            "a fresh database and an upgraded one disagree on columns -- schema.sql "
            "adds something to a fresh install that no migration adds to an existing one"
        )
        assert fresh["checks"] == upgraded["checks"], (
            "a fresh database and an upgraded one disagree on CHECK constraints"
        )
        assert fresh["triggers"] == upgraded["triggers"], (
            "a fresh database and an upgraded one disagree on triggers"
        )
        assert fresh["constraints"] == upgraded["constraints"], (
            "a fresh database and an upgraded one disagree on foreign key, primary "
            "key, or unique constraints"
        )
    finally:
        # The `conn` fixture rolls its transaction back, which already undoes
        # this DDL -- but drop explicitly anyway, including on failure, so a
        # disposable namespace can never survive into the shared database.
        #
        # Suppressed, not propagated: when the build itself raises (which is
        # exactly what the missing-column defect does), the transaction is
        # already aborted and every statement here raises
        # InFailedSQLTransactionError -- which would replace the real
        # UndefinedColumnError in the failure report with a useless one. The
        # rollback still drops the namespaces in that case, so suppressing
        # here leaks nothing.
        try:
            await conn.execute("SET search_path TO public")
            for ns in ("eq_fresh", "eq_upgraded"):
                await conn.execute(f'DROP SCHEMA IF EXISTS "{ns}" CASCADE')
        except asyncpg.PostgresError:
            pass
