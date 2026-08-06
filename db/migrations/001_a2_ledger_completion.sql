-- A-2 ledger completion. Mirrored in db/schema.sql for fresh databases;
-- tests/db/test_schema_equivalence.py asserts the two agree.

ALTER TABLE trade   ADD COLUMN IF NOT EXISTS fees_realized     NUMERIC;
ALTER TABLE trade   ADD COLUMN IF NOT EXISTS open_quantity     NUMERIC;
ALTER TABLE trade   ADD COLUMN IF NOT EXISTS open_cost_basis   NUMERIC;
ALTER TABLE trade   ADD COLUMN IF NOT EXISTS is_estimated      BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fill    ADD COLUMN IF NOT EXISTS funding_source    TEXT NOT NULL DEFAULT 'external';
ALTER TABLE account ADD COLUMN IF NOT EXISTS ignore_on_import  BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE fill DROP CONSTRAINT IF EXISTS fill_funding_source_chk;
ALTER TABLE fill ADD  CONSTRAINT fill_funding_source_chk
    CHECK (funding_source IN ('external','reinvestment'));

-- A zero or negative multiplier silently zeroes or inverts option P&L.
ALTER TABLE instrument DROP CONSTRAINT IF EXISTS instrument_multiplier_chk;
ALTER TABLE instrument ADD  CONSTRAINT instrument_multiplier_chk
    CHECK (contract_multiplier > 0);

ALTER TABLE mark DROP CONSTRAINT IF EXISTS mark_price_chk;
ALTER TABLE mark ADD  CONSTRAINT mark_price_chk CHECK (price >= 0);

-- 'tax' is an outflow and must be added to importers.base.OUTFLOW_KINDS too.
-- 'return_of_capital' is recorded but not yet applied to cost basis (spec A2-14).
ALTER TABLE cash_movement DROP CONSTRAINT IF EXISTS cash_movement_kind_check;
ALTER TABLE cash_movement ADD  CONSTRAINT cash_movement_kind_check
    CHECK (kind IN ('deposit','withdrawal','fee','funding','interest',
                    'dividend','payout','rebate','tax','return_of_capital'));

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS fill_set_updated_at ON fill;
CREATE TRIGGER fill_set_updated_at
    BEFORE UPDATE ON fill
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
