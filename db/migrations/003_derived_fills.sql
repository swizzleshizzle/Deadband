-- Spinoff children are the only fills adjust_fills invents rather than rescales:
-- ledger/corporate.py mints a uuid5 for them, and no `fill` row exists to match.
-- trade.opening_fill_id and trade_fill.fill_id are non-deferrable COMPOSITE foreign
-- keys into fill (id, account_id), so persisting one raises ForeignKeyViolationError.
--
-- Fills stay ground truth (design D1): derived rows get their own table rather than a
-- flag on `fill`. regroup_account regenerates this table on every run and never reads
-- it back -- its job is to give the foreign keys something real to point at, and to
-- let a human answer "where did this position come from?".
--
-- Mirrored in db/schema.sql for fresh databases; tests/db/test_schema_equivalence.py
-- asserts the two agree. Idempotent: safe to re-run.

CREATE TABLE IF NOT EXISTS derived_fill (
    id                   UUID PRIMARY KEY,          -- supplied, never defaulted: it is
                                                    -- _spinoff_fill_id's uuid5, which is
                                                    -- what makes ON CONFLICT (id) stable
    account_id           UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    instrument_id        UUID NOT NULL REFERENCES instrument(id),
    executed_at          TIMESTAMPTZ NOT NULL,
    side                 TEXT NOT NULL CHECK (side IN ('buy','sell')),
    quantity             NUMERIC NOT NULL
                         CHECK (quantity > 0 AND quantity < 'Infinity'::numeric),
    price                NUMERIC NOT NULL
                         CHECK (price >= 0 AND price < 'Infinity'::numeric),
    fee                  NUMERIC NOT NULL DEFAULT 0
                         CHECK (fee >= 0 AND fee < 'Infinity'::numeric),
    is_estimated         BOOLEAN NOT NULL DEFAULT TRUE,
    derived_from_fill_id UUID NOT NULL REFERENCES fill(id) ON DELETE CASCADE,
    corporate_action_id  UUID NOT NULL REFERENCES corporate_action(id) ON DELETE CASCADE,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Composite target, mirroring fill_id_account_uniq.
    CONSTRAINT derived_fill_id_account_uniq UNIQUE (id, account_id)
);

CREATE INDEX IF NOT EXISTS derived_fill_account_idx ON derived_fill (account_id);

ALTER TABLE trade ADD COLUMN IF NOT EXISTS effective_instrument_id UUID;
ALTER TABLE trade ADD COLUMN IF NOT EXISTS opening_derived_fill_id UUID;

ALTER TABLE trade DROP CONSTRAINT IF EXISTS trade_effective_instrument_fk;
ALTER TABLE trade ADD  CONSTRAINT trade_effective_instrument_fk
    FOREIGN KEY (effective_instrument_id) REFERENCES instrument(id);

-- Column-scoped SET NULL (PG15+), for the same reason trade_opening_fill_fk needs it:
-- a bare ON DELETE SET NULL on a composite FK nulls account_id too, which then violates
-- its own NOT NULL and makes the referenced row un-deletable.
ALTER TABLE trade DROP CONSTRAINT IF EXISTS trade_opening_derived_fill_fk;
ALTER TABLE trade ADD  CONSTRAINT trade_opening_derived_fill_fk
    FOREIGN KEY (opening_derived_fill_id, account_id)
    REFERENCES derived_fill (id, account_id) ON DELETE SET NULL (opening_derived_fill_id);

-- At most one opening kind. Both NULL stays legal and keeps its existing meaning:
-- an orphaned trade that kept its judgment (see regroup_account's protection step).
ALTER TABLE trade DROP CONSTRAINT IF EXISTS trade_one_opening_chk;
ALTER TABLE trade ADD  CONSTRAINT trade_one_opening_chk
    CHECK (opening_fill_id IS NULL OR opening_derived_fill_id IS NULL);

CREATE UNIQUE INDEX IF NOT EXISTS trade_opening_derived_uniq
    ON trade (account_id, opening_derived_fill_id)
    WHERE opening_derived_fill_id IS NOT NULL;

-- trade_fill: a composite PK cannot contain a nullable column, and a spinoff
-- allocation has no fill_id. Surrogate key, with the old uniqueness preserved by two
-- partial indexes.
ALTER TABLE trade_fill ADD COLUMN IF NOT EXISTS id UUID DEFAULT gen_random_uuid();
UPDATE trade_fill SET id = gen_random_uuid() WHERE id IS NULL;
ALTER TABLE trade_fill ALTER COLUMN id SET NOT NULL;
ALTER TABLE trade_fill DROP CONSTRAINT IF EXISTS trade_fill_pkey;
ALTER TABLE trade_fill ADD  CONSTRAINT trade_fill_pkey PRIMARY KEY (id);
ALTER TABLE trade_fill ALTER COLUMN fill_id DROP NOT NULL;
ALTER TABLE trade_fill ADD COLUMN IF NOT EXISTS derived_fill_id UUID;

ALTER TABLE trade_fill DROP CONSTRAINT IF EXISTS trade_fill_derived_fk;
ALTER TABLE trade_fill ADD  CONSTRAINT trade_fill_derived_fk
    FOREIGN KEY (derived_fill_id, account_id)
    REFERENCES derived_fill (id, account_id) ON DELETE CASCADE;

ALTER TABLE trade_fill DROP CONSTRAINT IF EXISTS trade_fill_one_source_chk;
ALTER TABLE trade_fill ADD  CONSTRAINT trade_fill_one_source_chk
    CHECK (num_nonnulls(fill_id, derived_fill_id) = 1);

CREATE UNIQUE INDEX IF NOT EXISTS trade_fill_real_uniq
    ON trade_fill (trade_id, fill_id) WHERE fill_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS trade_fill_derived_uniq
    ON trade_fill (trade_id, derived_fill_id) WHERE derived_fill_id IS NOT NULL;
