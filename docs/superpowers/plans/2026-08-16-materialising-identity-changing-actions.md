# Materialising Identity-Changing Corporate Actions — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist what `adjust_fills` already computes for `merger`, `spinoff` and `symbol_change`, so those three types stop being refused and report correctly.

**Architecture:** Two independent halves. **Half A** adds `trade.effective_instrument_id`, written by `regroup_account` from the adjusted fill, and has `open_positions` prefer it — that alone fixes `merger` and `symbol_change`. **Half B** adds a `derived_fill` table for spinoff children, so the composite foreign keys on `trade` and `trade_fill` have something real to point at while `fill` stays pure. Then the CLI refusal comes off.

**Tech Stack:** Python 3.11+, `uv`, asyncpg, pytest, PostgreSQL 15+ (the column-scoped `ON DELETE SET NULL (col)` form requires it).

**Spec:** `docs/superpowers/specs/2026-08-16-materialising-identity-changing-actions-design.md`. Read §4 (schema), §5 (the regroup lifecycle) and especially **§5.3 (the reaping trap)** and **§5.1a (recovering provenance)** before starting — they carry reasoning this plan compresses.

## Global Constraints

- **Purity.** `ledger/` and `importers/` import no I/O, no clock, no randomness, and not the first-party `db`/`venues` packages. `tests/test_purity.py` enforces it. **This plan changes nothing in `ledger/`.** `db/trades.py` *imports* `_spinoff_fill_id` from it, which is the allowed direction. If you find yourself editing a file under `ledger/`, stop and report.
- **`fill` stays pure.** No derived row is ever inserted into `fill`, marked or otherwise. That is decision D1 and the reason `derived_fill` exists at all.
- **`Decimal`, never `float`.**
- **The clock lives in `cli.py`.** `db/` never calls `datetime.now()`.
- **Refusals write nothing and exit non-zero (exit 2).** Validate before opening a write transaction.
- **Migrations are idempotent and mirrored in `schema.sql`.** `migrate.apply()` re-runs `schema.sql` before every migration, and `tests/db/test_schema_equivalence.py` fails if a fresh database and a migrated one disagree. Follow migration `002`'s pattern: `DROP CONSTRAINT IF EXISTS` before every `ADD CONSTRAINT`, `ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`.
- **The test database is SHARED and PERSISTENT**, and `instrument` rows are global. Never assert on an unqualified `SELECT count(*)`; scope assertions to rows the test created, and probe only through the transaction-rolled-back `conn` fixture.
- **DB tests skip silently without `TEST_PG_DSN`.** Always `set -a && . ./.env && set +a && uv run pytest <file>`, and read the summary line to confirm it says neither "skipped" nor a stale count.
- **Run tests in the FOREGROUND**, with a generous timeout. A previous implementer on this project stalled indefinitely on a backgrounded pytest and lost most of a mutation gate.
- **Do not run the full suite** (~8 minutes; the controller runs it). **Name the test FILE in selectors, never a `-k` substring** — a silently under-selecting `-k` looks identical to a passing run and has bitten this project twice.
- **Every new test is gated against a mutant.** Report each CAUGHT or SURVIVED honestly.
- **This repo is PUBLIC.** `imports/` holds real exports. Use fabricated symbols only (`ZXCO`, `ZXCB`). The deny-list guards identifiers, not values — real quantities and dates have reached drafts three times on this project. Never copy a quantity, price, date or symbol out of `imports/`.

---

## File Structure

| File | Responsibility |
|---|---|
| `db/migrations/003_derived_fills.sql` | **new** — `derived_fill`, three columns, two indexes, the `trade_fill` PK rework |
| `db/schema.sql` | modify: the same objects, so a fresh database matches a migrated one |
| `db/corporate.py` | modify: promote `_fetch_actions_for_instruments` to public `actions_with_ids_for_instruments` |
| `db/trades.py` | modify: `regroup_account` records the effective instrument, writes and reaps derived fills, routes allocations, and teaches both reaping predicates about derived openings |
| `db/positions.py` | modify: `open_positions` prefers the effective instrument |
| `cli.py` | modify: remove the type refusal and the three "unreachable" comments it stranded |
| `tests/db/test_migrations.py` | modify: migration `003` against a populated database |
| `tests/db/test_positions.py` | modify: Half A reporting |
| `tests/db/test_trades.py` | modify: Half B lifecycle, the reaping trap, provenance |
| `tests/db/test_cli.py` | modify: un-refusal, plus restoring the three tests PR #10 deleted |
| `docs/known-gaps.md`, `README.md` | modify |

---

## Task 1: Schema — `derived_fill` and the `trade_fill` rework

**Files:**
- Create: `db/migrations/003_derived_fills.sql`
- Modify: `db/schema.sql`
- Test: `tests/db/test_migrations.py`

**Interfaces:**
- Consumes: nothing.
- Produces: table `derived_fill`; columns `trade.effective_instrument_id`, `trade.opening_derived_fill_id`, `trade_fill.id`, `trade_fill.derived_fill_id`; constraint names `trade_one_opening_chk`, `trade_fill_one_source_chk`; indexes `trade_opening_derived_uniq`, `trade_fill_real_uniq`, `trade_fill_derived_uniq`.

**Read first:** `db/migrations/002_reject_non_finite_numerics.sql` (the idempotency pattern this must follow), `db/schema.sql`'s `fill`, `trade` and `trade_fill` definitions, and `tests/db/test_schema_equivalence.py`'s docstring — it explains exactly the divergence this task can cause.

**Why `trade_fill`'s primary key must change.** It is `PRIMARY KEY (trade_id, fill_id)` today with `fill_id NOT NULL`. A spinoff allocation has no `fill_id`, and **a composite primary key cannot contain a nullable column.** The PK is therefore dropped and replaced with a surrogate `id`, with the old uniqueness preserved by two partial unique indexes. This is the one destructive step in the branch.

**Ordering wrinkle.** `derived_fill` references `corporate_action`, which `schema.sql` declares *after* `trade` and `trade_fill`. Do **not** reorder existing table definitions. Declare `derived_fill` after `corporate_action`, and attach the two referring foreign keys as `ALTER TABLE ... ADD CONSTRAINT` below it — so `schema.sql`'s new text and the migration's are nearly identical.

- [ ] **Step 1: Write the failing test**

Add to `tests/db/test_migrations.py`:

```python
async def test_migration_003_survives_a_populated_trade_fill(conn):
    """The trade_fill PK rework is the only destructive step in 003. An empty
    database cannot exercise it: the failure mode is a NOT NULL or PK violation
    on rows that already exist."""
    acc = await create_account(conn, name="Mig3", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, quantity="10", price="1.00", ref="mig3")]
    )
    await regroup_account(conn, acc)
    before = await conn.fetchval(
        "SELECT count(*) FROM trade_fill WHERE account_id = $1", acc
    )
    assert before > 0

    await apply(conn)  # re-run: schema.sql + every migration, including 003

    after = await conn.fetch(
        "SELECT id, fill_id, derived_fill_id FROM trade_fill WHERE account_id = $1", acc
    )
    assert len(after) == before
    assert all(r["id"] is not None for r in after)
    assert all(r["fill_id"] is not None and r["derived_fill_id"] is None for r in after)


async def test_derived_fill_rejects_a_cross_account_trade_reference(conn):
    """derived_fill carries UNIQUE (id, account_id) so composite FKs get the
    same cross-account guard fill_id_account_uniq gives. Without it a trade in
    account B could anchor on a derived fill from account A."""
    row = await conn.fetchrow(
        """SELECT conname FROM pg_constraint
            WHERE conrelid = 'derived_fill'::regclass AND contype = 'u'"""
    )
    assert row is not None
```

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_migrations.py -v`
Expected: FAIL — `column "derived_fill_id" does not exist`, and `relation "derived_fill" does not exist`. **If you see "skipped", `TEST_PG_DSN` is unset and you are testing nothing.**

- [ ] **Step 3: Write the migration**

`db/migrations/003_derived_fills.sql`:

```sql
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
    fee                  NUMERIC NOT NULL DEFAULT 0,
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
```

- [ ] **Step 4: Mirror it in `db/schema.sql`**

Three edits, and **only** these:

1. In `CREATE TABLE IF NOT EXISTS trade (...)`, add the two columns and the CHECK inline:
   `effective_instrument_id UUID REFERENCES instrument(id),`,
   `opening_derived_fill_id UUID,`, and
   `CONSTRAINT trade_one_opening_chk CHECK (opening_fill_id IS NULL OR opening_derived_fill_id IS NULL)`.
   Leave `trade_opening_derived_fill_fk` out of the inline definition — `derived_fill` does not exist yet at that point in the file.
2. In `CREATE TABLE IF NOT EXISTS trade_fill (...)`, make it the post-rework shape: `id UUID PRIMARY KEY DEFAULT gen_random_uuid()`, `fill_id UUID` (no `NOT NULL`), `derived_fill_id UUID`, and `CONSTRAINT trade_fill_one_source_chk CHECK (num_nonnulls(fill_id, derived_fill_id) = 1)`. Remove `PRIMARY KEY (trade_id, fill_id)`. Add the two partial unique indexes beside the existing `trade_fill_fill_idx`.
3. **After** the `corporate_action` table, paste the `CREATE TABLE IF NOT EXISTS derived_fill (...)` block verbatim from the migration, followed by the three `ALTER TABLE ... ADD CONSTRAINT` statements for `trade_opening_derived_fill_fk`, `trade_effective_instrument_fk` and `trade_fill_derived_fk` — each preceded by its `DROP CONSTRAINT IF EXISTS`, because `schema.sql` re-runs on every `apply()`.

- [ ] **Step 5: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_migrations.py tests/db/test_schema.py tests/db/test_schema_equivalence.py -v`

`test_schema_equivalence.py` is the one that catches a mismatch between Steps 3 and 4. If it fails, the two files disagree — fix the disagreement, do not adjust the test.

- [ ] **Step 6: Mutation gate**

- Remove `trade_one_opening_chk` from the migration only (leave `schema.sql`) → `test_schema_equivalence.py` must FAIL.
- Remove `derived_fill_id_account_uniq` → `test_derived_fill_rejects_a_cross_account_trade_reference` must FAIL.
- Leave `trade_fill.fill_id` as `NOT NULL` (drop the `ALTER ... DROP NOT NULL` line from both files) → `test_migration_003_survives_a_populated_trade_fill` still passes, but Task 3 cannot work. **Report this one as SURVIVED** — it is genuinely not covered until Task 3 writes a derived allocation, and saying so is more useful than inventing a test here that duplicates Task 3's.

- [ ] **Step 7: Commit**

```bash
git add db/migrations/003_derived_fills.sql db/schema.sql tests/db/test_migrations.py
git commit -m "feat(db): derived_fill table and the trade_fill surrogate key"
```

---

## Task 2: Half A — the effective instrument

**Files:**
- Modify: `db/trades.py` (`regroup_account`'s trade UPSERT), `db/positions.py` (`_SQL`)
- Test: `tests/db/test_positions.py`

**Interfaces:**
- Consumes: `trade.effective_instrument_id` (Task 1).
- Produces: no new API. `open_positions` keeps its signature and return type; `OpenPosition.instrument_id` now reports the effective instrument.

**This half alone fixes `merger` and `symbol_change`.** Neither mints a fill id — both use `dataclasses.replace(f, instrument_id=...)` and keep the id — so no foreign key is ever violated by them. Their only defect is that `open_positions` reads the instrument from the *raw* opening fill, which read-time derivation never rewrites.

The CLI still refuses these types until Task 4, so these tests drive `db.corporate.add_action` and `regroup_account` directly. That is deliberate: it keeps this task's gate independent of the CLI's.

- [ ] **Step 1: Write the failing tests**

Add to `tests/db/test_positions.py`. `_split` and `account_with_1800` come from `tests/db/conftest.py` — import `_split`, and take `account_with_1800` as a fixture parameter. Do **not** redefine either.

```python
def _symbol_change(instrument_id, resulting_instrument_id, *, ex_date=date(2026, 3, 2)):
    return CorporateAction(
        instrument_id=instrument_id,
        action_type=ActionType.SYMBOL_CHANGE,
        ex_date=ex_date,
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(1),
        resulting_instrument_id=resulting_instrument_id,
    )


@pytest_asyncio.fixture
async def zxcb(conn):
    """The instrument a symbol change or merger resolves TO."""
    return await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCB", quote_currency="USD"),
    )


async def test_a_symbol_change_reports_the_position_under_the_new_instrument(
    conn, account_with_1800, zxcb
):
    """Before this, open_positions resolved the instrument from the RAW opening
    fill, which the adjustment never rewrites -- so the position kept reporting
    under the old symbol while `deadband trades` reported the new one, and a mark
    on the new symbol never priced it."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _symbol_change(instrument_id, zxcb))
    await regroup_account(conn, account_id)
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == zxcb
    assert position.symbol == "ZXCB"
    assert position.quantity == Decimal(1800)


async def test_a_merger_reports_the_new_instrument_and_the_rescaled_quantity(
    conn, account_with_1800, zxcb
):
    """A merger changes instrument AND magnitude. 1800 at 1:6 -> 300 of ZXCB."""
    account_id, instrument_id = account_with_1800
    await add_action(
        conn,
        CorporateAction(
            instrument_id=instrument_id,
            action_type=ActionType.MERGER,
            ex_date=date(2026, 3, 2),
            ratio_numerator=Decimal(1),
            ratio_denominator=Decimal(6),
            resulting_instrument_id=zxcb,
        ),
    )
    await regroup_account(conn, account_id)
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == zxcb
    assert position.quantity == Decimal(300)


async def test_a_reverse_split_leaves_the_instrument_alone(conn, account_with_1800):
    """effective_instrument_id is written for every trade, not only relabelled
    ones. A split must record the instrument it already had, not NULL and not
    something else."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == instrument_id
    assert position.quantity == Decimal(300)


async def test_a_trade_predating_the_column_still_reports_its_fills_instrument(
    conn, account_with_1800
):
    """effective_instrument_id is nullable and pre-existing rows are not
    backfilled, so the COALESCE fallback is load-bearing rather than defensive."""
    account_id, instrument_id = account_with_1800
    await regroup_account(conn, account_id)
    await conn.execute(
        "UPDATE trade SET effective_instrument_id = NULL WHERE account_id = $1", account_id
    )
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == instrument_id
```

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_positions.py -v`
Expected: FAIL — the first two report the *old* instrument, because nothing writes or reads `effective_instrument_id` yet.

- [ ] **Step 3: Implement — `db/trades.py`**

Add `effective_instrument_id` to the trade UPSERT. It comes from the adjusted fill's `instrument_id`, at the same moment and for the same reason `primary_underlying` already does. Add the column to the `INSERT ... (...)` list, one more placeholder to `VALUES`, one more line to `DO UPDATE SET`:

```python
                    effective_instrument_id = EXCLUDED.effective_instrument_id,
```

and pass the value positionally, immediately after `underlyings.get(g.instrument_ids[0])`:

```python
                by_id[opening_allocation.fill_id].instrument_id,
```

where `opening_allocation` is the allocation you already compute to derive `opening_fill_id` — hoist it into a local rather than calling `min(...)` twice:

```python
            # The opening allocation is this trade's stable identity across regroups.
            opening_allocation = min(
                g.allocations, key=lambda a: (by_id[a.fill_id].executed_at, str(a.fill_id))
            )
            opening_fill_id = opening_allocation.fill_id
```

- [ ] **Step 4: Implement — `db/positions.py`**

In `_SQL`, resolve the instrument through the effective column first:

```sql
      LEFT JOIN fill f       ON f.id = t.opening_fill_id
      LEFT JOIN instrument i ON i.id = COALESCE(t.effective_instrument_id, f.instrument_id)
```

Extend the existing comment above `_SQL` to say why the COALESCE is there:

```python
# The COALESCE below is not a defensive default. effective_instrument_id is
# written by regroup_account from the ADJUSTED fill, so it is the only place a
# symbol change or merger is visible: `fill` is never rewritten, so f.instrument_id
# still names the instrument the position was opened in. It stays NULL for trades
# written before the column existed, which is why the fallback is required rather
# than merely tidy.
```

- [ ] **Step 5: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_positions.py tests/db/test_trades.py -v`

Run `tests/db/test_trades.py` too: you changed `regroup_account`'s UPSERT, which it is the closest consumer of.

- [ ] **Step 6: Mutation gate**

- Drop `effective_instrument_id` from the `DO UPDATE SET` list (leave it in the INSERT) → `test_a_symbol_change_reports_the_position_under_the_new_instrument` must FAIL on the **second** regroup of the same account. If it does not, add a second `regroup_account` call to that test — an UPSERT whose update branch is untested is only half covered.
- Reverse the COALESCE to `COALESCE(f.instrument_id, t.effective_instrument_id)` → `test_a_symbol_change_reports_the_position_under_the_new_instrument` must FAIL.
- Write `effective_instrument_id` from `g.instrument_ids[0]` instead of the opening allocation's fill → report what reddens. These agree for every single-instrument trade, so a survival here is expected and is not a defect; say so plainly.

- [ ] **Step 7: Commit**

```bash
git add db/trades.py db/positions.py tests/db/test_positions.py
git commit -m "feat(db): report positions under the effective instrument"
```

---

## Task 3: Half B — persisting spinoff children

**Files:**
- Modify: `db/corporate.py`, `db/trades.py` (`regroup_account`)
- Test: `tests/db/test_trades.py`

**Interfaces:**
- Consumes: `derived_fill` and the routing columns (Task 1); `effective_instrument_id` (Task 2).
- Produces:
  ```python
  # db/corporate.py
  async def actions_with_ids_for_instruments(
      conn, instrument_ids: Sequence[UUID]
  ) -> list[tuple[UUID, CorporateAction]]
  ```
  `actions_for_instruments` keeps its existing signature and behaviour for its existing callers.

**This is the correctness core of the plan.** Read spec §5.1a and §5.3 before writing anything.

**§5.3, restated because it is the thing most likely to be missed.** Both the protection `UPDATE` and the final `DELETE` treat a trade as stale when `opening_fill_id IS NULL OR NOT (opening_fill_id = ANY($2))`. A spinoff-derived trade has **no** `opening_fill_id` by construction, so under the existing predicates it is written and then reaped by the very next statement — or, if it carries notes, hollowed into a judgment-only husk. **This fails quietly**: a test that stops before the reaping passes, and real use produces nothing. That is why Step 1's first test regroups **twice**.

**§5.1a, restated.** `adjust_fills` returns bare `Fill`s with no link to the action that produced them, but `derived_fill.derived_from_fill_id` and `corporate_action_id` are `NOT NULL`. Recover the link by **inverting the id function** — compute `_spinoff_fill_id(parent, action)` for every (pre-adjustment fill id × stored spinoff action) pair and index the derived fills against it. Do **not** re-derive which fills a spinoff applies to; that would duplicate `_ordered_actions`' ordering and ex-date rules in a second place. A derived fill matching no pair is a bug — raise, do not insert with NULLs.

- [ ] **Step 1: Write the failing tests**

Add to `tests/db/test_trades.py`. Import `_split` and use `account_with_1800` from `tests/db/conftest.py`; add a local `_spinoff` helper and a `zxcb` fixture as in Task 2.

```python
def _spinoff(instrument_id, resulting_instrument_id, *, num="1", den="10",
             allocation="0.375", ex_date=date(2026, 3, 2)):
    return CorporateAction(
        instrument_id=instrument_id,
        action_type=ActionType.SPINOFF,
        ex_date=ex_date,
        ratio_numerator=Decimal(num),
        ratio_denominator=Decimal(den),
        resulting_instrument_id=resulting_instrument_id,
        basis_allocation=Decimal(allocation),
    )


async def test_a_spinoff_survives_a_second_regroup(conn, account_with_1800, zxcb):
    """THE reaping test. Both the protection UPDATE and the final DELETE treat
    opening_fill_id IS NULL as stale, and a derived trade has no opening fill by
    construction -- so a first regroup can write it and the next statement can
    reap it. One regroup cannot tell a correct implementation from that."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _spinoff(instrument_id, zxcb))
    await regroup_account(conn, account_id)
    await regroup_account(conn, account_id)
    positions = await open_positions(conn, account_id)
    assert {p.instrument_id for p in positions} == {instrument_id, zxcb}


async def test_a_spinoff_creates_the_child_position(conn, account_with_1800, zxcb):
    """1800 shares, 1:10 spinoff, 37.5% of basis allocated -> 180 shares of ZXCB."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _spinoff(instrument_id, zxcb))
    await regroup_account(conn, account_id)
    positions = {p.instrument_id: p for p in await open_positions(conn, account_id)}
    assert positions[zxcb].quantity == Decimal(180)
    assert positions[instrument_id].quantity == Decimal(1800)


async def test_removing_a_spinoff_removes_its_derived_fill(conn, account_with_1800, zxcb):
    """Derived rows must not outlive the action that produced them, or removal
    stops being a genuine undo and becomes a second restatement."""
    account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _spinoff(instrument_id, zxcb))
    await regroup_account(conn, account_id)
    assert await conn.fetchval(
        "SELECT count(*) FROM derived_fill WHERE account_id = $1", account_id
    ) == 1
    await remove_action(conn, action_id)
    await regroup_account(conn, account_id)
    assert await conn.fetchval(
        "SELECT count(*) FROM derived_fill WHERE account_id = $1", account_id
    ) == 0
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == instrument_id


async def test_the_derived_fill_records_its_parent_and_its_action(
    conn, account_with_1800, zxcb
):
    """Provenance is recovered by inverting _spinoff_fill_id, not guessed. A row
    that cannot be attributed is a bug, not a row to insert with NULLs."""
    account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _spinoff(instrument_id, zxcb))
    (parent,) = await fetch_fills(conn, account_id)
    await regroup_account(conn, account_id)
    row = await conn.fetchrow(
        "SELECT * FROM derived_fill WHERE account_id = $1", account_id
    )
    assert row["derived_from_fill_id"] == parent.id
    assert row["corporate_action_id"] == action_id
    assert row["instrument_id"] == zxcb


async def test_two_spinoffs_are_not_cross_attributed(conn, account_with_1800, zxcb, zxcc):
    """_spinoff_fill_id hashes the resulting instrument and the ex-date, so two
    spinoffs off the same parent must land on different derived rows pointing at
    different actions."""
    account_id, instrument_id = account_with_1800
    first = await add_action(conn, _spinoff(instrument_id, zxcb))
    second = await add_action(
        conn, _spinoff(instrument_id, zxcc, ex_date=date(2026, 4, 2))
    )
    await regroup_account(conn, account_id)
    rows = await conn.fetch(
        "SELECT instrument_id, corporate_action_id FROM derived_fill WHERE account_id = $1",
        account_id,
    )
    assert {(r["instrument_id"], r["corporate_action_id"]) for r in rows} == {
        (zxcb, first),
        (zxcc, second),
    }


async def test_the_spun_off_shares_can_be_sold(conn, account_with_1800, zxcb):
    """D6: a view cannot be closed. A real SELL on the resulting instrument has
    to find a real opening trade to close against."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _spinoff(instrument_id, zxcb))
    await insert_fills(
        conn,
        [
            _fill(account_id, zxcb, side=Side.SELL, quantity="180", price="1.00",
                  ref="zxcb-sell")
        ],
    )
    await regroup_account(conn, account_id)
    positions = {p.instrument_id for p in await open_positions(conn, account_id)}
    assert zxcb not in positions


async def test_only_a_spinoff_mints_a_fill_id(conn, account_with_1800, zxcb):
    """§5.1: derived fills are identified by set difference against the fetched
    ids. That is only sound while spinoff is the sole action type that invents an
    id. If another type ever starts minting them, this reddens instead of
    silently mis-filing them as real."""
    account_id, instrument_id = account_with_1800
    before = {f.id for f in await fetch_fills(conn, account_id)}
    for action in (
        _split(instrument_id),
        _symbol_change(instrument_id, zxcb),
        _merger(instrument_id, zxcb),
    ):
        adjusted = adjust_fills(await fetch_fills(conn, account_id), [action])
        assert {f.id for f in adjusted} <= before
```

`zxcc` is a third fabricated instrument (`ZXCC`), built the same way as `zxcb`. `_symbol_change` and `_merger` are the local helpers from Task 2's file — repeat them in this file rather than importing across test modules.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_trades.py -v`
Expected: FAIL — `ForeignKeyViolationError` on `trade_opening_fill_fk`, exactly the error this branch exists to remove.

- [ ] **Step 3: Implement — `db/corporate.py`**

Promote the private helper. Rename `_fetch_actions_for_instruments` to `actions_with_ids_for_instruments`, make it public, and have `actions_for_instruments` call it:

```python
async def actions_with_ids_for_instruments(
    conn: asyncpg.Connection, instrument_ids: Sequence[UUID]
) -> list[tuple[UUID, CorporateAction]]:
    """Stored actions with their row ids, for callers that must record which
    action produced what. `actions_for_instruments` drops the ids; regroup_account
    needs them to fill derived_fill.corporate_action_id."""
    ...  # body unchanged from the private version


async def actions_for_instruments(
    conn: asyncpg.Connection, instrument_ids: Sequence[UUID]
) -> list[CorporateAction]:
    return [a for _id, a in await actions_with_ids_for_instruments(conn, instrument_ids)]
```

Update `preview_effect`'s internal call site to the new name.

- [ ] **Step 4: Implement — `db/trades.py`**

Import the id function and the new accessor:

```python
from db.corporate import actions_with_ids_for_instruments
# Private by name, but this is the allowed import direction (db -> ledger) and
# inverting this exact hash is how a derived fill's provenance is recovered
# without re-deriving which fills a spinoff applies to. See the design's §5.1a.
from ledger.corporate import ActionType, _spinoff_fill_id, adjust_fills
```

Capture the real ids before adjusting, and build the provenance map:

```python
    real_ids = {f.id for f in fills}
    derived_provenance: dict[UUID, tuple[UUID, UUID]] = {}

    if fills:
        pairs = await actions_with_ids_for_instruments(
            conn, list({f.instrument_id for f in fills})
        )
        if pairs:
            # Invert _spinoff_fill_id over (parent x spinoff action). Enumerating a
            # hash cannot drift from adjust_fills; re-deciding WHICH fills a spinoff
            # applies to would duplicate _ordered_actions' rules in a second place.
            for action_id, action in pairs:
                if action.action_type is not ActionType.SPINOFF:
                    continue
                for parent_id in real_ids:
                    derived_provenance[_spinoff_fill_id(parent_id, action)] = (
                        parent_id,
                        action_id,
                    )
            fills = adjust_fills(fills, [a for _id, a in pairs])

    derived = [f for f in fills if f.id not in real_ids]
```

Write the derived rows **before** any trade references them:

```python
    for d in derived:
        provenance = derived_provenance.get(d.id)
        if provenance is None:
            # Not a guess-and-insert: a synthetic fill we cannot attribute means
            # adjust_fills minted an id we do not model, and inserting it with
            # NULL provenance would bury that.
            raise UnattributableDerivedFillError(d.id)
        parent_id, action_id = provenance
        await conn.execute(
            """
            INSERT INTO derived_fill
                (id, account_id, instrument_id, executed_at, side, quantity, price,
                 fee, is_estimated, derived_from_fill_id, corporate_action_id)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            ON CONFLICT (id) DO UPDATE SET
                instrument_id = EXCLUDED.instrument_id,
                quantity      = EXCLUDED.quantity,
                price         = EXCLUDED.price
            """,
            d.id, account_id, d.instrument_id, d.executed_at, d.side.value,
            d.quantity, d.price, d.fee, d.is_estimated, parent_id, action_id,
        )
```

Define the error beside the module's other domain errors:

```python
class UnattributableDerivedFillError(RuntimeError):
    """adjust_fills produced a synthetic fill whose id inverts to no known
    (parent, action) pair. See the design's section 5.1a."""

    def __init__(self, fill_id: UUID) -> None:
        super().__init__(f"cannot attribute derived fill {fill_id} to a corporate action")
        self.fill_id = fill_id
```

Route the opening allocation and the allocations, and track two seen-lists:

```python
    derived_ids = {f.id for f in derived}
    seen_openings: list[UUID] = []
    seen_derived_openings: list[UUID] = []
```

```python
            is_derived_opening = opening_fill_id in derived_ids
            if is_derived_opening:
                seen_derived_openings.append(opening_fill_id)
            else:
                seen_openings.append(opening_fill_id)
```

The UPSERT's `ON CONFLICT` target differs by opening kind, so build the statement from a shared body with the conflict clause substituted — two module-level constants, `_TRADE_UPSERT_ON_FILL` and `_TRADE_UPSERT_ON_DERIVED`, differing only in
`ON CONFLICT (account_id, opening_fill_id) WHERE opening_fill_id IS NOT NULL` versus
`ON CONFLICT (account_id, opening_derived_fill_id) WHERE opening_derived_fill_id IS NOT NULL`,
and in which of the two opening columns receives the id (the other is passed `None`).

Allocations route on the same set:

```python
            await conn.executemany(
                "INSERT INTO trade_fill "
                "(trade_id, fill_id, derived_fill_id, account_id, quantity) "
                "VALUES ($1,$2,$3,$4,$5)",
                [
                    (
                        trade_id,
                        None if a.fill_id in derived_ids else a.fill_id,
                        a.fill_id if a.fill_id in derived_ids else None,
                        account_id,
                        a.quantity,
                    )
                    for a in g.allocations
                ],
            )
```

Teach **both** reaping predicates about derived openings. In the protection `UPDATE` and the final `DELETE`, replace

```sql
   AND (opening_fill_id IS NULL OR NOT (opening_fill_id = ANY($2::uuid[])))
```

with

```sql
   AND ((opening_fill_id IS NULL AND opening_derived_fill_id IS NULL)
        OR (opening_fill_id IS NOT NULL
            AND NOT (opening_fill_id = ANY($2::uuid[])))
        OR (opening_derived_fill_id IS NOT NULL
            AND NOT (opening_derived_fill_id = ANY($4::uuid[]))))
```

passing `seen_derived_openings` as the new parameter. A trade with **both** columns NULL is still stale, which preserves the orphan path exactly. Add `opening_derived_fill_id = NULL` to the protection `UPDATE`'s `SET` list beside `opening_fill_id = NULL`, for the same reason: a protected trade must not collide with a future auto upsert.

Reap stale derived rows **last**, after the trades that referenced them are gone:

```python
    await conn.execute(
        """
        DELETE FROM derived_fill
         WHERE account_id = $1
           AND NOT (id = ANY($2::uuid[]))
        """,
        account_id,
        list(derived_ids),
    )
```

- [ ] **Step 5: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_trades.py tests/db/test_positions.py tests/db/test_corporate_actions.py -v`

All three: you changed `db/corporate.py`'s public surface and `regroup_account`'s write path, and `test_positions.py` is the closest consumer of its output.

- [ ] **Step 6: Mutation gate**

- Leave both reaping predicates unchanged (the original `opening_fill_id IS NULL OR ...`) → `test_a_spinoff_survives_a_second_regroup` must FAIL. **This is the most important mutation in the plan.**
- Delete the `derived_fill` reap at the end → `test_removing_a_spinoff_removes_its_derived_fill` must FAIL.
- Attribute every derived fill to the first spinoff action instead of the inverted-hash lookup → `test_two_spinoffs_are_not_cross_attributed` must FAIL.
- Insert with `derived_from_fill_id = NULL` instead of raising → a `NotNullViolationError`, not a clean failure. Report which test reddens and how; if the failure is an integrity error rather than an assertion, say so — it means the schema is carrying the guard rather than the test.
- Route every allocation to `fill_id` regardless → `test_a_spinoff_creates_the_child_position` must FAIL on `trade_fill_fill_fk`.

- [ ] **Step 7: Commit**

```bash
git add db/corporate.py db/trades.py tests/db/test_trades.py
git commit -m "feat(db): persist spinoff children as derived fills"
```

---

## Task 4: Remove the CLI refusal

**Files:**
- Modify: `cli.py`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1-3.
- Produces: no new API. `cmd_corporate_add` keeps its signature.

**Read `cli.py`'s `_UNMATERIALISABLE_TYPES` block first** (added in PR #10, commit `8292e9e`). Removing it is the point of this task. PR #10 also left three guards commented as unreachable — the merger/spinoff resulting-symbol presence check, the spinoff basis-allocation presence check, and the basis-allocation `Decimal` parse. **All three become live code again**; delete the "unreachable" comments, do not delete the guards.

**PR #10 deleted three CLI tests as genuinely vacuous** once the types were refused: `test_corporate_add_refuses_a_merger_with_no_resulting_symbol`, `test_corporate_add_refuses_a_spinoff_with_no_basis_allocation`, and the two non-finite `--basis-allocation` parametrised cases. Their inputs are reachable again, so **restore them**. Their invariants currently survive only at the pure layer in `tests/test_corporate.py`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_corporate_add_commits_a_symbol_change(conn, account_with_1800, zxcb_symbol, monkeypatch):
    """The refusal added in 8292e9e comes off here. Before Tasks 1-3 this stored
    an action whose effect was reported under the OLD instrument."""
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="symbol_change", symbol="ZXCO", ex_date="2026-03-02",
        ratio="1:1", resulting_symbol="ZXCB", commit=True))
    assert rc == 0
    (position,) = await open_positions(conn, account_id)
    assert position.symbol == "ZXCB"


async def test_corporate_add_commits_a_spinoff(conn, account_with_1800, zxcb_symbol, monkeypatch):
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="spinoff", symbol="ZXCO", ex_date="2026-03-02", ratio="1:10",
        resulting_symbol="ZXCB", basis_allocation="0.375", commit=True))
    assert rc == 0
    symbols = {p.symbol for p in await open_positions(conn, account_id)}
    assert symbols == {"ZXCO", "ZXCB"}


async def test_corporate_add_refuses_a_merger_with_no_resulting_symbol(
    conn, account_with_1800, monkeypatch, capsys
):
    """RESTORED from PR #10, which deleted it as vacuous once merger was refused
    outright. The guard it covers is live code again."""
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _pool_that_must_not_open())
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="merger", symbol="ZXCO", ex_date="2026-03-02", ratio="1:6", commit=True))
    assert rc == 2
    assert "resulting" in capsys.readouterr().err.lower()


async def test_corporate_add_refuses_a_spinoff_with_no_basis_allocation(
    conn, account_with_1800, monkeypatch, capsys
):
    """RESTORED from PR #10. Refuses in stage 1, before a connection is opened --
    which is why ZXCB never needs to exist for this test."""
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _pool_that_must_not_open())
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="spinoff", symbol="ZXCO", ex_date="2026-03-02", ratio="1:1",
        resulting_symbol="ZXCB", commit=True))
    assert rc == 2
    assert "basis" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("allocation", ["NaN", "abc"])
async def test_corporate_add_refuses_a_basis_allocation_that_is_not_a_finite_number(
    conn, account_with_1800, monkeypatch, allocation
):
    """RESTORED from PR #10. InvalidOperation is NOT a ValueError subclass, and
    an ordering comparison against Decimal('NaN') raises rather than returning
    False -- so the is_finite() guard is load-bearing, not decorative."""
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _pool_that_must_not_open())
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="spinoff", symbol="ZXCO", ex_date="2026-03-02", ratio="1:10",
        resulting_symbol="ZXCB", basis_allocation=allocation, commit=True))
    assert rc == 2
```

`zxcb_symbol` is a fixture creating the `ZXCB` instrument so `resolve_instrument_by_symbol` finds it. `_pool_that_must_not_open()` is the existing idiom in this file — a `create_pool` replacement that raises `AssertionError`, proving the refusal happened before any connection.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`
Expected: FAIL — the first two return 2 from the `_UNMATERIALISABLE_TYPES` refusal. Run the whole file; do not use an unverified `-k`.

- [ ] **Step 3: Implement**

Delete `_UNMATERIALISABLE_TYPES` and the block in `cmd_corporate_add` that consults it. Delete the three "unreachable" comments PR #10 left on the resulting-symbol presence check, the basis-allocation presence check, and the basis-allocation parse — keeping the code. Leave the `--resulting-symbol` / `--basis-allocation` wrong-type refusals exactly as they are: they guard a real dependency-graph hazard and are unaffected.

Fix the two `--help` strings PR #10 left stale (issue #13 tracks them; they are in scope here because this task makes them correct again rather than merely less wrong): `--resulting-symbol`'s "required for merger, spinoff and symbol_change" is accurate once more, and `--basis-allocation`'s "spinoff only" likewise. Verify both read correctly rather than assuming.

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`

- [ ] **Step 5: Mutation gate**

- Restore the `_UNMATERIALISABLE_TYPES` refusal → `test_corporate_add_commits_a_symbol_change` and `test_corporate_add_commits_a_spinoff` must FAIL.
- Delete the resulting-symbol presence guard → `test_corporate_add_refuses_a_merger_with_no_resulting_symbol` must FAIL.
- Delete the `is_finite()` check on `--basis-allocation` → the `NaN` parametrisation must FAIL. Report whether it fails cleanly (exit 2 expected, something else returned) or raises — an `InvalidOperation` escaping to the caller is a different bug from a wrong return code.

- [ ] **Step 6: Commit**

```bash
git add cli.py tests/db/test_cli.py
git commit -m "feat(cli): accept merger, spinoff and symbol_change"
```

---

## Task 5: Documentation

**Files:**
- Modify: `docs/known-gaps.md`, `README.md`

- [ ] **Step 1: Find the current highest gap number.**

`grep -oE '^\| [0-9]+ \|' docs/known-gaps.md | tail -3`. **Check, do not assume** — this file has had two renumbering incidents and one session hit a conflict where two branches each added the same number. It ended at #39 when this plan was written; verify.

- [ ] **Step 2: Close gap #39 and narrow #38.**

Gap #39 recorded that identity-changing actions cannot be materialised and that the CLI refuses them. Both halves are now false. Rewrite the row to record what remains true: `derived_fill` is invisible to the CLI (no command explains where a spun-off position came from), and the derived-id invariant of §5.1 is a convention pinned by a test rather than a constraint the schema can express.

Gap #38 covered two things: which accounts get regrouped, and positions reporting under the old instrument. **The second half is fixed** — remove it and leave the scoping half, which is unchanged.

- [ ] **Step 3: Record the new gaps** the design's §9 names, each in its own row, matching neighbouring rows' format and depth of reasoning (read several first — these rows carry real argument, not one-liners):

1. **`derived_fill` is invisible to the CLI.** A user seeing a spun-off position has no command that shows where it came from, only the `corporate list` entry for the action.
2. **The derived-id invariant is a convention, not a constraint.** Derived fills are identified by set difference against the fetched ids. A test pins it, but the schema cannot express "`adjust_fills` only mints ids for spinoffs".
3. **Merger cash is still not modelled.** `cash_component` remains stored and never read by `adjust_fills`. Accepting mergers makes the omission reachable in practice rather than theoretical — cross-reference the existing gap rather than duplicating it.

- [ ] **Step 4: README** — update the corporate-actions section. It currently says split and reverse-split round-trip and the other three are refused; all five now work. Explain that a spinoff creates a second position, and that `--basis-allocation` is the fraction of the parent's cost basis that moves to the child. Keep `--ratio NEW:OLD`'s existing explanation intact — it is unchanged and inverting it is still wrong by a factor of 36.

- [ ] **Step 5: Verify and commit.**

Open every file you cite and check the line numbers resolve. **Cross-check every numeric token and symbol in your diff against `imports/`** and report the command you ran — the deny-list guards identifiers, not values. Stage, run `.githooks/pre-commit` without bypassing it, and confirm `git diff --cached --stat` shows only documentation.

```bash
git add docs/known-gaps.md README.md
git commit -m "docs: all five corporate action types now materialise"
```

---

## Self-Review

**Spec coverage.** §4.1 `derived_fill` → Task 1. §4.2 `trade` columns → Task 1 (schema) and Tasks 2-3 (writes). §4.3 `trade_fill` rework → Task 1. §4.4 `schema.sql` parity → Task 1, Steps 4-5, gated by `test_schema_equivalence.py`. §5.1 derived-fill identification → Task 3, `test_only_a_spinoff_mints_a_fill_id`. §5.1a provenance → Task 3, `test_the_derived_fill_records_its_parent_and_its_action` and `test_two_spinoffs_are_not_cross_attributed`. §5.2 write order → Task 3, Step 4. §5.3 the reaping trap → Task 3, `test_a_spinoff_survives_a_second_regroup` and its first mutation. §5.4 removal as undo → Task 3, `test_removing_a_spinoff_removes_its_derived_fill`. §6 reporting → Task 2. §7 failure policy → Task 4. §8 testing → distributed; the populated-migration test is Task 1, the restored tests are Task 4. §9 gaps → Task 5. D6 (spinoffs must be real trades) → Task 3, `test_the_spun_off_shares_can_be_sold`.

**Placeholders.** None. Every code step carries its code; every test step carries its test. Task 3's UPSERT-constant split is described rather than pasted twice, because the two statements differ only in their `ON CONFLICT` clause and pasting both invites them to drift — the difference is stated exactly.

**Type consistency.** `actions_with_ids_for_instruments(conn, instrument_ids) -> list[tuple[UUID, CorporateAction]]` is defined in Task 3 and consumed there only. `_spinoff_fill_id(parent_fill_id, action) -> UUID` is imported, not defined. `UnattributableDerivedFillError(fill_id)` is defined and raised in Task 3. `effective_instrument_id` is written in Task 2 and read in Task 2; `opening_derived_fill_id` and `trade_fill.derived_fill_id` are created in Task 1 and written in Task 3. `seen_openings` stays `list[UUID]` and gains a sibling `seen_derived_openings`, not a change of type.

**Known soft spot.** Task 3 is much the largest task and its correctness depends on a predicate change (§5.3) whose failure is silent. Its first mutation exists specifically to prove that predicate is load-bearing; if that mutation SURVIVES, the reaping change is untested no matter how many other tests pass, and it should be reported rather than passed over. Task 1's third mutation is expected to survive by construction and says so.
