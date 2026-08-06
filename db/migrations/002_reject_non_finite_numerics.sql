-- Postgres NUMERIC accepts the literal values 'NaN' and 'Infinity', neither of
-- which the original CHECK constraints from migration 001 caught:
--   contract_multiplier > 0     -- NaN and Infinity both compare > 0, so pass
--   price >= 0                  -- same problem
-- A NaN or Infinity contract_multiplier silently zeroes or corrupts option
-- P&L -- the exact failure instrument_multiplier_chk exists to prevent.
--
-- Postgres NUMERIC ordering is: ... -Infinity < ... < -1 < 0 < 1 < ... <
-- Infinity < NaN -- NaN sorts ABOVE Infinity, above every finite value. So
-- `x < 'Infinity'::numeric` is false for both Infinity and NaN and rejects
-- both in one comparison; it is combined with the original bound so
-- -Infinity is still caught by that bound rather than this one.
--
-- Mirrored in db/schema.sql for fresh databases; tests/db/test_schema_equivalence.py
-- asserts the two agree. Idempotent: safe to re-run.

ALTER TABLE instrument DROP CONSTRAINT IF EXISTS instrument_multiplier_chk;
ALTER TABLE instrument ADD  CONSTRAINT instrument_multiplier_chk
    CHECK (contract_multiplier > 0 AND contract_multiplier < 'Infinity'::numeric);

ALTER TABLE mark DROP CONSTRAINT IF EXISTS mark_price_chk;
ALTER TABLE mark ADD  CONSTRAINT mark_price_chk
    CHECK (price >= 0 AND price < 'Infinity'::numeric);
