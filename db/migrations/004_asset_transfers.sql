-- Branch B: outbound ACAT transfers (spec 2026-08-19-acat-transfer-out-design).
-- The share leg gets its own table: it is neither a fill (no transaction price
-- exists; booking one would fabricate P&L) nor a corporate action (derived_fill
-- is bound to one by NOT NULL construction). Direction admits only 'out' (D2):
-- an inbound transfer arrives with basis this ledger has no source for.

CREATE TABLE IF NOT EXISTS asset_transfer (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    instrument_id   UUID NOT NULL REFERENCES instrument(id),
    occurred_at     TIMESTAMPTZ NOT NULL,
    direction       TEXT NOT NULL CONSTRAINT asset_transfer_direction_chk
                    CHECK (direction IN ('out')),
    quantity        NUMERIC NOT NULL CONSTRAINT asset_transfer_quantity_chk
                    CHECK (quantity > 0 AND quantity < 'Infinity'::numeric),
    market_value    NUMERIC,  -- broker's stamp; informational, never P&L
    venue_ref       TEXT,
    content_hash    TEXT,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS asset_transfer_content_hash_uniq
    ON asset_transfer (account_id, content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS asset_transfer_account_time_idx
    ON asset_transfer (account_id, instrument_id, occurred_at);

-- The kind CHECK was inline and auto-named; both this migration and schema.sql
-- convert it to the SAME named constraint (the trade_effective_instrument_fk
-- pattern) or test_schema_equivalence.py fails on the name disagreement.
ALTER TABLE cash_movement DROP CONSTRAINT IF EXISTS cash_movement_kind_check;
ALTER TABLE cash_movement DROP CONSTRAINT IF EXISTS cash_movement_kind_chk;
ALTER TABLE cash_movement ADD CONSTRAINT cash_movement_kind_chk CHECK (kind IN
    ('deposit','withdrawal','fee','funding','interest',
     'dividend','payout','rebate','tax','return_of_capital','transfer_out'));

ALTER TABLE trade ADD COLUMN IF NOT EXISTS qty_transferred NUMERIC;
