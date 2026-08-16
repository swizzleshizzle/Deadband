-- Deadband ledger schema. All money and quantity columns are NUMERIC, never
-- FLOAT. All timestamps are TIMESTAMPTZ, stored UTC.

CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS account (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    venue           TEXT NOT NULL,
    external_ref    TEXT,
    account_type    TEXT NOT NULL CHECK (account_type IN ('cash','margin','funded','wallet')),
    default_intent  TEXT NOT NULL DEFAULT 'trade'
                    CHECK (default_intent IN ('trade','investment','mixed')),
    base_currency   TEXT NOT NULL DEFAULT 'USD',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    opened_at       TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ignore_on_import BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (venue, external_ref)
);

CREATE TABLE IF NOT EXISTS funded_account_rule (
    account_id          UUID PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
    max_drawdown        NUMERIC,
    drawdown_type       TEXT CHECK (drawdown_type IN ('static','trailing')),
    daily_loss_limit    NUMERIC,
    profit_target       NUMERIC,
    payout_split        NUMERIC,
    consistency_rule    TEXT,
    current_state       JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS instrument (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    natural_key         TEXT NOT NULL UNIQUE,
    asset_class         TEXT NOT NULL
                        CHECK (asset_class IN
                               ('crypto_spot','crypto_perp','equity','option','future')),
    symbol              TEXT NOT NULL,
    quote_currency      TEXT NOT NULL DEFAULT 'USD',
    underlying          TEXT,
    strike              NUMERIC,
    expiry              DATE,
    option_right        TEXT CHECK (option_right IN ('call','put')),
    root                TEXT,
    contract_multiplier NUMERIC NOT NULL DEFAULT 1
                        CONSTRAINT instrument_multiplier_chk
                        CHECK (contract_multiplier > 0 AND contract_multiplier < 'Infinity'::numeric),
    chain               TEXT,
    contract_address    TEXT,
    active_from         DATE,
    active_to           DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- fill is created before trade: trade.opening_fill_id references fill(id), so
-- fill must exist first or the schema will not apply to a clean database.
CREATE TABLE IF NOT EXISTS fill (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    instrument_id   UUID NOT NULL REFERENCES instrument(id),
    executed_at     TIMESTAMPTZ NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy','sell')),
    quantity        NUMERIC NOT NULL CHECK (quantity > 0),
    price           NUMERIC NOT NULL CHECK (price >= 0),
    fee             NUMERIC NOT NULL DEFAULT 0,
    fee_currency    TEXT NOT NULL DEFAULT 'USD',
    source          TEXT NOT NULL
                    CHECK (source IN ('manual','csv','api','opening_balance')),
    venue_order_id  TEXT,
    venue_fill_id   TEXT,
    content_hash    TEXT,
    is_estimated    BOOLEAN NOT NULL DEFAULT FALSE,
    funding_source  TEXT NOT NULL DEFAULT 'external'
                    CONSTRAINT fill_funding_source_chk CHECK (funding_source IN ('external','reinvestment')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Composite target for trade.opening_fill_id and trade_fill's cross-account
    -- guard: a fill can only be referenced together with its own account_id.
    CONSTRAINT fill_id_account_uniq UNIQUE (id, account_id)
);

-- Idempotent import rests on these two. A venue fill id is authoritative when
-- present; the content hash is the fallback for exports that carry no id.
CREATE UNIQUE INDEX IF NOT EXISTS fill_venue_id_uniq
    ON fill (account_id, venue_fill_id) WHERE venue_fill_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS fill_content_hash_uniq
    ON fill (account_id, content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS fill_account_instrument_time_idx
    ON fill (account_id, instrument_id, executed_at);

CREATE TABLE IF NOT EXISTS trade (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    primary_underlying  TEXT,
    direction           TEXT NOT NULL CHECK (direction IN ('long','short','spread')),
    status              TEXT NOT NULL CHECK (status IN ('open','closed')),
    intent              TEXT NOT NULL DEFAULT 'unassigned'
                        CHECK (intent IN ('trade','investment','unassigned')),
    grouping_mode       TEXT NOT NULL DEFAULT 'auto'
                        CHECK (grouping_mode IN ('auto','manual')),
    -- Stable identity for auto trades. Regroup upserts on this instead of
    -- deleting and rebuilding, so user-authored fields survive re-imports.
    -- ON DELETE SET NULL, never CASCADE: deleting a mis-imported fill must not take the
    -- trade's user-authored fields (notes, planned_risk, strategy_tag, intent) with it.
    -- A trade orphaned this way keeps its judgment; regroup decides what to do with it.
    opening_fill_id     UUID,
    -- FK attached below via named ALTER TABLE, not inline: derived_fill does
    -- not exist yet at this point in the file, and the migration attaches
    -- this same FK as a named ALTER too -- inline REFERENCES here would get
    -- a Postgres-generated constraint name instead, and the two files would
    -- disagree (see test_schema_equivalence.py).
    effective_instrument_id UUID,
    opening_derived_fill_id UUID,
    opened_at           TIMESTAMPTZ NOT NULL,
    closed_at           TIMESTAMPTZ,
    qty_opened          NUMERIC,
    qty_closed          NUMERIC,
    avg_entry           NUMERIC,
    avg_exit            NUMERIC,
    realized_pnl        NUMERIC,
    gross_realized_pnl  NUMERIC,
    fees_total          NUMERIC,
    fees_realized       NUMERIC,
    open_quantity       NUMERIC,
    open_cost_basis     NUMERIC,
    is_estimated        BOOLEAN NOT NULL DEFAULT FALSE,
    planned_risk        NUMERIC,
    r_multiple          NUMERIC,
    strategy_tag        TEXT,
    rolled_from_id      UUID REFERENCES trade(id) ON DELETE SET NULL,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Composite target for trade_fill's cross-account guard (see below).
    CONSTRAINT trade_id_account_uniq UNIQUE (id, account_id),
    -- Composite FK, not a plain fill(id) reference: pins opening_fill_id's account
    -- to match trade.account_id, so a trade in account B can never anchor on a
    -- fill from account A. The column-scoped "SET NULL (opening_fill_id)" (PG15+)
    -- is required, not stylistic: a bare "ON DELETE SET NULL" on a composite FK
    -- nulls every column in the constraint, including account_id — which then
    -- violates account_id's own NOT NULL and makes the fill un-deletable.
    CONSTRAINT trade_opening_fill_fk
        FOREIGN KEY (opening_fill_id, account_id)
        REFERENCES fill (id, account_id) ON DELETE SET NULL (opening_fill_id),
    -- At most one opening kind. Both NULL stays legal and keeps its existing
    -- meaning: an orphaned trade that kept its judgment (see regroup_account's
    -- protection step).
    CONSTRAINT trade_one_opening_chk
        CHECK (opening_fill_id IS NULL OR opening_derived_fill_id IS NULL)
);

CREATE INDEX IF NOT EXISTS trade_account_status_idx ON trade (account_id, status);
CREATE INDEX IF NOT EXISTS trade_opened_at_idx ON trade (opened_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS trade_opening_fill_uniq
    ON trade (account_id, opening_fill_id) WHERE opening_fill_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS trade_opening_derived_uniq
    ON trade (account_id, opening_derived_fill_id)
    WHERE opening_derived_fill_id IS NOT NULL;

-- Association is an allocation, not a foreign key on fill: one fill that crosses
-- zero belongs to two trades, split by quantity. account_id is a deliberate
-- denormalization: it is what lets the composite FKs below make a cross-account
-- allocation impossible to insert, rather than merely unlikely.
--
-- Surrogate id, not a composite PRIMARY KEY (trade_id, fill_id): a spinoff
-- allocation has no fill_id, and a composite primary key cannot contain a
-- nullable column. The old uniqueness is preserved instead by the two partial
-- indexes below.
CREATE TABLE IF NOT EXISTS trade_fill (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_id        UUID NOT NULL,
    fill_id         UUID,
    derived_fill_id UUID,
    account_id      UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    quantity        NUMERIC NOT NULL CHECK (quantity > 0),
    CONSTRAINT trade_fill_trade_fk FOREIGN KEY (trade_id, account_id)
        REFERENCES trade (id, account_id) ON DELETE CASCADE,
    CONSTRAINT trade_fill_fill_fk FOREIGN KEY (fill_id, account_id)
        REFERENCES fill (id, account_id) ON DELETE CASCADE,
    -- Exactly one of fill_id / derived_fill_id -- trade_fill_derived_fk (below,
    -- after derived_fill exists) attaches the second half of this pairing.
    CONSTRAINT trade_fill_one_source_chk CHECK (num_nonnulls(fill_id, derived_fill_id) = 1)
);

CREATE INDEX IF NOT EXISTS trade_fill_fill_idx ON trade_fill (fill_id);
CREATE UNIQUE INDEX IF NOT EXISTS trade_fill_real_uniq
    ON trade_fill (trade_id, fill_id) WHERE fill_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS trade_fill_derived_uniq
    ON trade_fill (trade_id, derived_fill_id) WHERE derived_fill_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS cash_movement (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    occurred_at     TIMESTAMPTZ NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN
                    ('deposit','withdrawal','fee','funding','interest',
                     'dividend','payout','rebate','tax','return_of_capital')),
    amount          NUMERIC NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    instrument_id   UUID REFERENCES instrument(id),
    venue_ref       TEXT,
    content_hash    TEXT,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS cash_content_hash_uniq
    ON cash_movement (account_id, content_hash) WHERE content_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS mark (
    instrument_id   UUID NOT NULL REFERENCES instrument(id) ON DELETE CASCADE,
    as_of           TIMESTAMPTZ NOT NULL,
    price           NUMERIC NOT NULL CONSTRAINT mark_price_chk
                        CHECK (price >= 0 AND price < 'Infinity'::numeric),
    source          TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (instrument_id, as_of)
);

CREATE TABLE IF NOT EXISTS corporate_action (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id           UUID NOT NULL REFERENCES instrument(id) ON DELETE CASCADE,
    action_type             TEXT NOT NULL CHECK (action_type IN
                            ('split','reverse_split','merger','spinoff','symbol_change')),
    ex_date                 DATE NOT NULL,
    ratio_numerator         NUMERIC NOT NULL CHECK (ratio_numerator > 0),
    ratio_denominator       NUMERIC NOT NULL CHECK (ratio_denominator > 0),
    resulting_instrument_id UUID REFERENCES instrument(id),
    cash_component          NUMERIC,
    basis_allocation        NUMERIC,
    note                    TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

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
-- Declared here, after corporate_action, not up near fill/trade/trade_fill: it
-- references corporate_action, which is declared later in this file, and table
-- order is not reshuffled for this. The three foreign keys that point at it from
-- trade and trade_fill are attached below as named ALTER TABLE statements instead
-- of inline, for the same reason -- see trade_effective_instrument_fk's comment.
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

-- Inline REFERENCES would get a Postgres-generated constraint name
-- (trade_effective_instrument_id_fkey); the migration attaches this same FK as
-- the named ALTER below, so both must use ALTER or schema.sql and the
-- migration would disagree on the constraint name (test_schema_equivalence.py).
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

ALTER TABLE trade_fill DROP CONSTRAINT IF EXISTS trade_fill_derived_fk;
ALTER TABLE trade_fill ADD  CONSTRAINT trade_fill_derived_fk
    FOREIGN KEY (derived_fill_id, account_id)
    REFERENCES derived_fill (id, account_id) ON DELETE CASCADE;

CREATE TABLE IF NOT EXISTS account_snapshot (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    as_of           TIMESTAMPTZ NOT NULL,
    cash_balance    NUMERIC NOT NULL,
    total_equity    NUMERIC NOT NULL,
    source          TEXT NOT NULL DEFAULT 'statement',
    note            TEXT,
    UNIQUE (account_id, as_of)
);

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
