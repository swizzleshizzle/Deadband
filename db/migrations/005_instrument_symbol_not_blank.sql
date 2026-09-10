-- Issue #27's second guard: an instrument may not carry a blank symbol.
--
-- The importer stopped MINTING blank symbols in PR #35 (derive_symbol keys a
-- blank-symbol row on the ISIN in its Description). This is the schema-level
-- half, so nothing else can ever mint one either -- a hand-written INSERT, a
-- future importer, a CLI path that skips validation.
--
-- WHY `NOT VALID`, and why that is not a weaker guard:
--
-- The production ledger still holds one legacy instrument with symbol = ''
-- (known-gap #77: 17 fills spanning four securities, merged by the old
-- importer). A plain ADD CONSTRAINT validates every existing row, so it would
-- fail against that database -- and `cli.py migrate` runs on EVERY deploy, so
-- it would not fail once, it would break deploys permanently.
--
-- NOT VALID skips the scan of existing rows and enforces on every INSERT and
-- UPDATE from this point on. That is the entire behaviour issue #27 asks for.
-- The legacy row stays as a known exception until it is repaired, and a later
-- migration then runs ALTER TABLE ... VALIDATE CONSTRAINT to close it fully.
--
-- Verified against Postgres 16, not assumed (2026-09-09):
--   * ADD ... NOT VALID succeeds with the blank-symbol row present
--   * a new blank symbol is refused; so is a whitespace-only one
--   * the legacy row stays readable
--   * an UPDATE of the legacy row IS re-checked, even on an unrelated column.
--     That is safe here because upsert_instrument (db/instruments.py) is the
--     only write path to this table -- there is no bare UPDATE anywhere in the
--     codebase -- and its ON CONFLICT DO UPDATE always repaints `symbol` from
--     EXCLUDED. So an update either sets a real symbol (allowed, and it
--     repairs the row) or sets a blank one (refused, which is the point).
--   * VALIDATE CONSTRAINT then succeeds once the data is repaired.
--
-- Mirrored in db/schema.sql for fresh databases;
-- tests/db/test_schema_equivalence.py asserts the two agree. That test
-- compares pg_get_constraintdef(), whose output includes the literal string
-- "NOT VALID" -- so both sides must add it NOT VALID or the suite goes red on
-- a difference that is real but harmless.
--
-- Guarded by IF NOT EXISTS rather than this repo's usual
-- `DROP CONSTRAINT IF EXISTS` + `ADD` pair, deliberately: schema.sql re-runs
-- on every deploy, so a drop-and-re-add there would silently undo a later
-- VALIDATE and re-diverge from a fresh install on the next deploy.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'instrument_symbol_not_blank'
          AND conrelid = 'instrument'::regclass
    ) THEN
        ALTER TABLE instrument
            ADD CONSTRAINT instrument_symbol_not_blank
            CHECK (btrim(symbol) <> '') NOT VALID;
    END IF;
END $$;
