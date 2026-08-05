# A-2 Part 1: Ledger Correctness & Schema — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the ledger's grouping and P&L arithmetic correct, and land every schema change A-2 needs, before any importer work codes against that schema.

**Architecture:** Six independent tasks. Task 1 is a pure-layer fix with no schema dependency. Task 2 builds the guard that makes every later migration safe. Task 3 lands all six columns and three constraints in one migration. Tasks 4–6 code against that final shape. Nothing here touches HTTP, UI, or the importers — those are Part 2.

**Tech Stack:** Python 3.10+, asyncpg, PostgreSQL 15+, pytest, `uv` for execution.

## Global Constraints

- **PostgreSQL 15+ required.** `db/schema.sql` uses column-scoped `ON DELETE SET NULL (opening_fill_id)`.
- **The pure layer takes no I/O, no clock, no network.** `tests/test_purity.py` enforces this; anything under `ledger/` must stay pure.
- **Every pure module that does decimal arithmetic pins its own precision** via `localcontext()`. Four separate modules each needed this in A-1 and each was missed until reviewed.
- **Database tests are opt-in.** They require `TEST_PG_DSN` and use the `requires_db` marker plus the `conn` fixture from `tests/conftest.py`.
- **Every schema change is written twice** — in `db/schema.sql` for fresh databases and in a numbered migration for existing ones. Task 2 builds the test that enforces this.
- **Migration filenames are zero-padded** (`001_x.sql`), because `sorted()` is lexicographic.
- **This repository is public.** No real account numbers, balances, holdings, or host detail in any file. See `.claude/skills/public-repo-hygiene`.
- **Gate every new test against a mutant before accepting it.** Ten assertions that could not fail shipped during A-1.

---

## Task 1: Quantity-aware exclusion in `group_fills`

The auto-regroup pass excludes a manual trade's fills **whole**. A zero-crossing fill may be only partly a manual trade's; the remainder is then never regrouped and is reaped. A `NotImplementedError` in `regroup_account` currently converts this into a hard error for the entire regroup.

The fix keeps `ledger/grouping.py` untouched: the database layer reduces each fill's available quantity by what manual trades already hold, and passes the reduced fills to the pure grouper.

**Files:**
- Modify: `db/trades.py:63-91` (the `NotImplementedError` guard and the fill filter)
- Test: `tests/db/test_trades.py`

**Interfaces:**
- Consumes: `ledger.grouping.group_fills(fills: list[Fill]) -> list[TradeGroup]` (unchanged)
- Produces: no new public names. `regroup_account` stops raising `NotImplementedError`.

- [ ] **Step 1: Write the failing test**

Add to `tests/db/test_trades.py`:

```python
@requires_db
async def test_partial_manual_allocation_leaves_the_remainder_groupable(conn):
    """SELL 1 @100 then BUY 5 @90 closes a short of 1 and opens a long of 4.
    Marking the closed short manual must not strand the open long of 4: the
    BUY fill is only 1/5 the manual trade's, and the other 4 must regroup.

    Asserts on the surviving open quantity, not on a trade count -- a count of
    2 would also hold if the remainder were grouped with the wrong quantity.
    """
    account_id = await create_account(
        conn, name="t", venue="fidelity", account_type="cash"
    )
    inst = await upsert_instrument(conn, _equity("ACME"))
    sell = await insert_fill(conn, account_id, inst, side="sell", qty="1", price="100")
    buy = await insert_fill(conn, account_id, inst, side="buy", qty="5", price="90")

    await regroup_account(conn, account_id)
    closed = await _trade_holding(conn, account_id, sell)
    await conn.execute(
        "UPDATE trade SET grouping_mode = 'manual' WHERE id = $1", closed["id"]
    )

    await regroup_account(conn, account_id)   # must not raise

    rows = await conn.fetch(
        "SELECT qty_opened, qty_closed, status FROM trade WHERE account_id = $1",
        account_id,
    )
    open_qty = sum(
        r["qty_opened"] - r["qty_closed"] for r in rows if r["status"] == "open"
    )
    assert open_qty == Decimal("4")
    total_allocated = await conn.fetchval(
        "SELECT sum(quantity) FROM trade_fill WHERE fill_id = $1", buy
    )
    assert total_allocated == Decimal("5")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/test_trades.py::test_partial_manual_allocation_leaves_the_remainder_groupable -v`

Expected: FAIL with `NotImplementedError: a manual trade holds a partial allocation of ...`

- [ ] **Step 3: Replace the guard with quantity-aware exclusion**

In `db/trades.py`, delete the `NotImplementedError` block (lines ~63-89) and replace the fill filter at line ~91:

```python
    # How much of each fill is already held by a manual trade. A manual trade may
    # hold only PART of a zero-crossing fill, so excluding the fill whole would
    # strand -- and then reap -- the remainder. Reduce the available quantity
    # instead, and let the pure grouper allocate what is left.
    manual_held: dict[UUID, Decimal] = {
        r["fill_id"]: r["held"]
        for r in await conn.fetch(
            """SELECT tf.fill_id, sum(tf.quantity) AS held
                 FROM trade_fill tf
                 JOIN trade t ON t.id = tf.trade_id
                WHERE t.account_id = $1 AND t.grouping_mode = 'manual'
             GROUP BY tf.fill_id""",
            account_id,
        )
    }

    fills = []
    for f in await fetch_fills(conn, account_id):
        remaining = f.quantity - manual_held.get(f.id, Decimal(0))
        if remaining <= 0:
            continue  # wholly owned by a manual trade
        fills.append(f if remaining == f.quantity else replace(f, quantity=remaining))
```

Add `from dataclasses import replace` and `from decimal import Decimal` to the imports if absent.

- [ ] **Step 4: Run the test to verify it passes**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/test_trades.py -v`

Expected: PASS, and every pre-existing test in the file still passes.

- [ ] **Step 5: Verify the whole suite**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest -q`

Expected: all pass, output pristine. Any test asserting the `NotImplementedError` is now obsolete — delete it deliberately and note it in the commit message rather than leaving it skipped.

- [ ] **Step 6: Commit**

```bash
git add db/trades.py tests/db/test_trades.py
git commit -m "fix(grouping): exclude manual trades by quantity, not by fill id

A manual trade holding part of a zero-crossing fill made the auto pass
exclude that fill whole, stranding the remainder to be reaped. Reduce each
fill's available quantity by what manual trades hold and let the pure grouper
allocate the rest. Removes the NotImplementedError that converted this into a
hard failure for the entire regroup.

Unblocks A-4's manual-grouping UI, which would otherwise stop imports for an
account the moment it created its first partial allocation."
```

---

## Task 2: Baseline schema freeze and the equivalence guard

`migrate.apply()` re-executes `schema.sql` unconditionally, then applies `db/migrations/*.sql` once each. Because `schema.sql` is idempotent, `CREATE TABLE IF NOT EXISTS` will **not** add a column to an existing table — so every change must be written in both places. A-2 pushes six columns and three constraints through that path.

This task builds the guard **before** the first migration exists, so it covers every one that follows. `db/migrations/` is currently empty, which makes now the only moment a baseline can be frozen honestly.

**Files:**
- Create: `tests/fixtures/schema_baseline_a1.sql` (byte-for-byte copy of the current `db/schema.sql`)
- Create: `tests/db/test_schema_equivalence.py`

**Interfaces:**
- Produces: `tests/db/test_schema_equivalence.py::_describe(conn, namespace) -> dict` — used only within this file.

- [ ] **Step 1: Freeze the baseline**

```bash
cp db/schema.sql tests/fixtures/schema_baseline_a1.sql
```

This file is **never edited again**. It represents what a database created before A-2 looks like. Migrations accumulate against it forever.

- [ ] **Step 2: Write the failing test**

Create `tests/db/test_schema_equivalence.py`:

```python
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
    """Columns and check constraints of every table in a namespace."""
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
    return {
        "columns": [tuple(r) for r in cols],
        "checks": sorted((r["table_name"], r["check_clause"]) for r in checks),
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

    assert fresh["columns"] == migrated["columns"], (
        "schema.sql and baseline+migrations disagree on columns -- a change was "
        "written to one and not the other"
    )
    assert fresh["checks"] == migrated["checks"], (
        "schema.sql and baseline+migrations disagree on CHECK constraints"
    )
```

- [ ] **Step 3: Run it and confirm it passes trivially**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/test_schema_equivalence.py -v`

Expected: PASS. With zero migrations the two sides are the same file, so passing is correct — but a passing test proves nothing yet.

- [ ] **Step 4: Prove the test can fail (mutant gate)**

This is the step that makes the test real. Temporarily append to `db/schema.sql`:

```sql
ALTER TABLE account ADD COLUMN IF NOT EXISTS mutant_check TEXT;
```

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/test_schema_equivalence.py -v`

Expected: **FAIL** on the columns assertion. Then **revert the edit** and confirm it passes again. Do not proceed until you have seen it fail — an equivalence test that cannot detect divergence is exactly the "assertions that cannot fail" defect this repository has shipped ten of.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/schema_baseline_a1.sql tests/db/test_schema_equivalence.py
git commit -m "test(db): guard schema.sql against migration divergence

migrate.apply() re-runs an idempotent schema.sql, so CREATE TABLE IF NOT
EXISTS skips an existing table: a column added only to schema.sql reaches
fresh databases and never reaches migrated ones, and both keep working while
disagreeing. A-2 pushes six columns and three constraints through that path.

Freezes the A-1 schema as a baseline and asserts schema.sql equals
baseline + all migrations. Verified failing against a deliberately divergent
column before acceptance."
```

---

## Task 3: Migration 001 — columns, constraints, trigger

All six columns and three constraint changes in one pass, so Tasks 4–6 code against the final shape.

**Files:**
- Create: `db/migrations/001_a2_ledger_completion.sql`
- Modify: `db/schema.sql` (same changes, for fresh databases)
- Test: covered by Task 2's equivalence test plus a new constraint test

**Interfaces:**
- Produces: columns `trade.fees_realized`, `trade.open_quantity`, `trade.open_cost_basis`, `trade.is_estimated`, `fill.funding_source`, `account.ignore_on_import`. Cash kinds `tax` and `return_of_capital`.

- [ ] **Step 1: Write the migration**

Create `db/migrations/001_a2_ledger_completion.sql`:

```sql
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
```

- [ ] **Step 2: Mirror every change into `db/schema.sql`**

Add the six columns to their `CREATE TABLE` bodies, add the three `CHECK` constraints inline, extend the `cash_movement.kind` CHECK with `'tax','return_of_capital'`, and append the `set_updated_at` function and `fill_set_updated_at` trigger at the end of the file.

- [ ] **Step 3: Run the equivalence test**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/test_schema_equivalence.py -v`

Expected: PASS. If it fails, `schema.sql` and the migration disagree — that is the guard from Task 2 doing its job. Fix the mismatch; do not weaken the test.

- [ ] **Step 4: Write a test that the new constraints actually bite**

Add to `tests/db/test_schema.py` (create if absent):

```python
@requires_db
async def test_zero_contract_multiplier_is_rejected(conn):
    """A zero multiplier silently zeroes option P&L; the DB must refuse it."""
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """INSERT INTO instrument (natural_key, asset_class, symbol,
                                       quote_currency, contract_multiplier)
               VALUES ('x:zero', 'option', 'ZERO', 'USD', 0)"""
        )


@requires_db
async def test_unknown_funding_source_is_rejected(conn):
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "UPDATE fill SET funding_source = 'nonsense' WHERE false OR true"
        )


@requires_db
async def test_return_of_capital_is_an_accepted_cash_kind(conn):
    """Guards the CHECK expansion: without it this raises and Part 2's rule
    table cannot record a return of capital at all."""
    account_id = await create_account(
        conn, name="t", venue="fidelity", account_type="cash"
    )
    await conn.execute(
        """INSERT INTO cash_movement (account_id, occurred_at, kind, amount)
           VALUES ($1, now(), 'return_of_capital', 10)""",
        account_id,
    )
```

- [ ] **Step 5: Run the tests**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/ -v`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add db/migrations/001_a2_ledger_completion.sql db/schema.sql tests/db/test_schema.py
git commit -m "feat(db): A-2 schema -- derived columns, funding source, constraints

Adds trade.fees_realized/open_quantity/open_cost_basis/is_estimated,
fill.funding_source, account.ignore_on_import; CHECK constraints on
contract_multiplier and mark.price; cash kinds 'tax' and 'return_of_capital';
and an updated_at trigger on fill.

Written in both db/schema.sql and migration 001. The equivalence test from the
previous commit asserts the two agree."
```

---

## Task 4: Fee allocation — capitalize entry fees, amortize across closes

`ledger/pnl.py` pro-rates each fill's fee by that allocation's share **of the fill**, so a fill wholly inside a trade contributes 100% of its fee no matter how little of the position has closed. Measured on the Coinbase fixture at 25% closed: reported `realized_pnl = 1360`, correct `1588.75` — a 17% error that persists for as long as the trade stays partly open.

Spec D6 mandates average-cost basis, under which acquisition fees are part of the basis of the units acquired and are recognised as those units are sold.

**Files:**
- Modify: `ledger/pnl.py:35-133`
- Test: `tests/test_pnl.py`

**Interfaces:**
- Consumes: `FillAllocation`, `Fill`, `Direction`, `Side` (unchanged)
- Produces: `TradePnL` gains a field `fees_realized: Decimal`. `fees_total` keeps its meaning: all fees paid on the trade. `realized_pnl` becomes `gross_realized_pnl − fees_realized`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_pnl.py`:

```python
def test_entry_fee_is_amortized_across_closes_not_expensed_at_once():
    """A trade 25% closed must recognise 25% of its entry fee, not all of it.

    The old convention expensed the whole entry fee immediately, which matches
    no accounting convention and self-corrects only when the trade closes flat.
    Asserts on realized_pnl, whose value the bug moves by 228.75.
    """
    entry = _fill(side=Side.BUY, qty="4", price="60000", fee="300")
    exit_ = _fill(side=Side.SELL, qty="1", price="76000", fee="85")
    result = compute_pnl(
        _allocs(entry, "4", exit_, "1"),
        {entry.id: entry, exit_.id: exit_},
        {entry.instrument_id: Decimal(1)},
        Direction.LONG,
    )

    assert result.qty_closed == Decimal("1")
    assert result.fees_total == Decimal("385")          # unchanged meaning
    assert result.fees_realized == Decimal("160")       # 85 exit + 300 * 1/4
    assert result.realized_pnl == result.gross_realized_pnl - result.fees_realized


def test_unamortized_entry_fee_is_carried_in_open_cost_basis():
    """The 225 of entry fee not yet recognised belongs to the 3 open units.

    Per-unit, and divided by the multiplier, because open_cost_basis is
    expressed in price terms while a fee is expressed in currency.
    """
    entry = _fill(side=Side.BUY, qty="4", price="60000", fee="300")
    exit_ = _fill(side=Side.SELL, qty="1", price="76000", fee="85")
    result = compute_pnl(
        _allocs(entry, "4", exit_, "1"),
        {entry.id: entry, exit_.id: exit_},
        {entry.instrument_id: Decimal(1)},
        Direction.LONG,
    )
    assert result.open_quantity == Decimal("3")
    assert result.open_cost_basis == Decimal("60075")   # 60000 + 300/4


def test_option_entry_fee_capitalizes_per_contract_not_per_share():
    """open_cost_basis excludes the multiplier, so a currency fee must be
    divided by (quantity * multiplier) to land in the same units as price.

    Without the multiplier this is 100x wrong for options -- the exact silent
    failure mode that CHECK (contract_multiplier > 0) exists to bound.
    """
    entry = _fill(side=Side.BUY, qty="10", price="0.40", fee="6.60")
    result = compute_pnl(
        _allocs(entry, "10"),
        {entry.id: entry},
        {entry.instrument_id: Decimal(100)},
        Direction.LONG,
    )
    assert result.open_quantity == Decimal("10")
    assert result.open_cost_basis == Decimal("0.406")   # 0.40 + 6.60/(10*100)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_pnl.py -k "amortized or unamortized or per_contract" -v`

Expected: FAIL — `TradePnL` has no attribute `fees_realized`, and `open_cost_basis` returns the fee-free basis.

- [ ] **Step 3: Implement**

In `ledger/pnl.py`, add `fees_realized: Decimal` to `TradePnL` (after `fees_total`). Then split fee accumulation by side and compute the split. Replace the fee line at line ~89:

```python
            # Split by side: an entry fee is part of the basis of the units
            # acquired and is recognised as those units are sold; an exit fee
            # is recognised in full at the close. Pro-rating by the allocation's
            # share of the FILL (the old behaviour) expensed an entry fee
            # entirely on a fill wholly inside a barely-closed trade.
            fee_share = (f.fee * qty / f.quantity) if f.quantity else Decimal(0)
            fees += fee_share
            if f.side is opening_side:
                fees_entry += fee_share
            else:
                fees_exit += fee_share
```

Declare `fees_entry = Decimal(0)`, `fees_exit = Decimal(0)` and `entry_mult = Decimal(0)` alongside `fees` at line ~76, and record the opening leg's multiplier inside the `if f.side is opening_side:` branch — take it from `mult`, which is already resolved for that fill:

```python
                entry_mult = mult   # opening leg's multiplier, for fee capitalization
```

Reading it from the opening fill rather than from `multipliers.values()` matters: a `SPREAD` trade has several instruments and several multipliers, and picking an arbitrary one would be silently wrong the day spreads are implemented. (`SPREAD` raises `NotImplementedError` today, so this is defence against a future change, not a live bug.)

Then replace the return block:

```python
        # Entry fees attributable to closed quantity, plus every exit fee.
        entry_fee_recognised = (
            fees_entry * (qty_closed / qty_opened) if qty_opened else Decimal(0)
        )
        fees_realized_val = _q(fees_exit + entry_fee_recognised)

        # The remainder rides with the open units. open_cost_basis is per-unit and
        # excludes the multiplier, so convert the currency fee into price terms.
        entry_fee_per_unit = Decimal(0)
        if qty_opened and position and entry_mult:
            entry_fee_per_unit = (fees_entry / qty_opened) / entry_mult
        open_cost_basis_val = _q(
            ((basis_total / position) + entry_fee_per_unit) if position else Decimal(0)
        )

        return TradePnL(
            qty_opened=qty_opened,
            qty_closed=qty_closed,
            avg_entry=avg_entry_val,
            avg_exit=avg_exit_val,
            gross_realized_pnl=gross_val,
            fees_total=fees_val,
            fees_realized=fees_realized_val,
            realized_pnl=gross_val - fees_realized_val,
            open_quantity=position,
            open_cost_basis=open_cost_basis_val,
        )
```

Delete the now-superseded `open_cost_basis_val` assignment at line ~121.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_pnl.py -v`

Expected: the three new tests PASS. **Pre-existing tests asserting `realized_pnl == gross_realized_pnl − fees_total` will now FAIL — that is correct and expected.** Restate each one against `fees_realized`. Do not delete them; that identity is load-bearing and must survive in its corrected form.

- [ ] **Step 5: Verify against the Coinbase fixture**

Run: `uv run pytest tests/test_pnl.py tests/test_coinbase.py -q`

Expected: all pass. The known-gaps doc records the corrected figure for a 25%-closed Coinbase trade as `realized_pnl = 1588.75` against a reported `1360`; if a fixture test covers that trade, it should now report the former.

- [ ] **Step 6: Commit**

```bash
git add ledger/pnl.py tests/test_pnl.py
git commit -m "fix(pnl): amortize entry fees across closes per average-cost basis

Each fill's fee was pro-rated by that allocation's share of the FILL, so a
fill wholly inside a trade contributed 100% of its fee however little of the
position had closed. Measured on the Coinbase fixture at 25% closed: reported
1360 against a correct 1588.75, a 17% error persisting for as long as the
trade stays partly open -- which is exactly the state a journal is read in.

Adds fees_realized (exit fees in full plus entry fees x qty_closed/qty_opened)
and restates realized = gross - fees_realized. fees_total keeps meaning all
fees paid on the trade. The unamortized remainder rides in open_cost_basis,
converted into price terms by the contract multiplier -- without which it is
100x wrong for options."
```

---

## Task 5: Persist the derived columns

`open_quantity` and `open_cost_basis` are computed but never written, so `unrealized_pnl()` cannot obtain its inputs without re-running the grouper. `fees_realized` needs the same treatment.

**Files:**
- Modify: `db/trades.py:130` (the trade upsert column list and its parameters)
- Test: `tests/db/test_trades.py`

**Interfaces:**
- Consumes: `TradePnL.fees_realized`, `.open_quantity`, `.open_cost_basis` from Task 4
- Produces: those three values readable from the `trade` table after `regroup_account`

- [ ] **Step 1: Write the failing test**

```python
@requires_db
async def test_regroup_persists_derived_pnl_columns(conn):
    """unrealized_pnl() must be able to read its inputs from the database
    without re-running the grouper. Asserts on values, not on non-NULL --
    a column written as 0 would satisfy a NOT NULL check and still be wrong.
    """
    account_id = await create_account(
        conn, name="t", venue="fidelity", account_type="cash"
    )
    inst = await upsert_instrument(conn, _equity("ACME"))
    await insert_fill(conn, account_id, inst, side="buy", qty="4", price="100", fee="8")
    await insert_fill(conn, account_id, inst, side="sell", qty="1", price="120", fee="2")

    await regroup_account(conn, account_id)

    row = await conn.fetchrow(
        """SELECT open_quantity, open_cost_basis, fees_realized, realized_pnl,
                  gross_realized_pnl
             FROM trade WHERE account_id = $1""",
        account_id,
    )
    assert row["open_quantity"] == Decimal("3")
    assert row["open_cost_basis"] == Decimal("102")        # 100 + 8/4
    assert row["fees_realized"] == Decimal("4")            # 2 exit + 8 * 1/4
    assert row["realized_pnl"] == row["gross_realized_pnl"] - row["fees_realized"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/test_trades.py::test_regroup_persists_derived_pnl_columns -v`

Expected: FAIL — the columns are NULL.

- [ ] **Step 3: Add the three columns to the upsert**

In `db/trades.py`, extend the INSERT column list at line ~130 with `fees_realized, open_quantity, open_cost_basis`, add the matching `$n` placeholders and values (`pnl.fees_realized`, `pnl.open_quantity`, `pnl.open_cost_basis`), and add all three to the `ON CONFLICT ... DO UPDATE SET` clause. They are derived columns and the regroup owns them — matching how `realized_pnl` is already handled.

- [ ] **Step 4: Run the test to verify it passes**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/test_trades.py -v`

Expected: PASS.

- [ ] **Step 5: Confirm regroup stays non-destructive**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/ -q`

Expected: all pass, including the existing tests asserting regroup never writes `notes`, `planned_risk`, `strategy_tag` or `intent`.

- [ ] **Step 6: Commit**

```bash
git add db/trades.py tests/db/test_trades.py
git commit -m "feat(db): persist open_quantity, open_cost_basis and fees_realized

Derived columns owned by the regroup, so unrealized_pnl() can read its inputs
from the database instead of re-running the grouper."
```

---

## Task 6: Propagate `is_estimated` from fill to trade

Spec §4 requires opening-balance trades to be excluded from R-multiple and win-rate statistics. `fill.is_estimated` exists and is set; nothing carries it up to `trade`, so the rule has no representation. Subsystem C cannot be built without it.

**Files:**
- Modify: `db/trades.py` (the trade upsert)
- Test: `tests/db/test_trades.py`

**Interfaces:**
- Produces: `trade.is_estimated` — true when **any** constituent fill is estimated.

- [ ] **Step 1: Write the failing test**

```python
@requires_db
async def test_a_trade_containing_an_estimated_fill_is_itself_estimated(conn):
    """Any estimated fill taints the trade: an opening-balance fill makes the
    whole trade's P&L an estimate, so spec 4 excludes it from R-multiple and
    win-rate. Uses ANY, not ALL -- one estimated leg is enough.
    """
    account_id = await create_account(
        conn, name="t", venue="fidelity", account_type="cash"
    )
    inst = await upsert_instrument(conn, _equity("ACME"))
    await insert_fill(
        conn, account_id, inst, side="buy", qty="5", price="100", is_estimated=True
    )
    await insert_fill(conn, account_id, inst, side="sell", qty="5", price="110")

    await regroup_account(conn, account_id)

    assert await conn.fetchval(
        "SELECT is_estimated FROM trade WHERE account_id = $1", account_id
    ) is True


@requires_db
async def test_a_trade_of_only_exact_fills_is_not_estimated(conn):
    """Negative control: without it the test above passes for a function that
    hardcodes True."""
    account_id = await create_account(
        conn, name="t", venue="fidelity", account_type="cash"
    )
    inst = await upsert_instrument(conn, _equity("ACME"))
    await insert_fill(conn, account_id, inst, side="buy", qty="5", price="100")
    await insert_fill(conn, account_id, inst, side="sell", qty="5", price="110")

    await regroup_account(conn, account_id)

    assert await conn.fetchval(
        "SELECT is_estimated FROM trade WHERE account_id = $1", account_id
    ) is False
```

- [ ] **Step 2: Run them to verify the first fails**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/test_trades.py -k estimated -v`

Expected: the ANY test FAILS (column is `False` by default); the negative control passes trivially.

- [ ] **Step 3: Implement the rollup**

In `db/trades.py`, where each `TradeGroup` is written, compute:

```python
    # Any estimated fill taints the trade -- an opening-balance fill makes the
    # whole trade's P&L an estimate (spec section 4).
    is_estimated = any(
        fills_by_id[a.fill_id].is_estimated for a in group.allocations
    )
```

and add `is_estimated` to the upsert column list, placeholders, values, and `DO UPDATE SET` clause.

- [ ] **Step 4: Run the tests to verify both pass**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest tests/db/test_trades.py -k estimated -v`

Expected: both PASS.

- [ ] **Step 5: Full suite**

Run: `TEST_PG_DSN=$TEST_PG_DSN uv run pytest -q`

Expected: all pass, output pristine.

- [ ] **Step 6: Commit**

```bash
git add db/trades.py tests/db/test_trades.py
git commit -m "feat(db): propagate is_estimated from fill to trade

Any estimated constituent fill makes the trade estimated. Spec section 4
excludes such trades from R-multiple and win-rate statistics; without this
the rule had no representation and subsystem C could not be built."
```

---

## Task 7: Regroup every account after migrating

Existing `realized_pnl` values were computed under the old fee convention. The new figure requires the grouper, so a regroup is a **required** post-migration step — without it one column carries two meanings across rows, which is worse than either convention alone.

**Files:**
- Modify: `cli.py` (the `migrate` command's output)
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
def test_migrate_tells_the_operator_to_regroup(capsys):
    """A migration that changes the meaning of realized_pnl leaves existing rows
    stale. The command must say so -- silence reads as 'nothing more to do'."""
    exit_code = _run_cli(["migrate"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "regroup" in out.lower()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_cli.py::test_migrate_tells_the_operator_to_regroup -v`

Expected: FAIL — no such text in the output.

- [ ] **Step 3: Implement**

In `cmd_migrate`, when `applied` is non-empty, print after the applied list:

```python
        print(
            "\nDerived columns are stale: migration 001 changes how realized_pnl\n"
            "is computed. Run `regroup --account <uuid>` for every account before\n"
            "trusting any P&L figure."
        )
```

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_cli.py -v`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add cli.py tests/test_cli.py
git commit -m "feat(cli): warn that migrating leaves derived P&L stale

Migration 001 changes the meaning of realized_pnl, and existing rows keep the
old convention until regrouped. Saying nothing reads as 'nothing more to do'."
```

---

## Self-review

**Spec coverage.** Against `2026-08-05-a2-ledger-completion-design.md` §4 steps 1–4 and §5–6:

| Spec item | Task |
|---|---|
| `group_fills` quantity-aware exclusion | 1 |
| Schema-equivalence guard (§5) | 2 |
| Six new columns, three constraints, `updated_at` trigger | 3 |
| Fee allocation + `fees_realized` (§6) | 4 |
| Persisted `open_quantity` / `open_cost_basis` | 5 |
| `is_estimated` propagation | 6 |
| "Regroup is a required post-migration step" (§5) | 7 |

Deferred to Part 2 by design: the importer rule table, account routing, sweep classification, DRIP funding source, the blocking policy, the Coinbase audit, and §4's step-7 residual gaps (`upsert_instrument` repaint, self-referential corporate action validation, `content_hash` side test, spinoff-child dedupe test, §9 property test, `positions`, preview duplicate reporting).

**Placeholder scan.** No TBDs. Every code step carries real code. Task 3 step 2 describes edits to `db/schema.sql` in prose rather than a diff — acceptable because the changes are the migration's DDL restated inline, and the equivalence test mechanically verifies the result.

**Type consistency.** `fees_realized` is named identically in `TradePnL` (Task 4), the migration (Task 3), and the upsert (Task 5). `funding_source` and `ignore_on_import` are created in Task 3 and consumed only in Part 2. `open_cost_basis` keeps its existing "per-unit, excluding multiplier" meaning throughout.

**One thing the implementer must not miss:** Task 4 step 4 will break existing tests, deliberately. They are to be restated against `fees_realized`, never deleted.
