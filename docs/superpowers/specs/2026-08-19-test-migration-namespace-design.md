# Per-session disposable namespace for the test database

**Status:** design, approved 2026-08-19.
**Issue:** #15 — the `pool` fixture records migrations permanently in the shared
test database, silently disarming migration tests (it hid a Critical on PR #14).
**Sequencing:** lands **before** branch B, whose migration `004` would otherwise
be recorded on the first run and never exercised again.

## The fix (issue #15's option 1, disposable-namespace variant)

All inside `tests/conftest.py`'s session-scoped `pool` fixture; no production
code changes — `db/pool.create_pool` already passes `**kwargs` through to
asyncpg:

1. Sweep `test_session_*` schemas orphaned by crashed runs, then
   `CREATE SCHEMA test_session_<shortid>`.
2. Create the pool with `server_settings={"search_path": "<that schema>"}`.
   `db/schema.sql` and `db/migrate.apply` are schema-unqualified throughout
   (verified, including the `set_updated_at` trigger function), so everything
   they create lands in the namespace.
3. Run `apply()` as before — now against an empty namespace, so **every
   migration executes from zero on every run**. `public` is never written again;
   its already-polluted `schema_migrations` stops mattering.
4. `DROP SCHEMA ... CASCADE` on session end.

**Why not "make `apply()` transactional":** a new migration's schema changes must
stay visible to every pooled connection for the whole session; no
rollback-at-session-end transaction spans connections. The namespace gives the
same no-residue guarantee without the contradiction.

## Proof in the suite

One new test asserts the session namespace's `schema_migrations` rows match the
files in `db/migrations/` exactly — true only if all of them executed *this*
session. That is the anti-disarm guarantee a future migration-004 test inherits.

## Unchanged

The rollback-per-test `conn` fixture; `tests/db/test_migrations.py` and
`test_schema_equivalence.py`, which build their own pre-migration namespaces
deliberately and keep doing so.

## Costs accepted

One full `schema.sql` + all-migrations apply per session (sub-second today); a
crashed run leaves one orphan schema until the next run's sweep.

