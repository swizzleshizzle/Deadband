# Outbound ACAT Transfer (Branch B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Model outbound ACAT transfers so the last blocking verb imports: the position closes at zero realised P&L, the cost basis leaves with the shares, and all eleven real export files commit end to end.

**Architecture:** A new `asset_transfer` table (migration 004) carries the share leg; `cash_movement` gains kind `transfer_out` for the cash leg. Transfers thread through the pure layer as a second input — `group_fills` consumes them as reduce-only closing events, `compute_pnl` walks them at running average cost so realised P&L is untouched by construction — and `regroup_account` persists the result in a new `trade.qty_transferred` column. The importer recognises the verb and writes both legs directly with content-hash dedupe; inbound-shaped rows block the import loudly.

**Tech Stack:** Python 3.10 / asyncpg / pytest (`asyncio_mode = auto`). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-19-acat-transfer-out-design.md` — decisions D1–D7 there govern; this plan cites them by number.

## Global Constraints

- **DB test runs:** every command below that touches `tests/db/` MUST be run as `set -a && . ./.env && set +a && uv run pytest <file> -q` in the FOREGROUND, and you MUST read the summary line. A green run without `TEST_PG_DSN` skips every DB test and proves nothing. Individual DB files take up to ~4 min; let the command block — piped output buffering mid-run is normal and is not a hang. NEVER run the whole suite in one command (it exceeds the 600 s tool ceiling); run the file(s) the task names.
- **Hygiene (public repo, real brokerage data):** never write a real ticker, account number, or dollar/share amount from `imports/` into any tracked file — fixtures use invented values and are described by sign-and-column. Changing a ticker does NOT make a real amount fabricated. `imports/` is gitignored; tests may read it only behind a skip-if-absent guard and must assert counts, never row text.
- **Cite by symbol, never line range**, in comments, docs and commit messages.
- **Money/quantity are `Decimal` end to end**; never float. Amounts stored positive with direction in `kind` (`OUTFLOW_KINDS`), matching `ledger/cash.net_cash`.
- **Schema changes land in BOTH `db/schema.sql` and `db/migrations/004_asset_transfers.sql`** with identical named constraints — `tests/db/test_schema_equivalence.py` is the referee.
- **Spec deviation, decided here:** spec §5 says the cash leg's "amount as signed"; the codebase convention (`CanonicalCash.amount` "always positive — see OUTFLOW_KINDS") wins. `transfer_out` joins `OUTFLOW_KINDS` and the amount is stored positive.
- Branch: `feat/acat-transfer-out` off `main`. Commit after every task.

---

### Task 1: Migration 004 and schema.sql, in lockstep

**Files:**
- Create: `db/migrations/004_asset_transfers.sql`
- Modify: `db/schema.sql` (cash_movement CREATE, trade CREATE, upgrade-path ALTER block, new table)
- Test: `tests/db/test_migrations.py`, `tests/db/test_schema.py`

**Interfaces:**
- Produces: table `asset_transfer(id, account_id, instrument_id, occurred_at, direction, quantity, market_value, venue_ref, content_hash, note, created_at)`; `cash_movement.kind` admits `'transfer_out'` via named constraint `cash_movement_kind_chk`; nullable `trade.qty_transferred NUMERIC`.

- [ ] **Step 1: Write the failing migration test** — append to `tests/db/test_migrations.py` (its namespace-builder helpers are already imported there; mirror `test_migration_003_survives_a_populated_trade_fill`'s disposable-namespace pattern):

```python
async def test_migration_004_survives_populated_rows_and_widens_the_kind_check(pool):
    """Build a pre-004 namespace (baseline + 001..003), seed cash_movement rows
    under the OLD kind constraint, run 004 alone, and prove: existing rows
    survive, 'transfer_out' becomes insertable, a bogus kind still raises, and
    trade grew qty_transferred. Guards the named-constraint conversion: 004
    must drop the auto-named inline CHECK and install cash_movement_kind_chk."""
    ns = "mig004_populated"
    async with pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{ns}" CASCADE')
            await conn.execute(f'CREATE SCHEMA "{ns}"')
            await conn.execute(f'SET search_path TO "{ns}"')
            await conn.execute(BASELINE.read_text())
            for m in ("001_a2_ledger_completion.sql",
                      "002_reject_non_finite_numerics.sql",
                      "003_derived_fills.sql"):
                await conn.execute((DB_DIR / "migrations" / m).read_text())

            acc = await conn.fetchval(
                "INSERT INTO account (name, venue, account_type)"
                " VALUES ('t', 'manual', 'cash') RETURNING id")
            await conn.execute(
                "INSERT INTO cash_movement (account_id, occurred_at, kind, amount)"
                " VALUES ($1, now(), 'deposit', 10), ($1, now(), 'withdrawal', 5)", acc)

            await conn.execute((DB_DIR / "migrations" / "004_asset_transfers.sql").read_text())

            assert await conn.fetchval("SELECT count(*) FROM cash_movement") == 2
            await conn.execute(
                "INSERT INTO cash_movement (account_id, occurred_at, kind, amount)"
                " VALUES ($1, now(), 'transfer_out', 1)", acc)
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO cash_movement (account_id, occurred_at, kind, amount)"
                        " VALUES ($1, now(), 'bogus', 1)", acc)
            assert await conn.fetchval(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_schema = $1 AND table_name = 'trade'"
                " AND column_name = 'qty_transferred'", ns) == 1
            inst = await conn.fetchval(
                "INSERT INTO instrument (natural_key, asset_class, symbol)"
                " VALUES ('equity:TSTX', 'equity', 'TSTX') RETURNING id")
            await conn.execute(
                "INSERT INTO asset_transfer (account_id, instrument_id, occurred_at,"
                " direction, quantity, market_value) VALUES ($1,$2,now(),'out',40,259.2)",
                acc, inst)
            with pytest.raises(asyncpg.CheckViolationError):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO asset_transfer (account_id, instrument_id, occurred_at,"
                        " direction, quantity) VALUES ($1,$2,now(),'in',40)", acc, inst)
        finally:
            await tx.rollback()
```

Also add `"asset_transfer"` to the expected-names set in `tests/db/test_schema.py::test_schema_creates_expected_tables`.

- [ ] **Step 2: Run to verify both fail** — `set -a && . ./.env && set +a && uv run pytest tests/db/test_migrations.py tests/db/test_schema.py -q`. Expected: the new test fails with `FileNotFoundError` on `004_asset_transfers.sql`; the schema test fails on the missing table.

- [ ] **Step 3: Write `db/migrations/004_asset_transfers.sql`:**

```sql
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
```

- [ ] **Step 4: Mirror in `db/schema.sql`:**
  1. In `CREATE TABLE IF NOT EXISTS cash_movement`, DELETE the inline `CHECK (kind IN (...))` clause (leave `kind TEXT NOT NULL`). Fresh databases get the constraint from the ALTER block added next.
  2. Immediately AFTER the cash_movement CREATE + its `cash_content_hash_uniq` index, add the same three `ALTER TABLE cash_movement ...` statements from Step 3, with a comment: `-- Named constraint, attached by ALTER in both this file and migration 004 -- see trade_effective_instrument_fk's comment for why inline would diverge.`
  3. In `CREATE TABLE IF NOT EXISTS trade`, add `qty_transferred      NUMERIC,` beside `qty_closed`.
  4. Next to the existing upgrade-path pair (`ALTER TABLE trade ADD COLUMN IF NOT EXISTS effective_instrument_id ...`), add `ALTER TABLE trade ADD COLUMN IF NOT EXISTS qty_transferred NUMERIC;` and extend that block's comment: an existing database skips the CREATE whole, so the column must be supplied here too.
  5. Add the whole `asset_transfer` CREATE + two indexes from Step 3 after the `account_snapshot` block (it references only account/instrument, so placement is unconstrained; keep the same SQL text verbatim).

- [ ] **Step 5: Run to verify green** — `set -a && . ./.env && set +a && uv run pytest tests/db/test_migrations.py tests/db/test_schema.py tests/db/test_schema_equivalence.py tests/db/test_session_namespace.py -q`. All pass; equivalence proves both files converge; the session-namespace test proves 004 executed this session.

- [ ] **Step 6: Commit** — `git add db/ tests/db/ && git commit -m "feat(db): asset_transfer table, transfer_out cash kind, trade.qty_transferred (migration 004)"`

---

### Task 2: AssetTransfer type and db/transfers.py

**Files:**
- Modify: `ledger/types.py`
- Create: `db/transfers.py`
- Test: create `tests/db/test_transfers.py`

**Interfaces:**
- Produces: `ledger.types.AssetTransfer` frozen dataclass — fields `id: UUID | None`, `account_id: UUID`, `instrument_id: UUID`, `occurred_at: datetime`, `quantity: Decimal` (positive), `market_value: Decimal | None`, `venue_ref: str | None = None`, `content_hash: str | None = None`, `note: str | None = None`.
- Produces: `db.transfers.insert_transfers(conn, transfers: list[AssetTransfer]) -> InsertResult` (reuse `db.fills.InsertResult`; dedupe via `ON CONFLICT DO NOTHING` on the content-hash index, count inserted vs skipped) and `db.transfers.fetch_transfers(conn, account_id: UUID | None = None) -> list[AssetTransfer]` ordered by `occurred_at`.

- [ ] **Step 1: Write failing tests** in `tests/db/test_transfers.py` (module header `pytestmark = requires_db`; use the `conn` fixture and `tests/db/conftest.py`'s `_fill`-style helpers for account/instrument setup):

```python
"""asset_transfer round-trip and dedupe. All values invented."""
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.instruments import upsert_instrument
from db.transfers import fetch_transfers, insert_transfers
from ledger.types import AssetClass, AssetTransfer, Instrument
from tests.conftest import requires_db

pytestmark = requires_db

_T = datetime(2026, 3, 11, 0, 0, tzinfo=UTC)


async def _setup(conn):
    acc = await create_account(conn, name="T", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    return acc, inst


def _transfer(acc, inst, *, hash_=None):
    return AssetTransfer(
        id=uuid4(), account_id=acc, instrument_id=inst, occurred_at=_T,
        quantity=Decimal("40"), market_value=Decimal("259.20"),
        content_hash=hash_,
    )


async def test_insert_and_fetch_round_trip(conn):
    acc, inst = await _setup(conn)
    result = await insert_transfers(conn, [_transfer(acc, inst)])
    assert (result.inserted, result.skipped) == (1, 0)
    got = await fetch_transfers(conn, acc)
    assert len(got) == 1
    assert got[0].quantity == Decimal("40")
    assert got[0].market_value == Decimal("259.20")


async def test_content_hash_dedupes_reimports(conn):
    acc, inst = await _setup(conn)
    await insert_transfers(conn, [_transfer(acc, inst, hash_="h1")])
    result = await insert_transfers(conn, [_transfer(acc, inst, hash_="h1")])
    assert (result.inserted, result.skipped) == (0, 1)
    assert len(await fetch_transfers(conn, acc)) == 1
```

- [ ] **Step 2: Run to verify fail** — `set -a && . ./.env && set +a && uv run pytest tests/db/test_transfers.py -q`. Expected: `ImportError` on `AssetTransfer` / `db.transfers`; add the dataclass first if the import error masks the assertion, then re-run until failures are assertion-shaped or clean import errors of the module under construction only.

- [ ] **Step 3: Implement** — `AssetTransfer` in `ledger/types.py` (beside `Fill`, same frozen/slots style); `db/transfers.py` mirrors `db/fills.py`'s insert/fetch shape: INSERT with explicit column list and `direction` hardcoded `'out'`, `ON CONFLICT DO NOTHING RETURNING id`, count non-None returns; `_to_transfer(record)` builder for fetch.

- [ ] **Step 4: Run to verify green** — same command. Expected: 2 passed.

- [ ] **Step 5: Commit** — `git commit -m "feat(db): AssetTransfer type with insert/fetch and content-hash dedupe"`

---

### Task 3: adjust_transfers in ledger/corporate.py

**Files:**
- Modify: `ledger/corporate.py`
- Test: `tests/test_corporate.py`

**Interfaces:**
- Consumes: `AssetTransfer` (Task 2), `CorporateAction`, `_ordered_actions`.
- Produces: `adjust_transfers(transfers: Sequence[AssetTransfer], actions: Sequence[CorporateAction]) -> list[AssetTransfer]` — pre-ex-date transfers rescale quantity and follow identity remaps exactly as `adjust_fills` does for fills (D7); spinoffs are IGNORED for transfers (documented gap, Task 10).

- [ ] **Step 1: Write failing tests** in `tests/test_corporate.py` (pure — no DB; reuse that module's existing action factories if present, else build `CorporateAction` inline as `tests/db/conftest.py`'s `_split` does):

```python
def _xfer(inst, *, qty="1800", when=datetime(2026, 2, 1, tzinfo=UTC)):
    return AssetTransfer(
        id=uuid4(), account_id=uuid4(), instrument_id=inst, occurred_at=when,
        quantity=Decimal(qty), market_value=None,
    )


def test_pre_ex_transfer_rescales_like_a_pre_ex_fill():
    inst = uuid4()
    action = CorporateAction(
        instrument_id=inst, action_type=ActionType.REVERSE_SPLIT,
        ex_date=date(2026, 3, 2), ratio_numerator=Decimal(1), ratio_denominator=Decimal(6),
    )
    out = adjust_transfers([_xfer(inst)], [action])
    assert out[0].quantity == Decimal(300)


def test_post_ex_transfer_is_untouched():
    inst = uuid4()
    action = CorporateAction(
        instrument_id=inst, action_type=ActionType.REVERSE_SPLIT,
        ex_date=date(2026, 3, 2), ratio_numerator=Decimal(1), ratio_denominator=Decimal(6),
    )
    out = adjust_transfers([_xfer(inst, when=datetime(2026, 4, 1, tzinfo=UTC))], [action])
    assert out[0].quantity == Decimal(1800)


def test_symbol_change_remaps_a_pre_ex_transfer_instrument():
    old, new = uuid4(), uuid4()
    action = CorporateAction(
        instrument_id=old, action_type=ActionType.SYMBOL_CHANGE,
        ex_date=date(2026, 3, 2), ratio_numerator=Decimal(1), ratio_denominator=Decimal(1),
        resulting_instrument_id=new,
    )
    out = adjust_transfers([_xfer(old)], [action])
    assert out[0].instrument_id == new


def test_spinoff_leaves_transfers_untouched_and_mints_nothing():
    inst, child = uuid4(), uuid4()
    action = CorporateAction(
        instrument_id=inst, action_type=ActionType.SPINOFF,
        ex_date=date(2026, 3, 2), ratio_numerator=Decimal(1), ratio_denominator=Decimal(10),
        resulting_instrument_id=child, basis_allocation=Decimal("0.375"),
    )
    out = adjust_transfers([_xfer(inst)], [action])
    assert len(out) == 1 and out[0].quantity == Decimal(1800)
```

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/test_corporate.py -q` (no DSN needed). Expected: ImportError/NameError on `adjust_transfers`, then assertion failures once stubbed.

- [ ] **Step 3: Implement** `adjust_transfers` in `ledger/corporate.py`, directly below `adjust_fills`. Read `adjust_fills` first and mirror its structure: order actions with `_ordered_actions`, apply each to transfers whose `occurred_at.date() < action.ex_date` on the matching `instrument_id` — rescale `quantity * ratio_numerator / ratio_denominator` for ratio actions, follow `resulting_instrument_id` for identity actions, `dataclasses.replace` since the type is frozen. Skip `ActionType.SPINOFF` entirely with a comment: a transfer is an outflow, not a holding — minting child transfers would fabricate outflows of shares never received; the transfer×spinoff interplay is recorded as a gap (spec §8 keeps gap #53's whole family out of scope).

- [ ] **Step 4: Run to verify green** — `uv run pytest tests/test_corporate.py -q`.

- [ ] **Step 5: Commit** — `git commit -m "feat(ledger): corporate-action adjustment for transfers (D7)"`

---

### Task 4: group_fills consumes transfers as reduce-only closing events

**Files:**
- Modify: `ledger/grouping.py`
- Test: `tests/test_grouping.py`

**Interfaces:**
- Consumes: `AssetTransfer` (Task 2).
- Produces: `TransferAllocation` frozen dataclass — `transfer_id: UUID`, `quantity: Decimal`, `occurred_at: datetime`. `TradeGroup` gains field `transfers: tuple[TransferAllocation, ...] = ()`. `TransferError(ValueError)`. New signature `group_fills(fills: list[Fill], transfers: Sequence[AssetTransfer] = ()) -> list[TradeGroup]` — existing callers unchanged.

- [ ] **Step 1: Write failing tests** in `tests/test_grouping.py`, following its existing fill-builder helpers (read them first and reuse; the sketches below assume a `_fill(side, qty, price, at)` local — adapt to the module's real helper names):

```python
def test_transfer_out_closes_the_position_at_zero_crossing_free(...):
    # BUY 40 then transfer 40 out: one trade, CLOSED, closed_at == transfer time,
    # transfers == one TransferAllocation of 30, allocations hold only the BUY.

def test_partial_transfer_leaves_the_trade_open(...):
    # BUY 40, transfer 15: one OPEN trade, transfers carry 10.

def test_transfer_exceeding_position_raises(...):
    # BUY 10, transfer 45 -> TransferError naming the instrument and quantities.

def test_transfer_against_a_short_position_raises(...):
    # SELL 10 (short), transfer 5 -> TransferError: delivering shares out
    # requires a long holding.

def test_transfer_with_no_open_position_raises(...):
    # transfer with zero fills -> TransferError.

def test_same_timestamp_fill_processes_before_transfer(...):
    # BUY 40 and transfer 40 with identical timestamps: the buy opens, the
    # transfer closes -- fills sort before transfers at a tied timestamp.
```

Write these as real tests with concrete values (quantities/prices invented), one behavior each.

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/test_grouping.py -q`.

- [ ] **Step 3: Implement.** In each `(account_id, instrument_id)` bucket, merge fills and that instrument's transfers into one event walk ordered by `(timestamp, kind, id)` where fills sort before transfers on ties (a broker's same-day executions precede its ACAT snapshot). On a transfer event: `position == 0` → raise; `position < 0` → raise; `quantity > position` → raise (never clamp — spec §4); else append a `TransferAllocation` to a group-local list, `position -= quantity`, and if position reaches zero call `flush(closed_at=transfer.occurred_at)`. `flush` packs the local list into `TradeGroup.transfers` and resets it alongside `allocations`. Keep `group_fills(fills)` call-compatible (default `()`).

- [ ] **Step 4: Run to verify green** — `uv run pytest tests/test_grouping.py tests/test_grouping_properties.py -q` (the property module must stay green untouched: default argument preserves every existing call).

- [ ] **Step 5: Commit** — `git commit -m "feat(ledger): transfers close positions in grouping, loudly or not at all"`

---

### Task 5: compute_pnl walks transfers at running average cost

**Files:**
- Modify: `ledger/pnl.py`
- Test: `tests/test_pnl.py`

**Interfaces:**
- Consumes: `TransferAllocation` (Task 4).
- Produces: `compute_pnl(allocations, fills_by_id, multipliers, direction, transfers: Sequence[TransferAllocation] = ()) -> TradePnL`; `TradePnL` gains `qty_transferred: Decimal` (Decimal(0) when no transfers).

- [ ] **Step 1: Write failing tests** in `tests/test_pnl.py` (reuse its existing fill/allocation builders; invented values):

```python
def test_full_transfer_realises_exactly_zero(...):
    # BUY 40 @ 6.17, transfer 40: realized_pnl == 0, gross_realized_pnl == 0,
    # qty_transferred == 40, open_quantity == 0, open_cost_basis == 0,
    # avg_exit is None (transfers are not exits), qty_closed == 0.

def test_partial_transfer_takes_proportional_basis(...):
    # BUY 30 @ 10 (basis 300), transfer 10: open_quantity == 20,
    # open_cost_basis == 200, realized_pnl == 0, qty_transferred == 10.

def test_sell_after_partial_transfer_uses_surviving_basis(...):
    # BUY 40 @ 10, transfer 15, SELL 25 @ 12: realized == 25*(12-10) == 50
    # (fees zero), qty_closed == 20, avg_exit == 12.

def test_entry_fees_of_transferred_quantity_stay_unrecognised(...):
    # BUY 40 @ 10 fee 3, transfer 40: fees_total == 3, fees_realized == 0,
    # realized_pnl == 0 -- the fee share leaves with the basis (D4).
```

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/test_pnl.py -q`.

- [ ] **Step 3: Implement.** Merge transfers into the ordered walk (same tie rule as grouping: fills first at equal timestamps). At a transfer event inside the position loop: `per_unit = basis_total / position`, `basis_total -= per_unit * t.quantity`, `position -= t.quantity`, accumulate `qty_transferred`. Do NOT touch `exit_notional`, `qty_closed`, fee recognition, or realized accumulators — zero realised P&L is a consequence of the code shape, and the identity `realized = gross - fees` (the module docstring's guarantee) is untouched. Add `qty_transferred=_q(...)`-free plain Decimal to the `TradePnL` construction (quantities in this module are not quantized — match `qty_closed`'s treatment).

- [ ] **Step 4: Run to verify green** — `uv run pytest tests/test_pnl.py -q`.

- [ ] **Step 5: Commit** — `git commit -m "feat(ledger): transfers reduce basis at average cost, realising nothing"`

---

### Task 6: regroup_account threads transfers end to end

**Files:**
- Modify: `db/trades.py` (`regroup_account`, `_TRADE_UPSERT_BODY`, the orphan-protection UPDATE that nulls derived columns)
- Test: `tests/db/test_trades.py`

**Interfaces:**
- Consumes: `fetch_transfers` (Task 2), `adjust_transfers` (Task 3), `group_fills(fills, transfers)` (Task 4), `compute_pnl(..., transfers=...)` (Task 5).
- Produces: `trade.qty_transferred` persisted (NULL when the trade has no transfers); a trade fully closed by transfer is `status='closed'` with `realized_pnl = 0` and `closed_at` = the transfer's `occurred_at`.

- [ ] **Step 1: Write failing tests** in `tests/db/test_trades.py` (reuse `tests/db/conftest.py`'s `_fill` and `account_with_1800`-style setup; invented values):

```python
async def test_regroup_closes_a_transferred_position_with_zero_pnl(conn):
    # account holds BUY 30 @ 6 on ZXCO; insert_transfers one transfer of 40;
    # regroup_account; the single trade row has status 'closed',
    # qty_transferred == 40, realized_pnl == 0, open_quantity == 0,
    # closed_at == the transfer's occurred_at, avg_exit IS NULL.

async def test_regroup_pre_split_transfer_is_adjusted_before_grouping(conn):
    # BUY 1800 @ 0.05 before a 1:6 reverse split (ex 2026-03-02, use the
    # conftest _split helper), transfer 1800 BEFORE the ex-date too; regroup:
    # the trade closes with qty_transferred == 400 (both sides adjusted, so
    # quantities reconcile) and realized_pnl == 0.

async def test_regroup_without_transfers_leaves_qty_transferred_null(conn):
    # plain BUY with no transfer: qty_transferred IS NULL after regroup.
```

- [ ] **Step 2: Run to verify fail** — `set -a && . ./.env && set +a && uv run pytest tests/db/test_trades.py -q`.

- [ ] **Step 3: Implement in `regroup_account`:**
  1. After the manual-held fill reduction, fetch `transfers = await fetch_transfers(conn, account_id)`.
  2. Extend the corporate-action fetch to the union of fill AND transfer instrument ids, then `transfers = adjust_transfers(transfers, [a for _id, a in pairs])` right where `adjust_fills` runs (same actions, same moment — D7).
  3. `groups = group_fills(fills, transfers)`.
  4. Per group: `pnl = compute_pnl(g.allocations, by_id, multipliers, g.direction, transfers=g.transfers)`; compute `qty_transferred = pnl.qty_transferred if g.transfers else None`.
  5. Add `qty_transferred` to `_TRADE_UPSERT_BODY`'s column list, VALUES placeholders (renumber — it becomes $21 beside is_estimated), and `DO UPDATE SET`; pass it in both fetchval calls.
  6. In the orphan-protection UPDATE that nulls `qty_opened = NULL, qty_closed = NULL, ...`, add `qty_transferred = NULL`.
  A trade closed purely by transfer still has fill allocations (its opens), so opening-identity logic is untouched. Manual trades: transfers never participate in manual grouping (no entry point exists); auto-pass only.

- [ ] **Step 4: Run to verify green** — `set -a && . ./.env && set +a && uv run pytest tests/db/test_trades.py tests/db/test_positions.py -q` (positions read what regroup wrote; a transfer-closed trade must vanish from open positions).

- [ ] **Step 5: Commit** — `git commit -m "feat(db): regroup persists transfer-closed trades with qty_transferred"`

---

### Task 7: Importer recognises the verb; inbound shapes block

**Files:**
- Modify: `importers/base.py` (`OUTFLOW_KINDS`, new `CanonicalTransfer`, `ImportBatch.transfers`), `importers/fidelity.py` (`Outcome.TRANSFER`, rule, shape handling)
- Test: `tests/test_fidelity.py`, `tests/test_importer_base.py`

**Interfaces:**
- Produces: `importers.base.CanonicalTransfer` — `instrument: Instrument`, `occurred_at: datetime`, `quantity: Decimal` (positive), `market_value: Decimal | None`, `venue_ref: str | None = None`, `external_ref: str | None = None`, `note: str | None = None`. `ImportBatch` gains `transfers: tuple[CanonicalTransfer, ...] = ()`. `OUTFLOW_KINDS` gains `"transfer_out"`. `Outcome.TRANSFER` in `importers/fidelity.py` with one `Rule("acat_transfer", "TRANSFER OF ASSETS", Outcome.TRANSFER)`.

- [ ] **Step 1: Write failing tests** in `tests/test_fidelity.py`, using its existing row-builder/fixture idioms (read how neighbouring rule tests fabricate rows; all values invented, shapes match spec §1's sign-and-column table):

```python
def test_acat_share_delivery_becomes_a_transfer(...):
    # Row: action 'TRANSFER OF ASSETS ACAT DELIVER FAKECO COM (ZXCO) (Cash)',
    # symbol ZXCO, quantity -30, amount -259.20.
    # parse() -> batch.transfers has one CanonicalTransfer: quantity 30
    # (positive), market_value Decimal('259.20'), instrument symbol ZXCO;
    # batch.blocking == () and batch.fills/cash gain nothing from this row.

def test_acat_cash_residual_becomes_transfer_out_cash(...):
    # Row: action 'TRANSFER OF ASSETS ACAT DELIVER (Cash)', empty symbol,
    # quantity 0, amount -114.37.
    # parse() -> one CanonicalCash kind 'transfer_out', amount positive
    # Decimal('114.37'); batch.transfers empty; blocking empty.

def test_inbound_shaped_acat_blocks_the_import(...):
    # Row with positive quantity (or positive amount): parse() -> no transfer,
    # no cash; batch.blocking carries one (ref, reason) whose reason names
    # 'TRANSFER OF ASSETS' and says inbound transfers are undesigned.

def test_transfer_out_is_an_outflow_kind():
    assert "transfer_out" in OUTFLOW_KINDS   # in tests/test_importer_base.py
```

- [ ] **Step 2: Run to verify fail** — `uv run pytest tests/test_fidelity.py tests/test_importer_base.py -q`.

- [ ] **Step 3: Implement.** `Outcome.TRANSFER` with a docstring in the enum's established voice (recognised; produces an asset_transfer write, the one outcome that writes to neither `fills` nor `cash` alone). Place `Rule("acat_transfer", "TRANSFER OF ASSETS", Outcome.TRANSFER)` in `RULES` where verb-prefix ordering demands (run `test_every_rule_is_reachable` to confirm no shadowing). In `parse()`'s outcome dispatch, handle `Outcome.TRANSFER` by shape: symbol present AND quantity < 0 → `CanonicalTransfer(quantity=abs(qty), market_value=abs(amount) if amount else None, ...)` building `Instrument` the way FILL rows do; symbol empty AND quantity == 0 AND amount < 0 → `CanonicalCash(kind="transfer_out", amount=abs(amount), ...)`; ANY other shape → append `(external_ref, reason)` to blocking, reason: `"TRANSFER OF ASSETS row is not an outbound delivery -- inbound transfers are undesigned (asset arrives with basis this ledger has no source for); refusing the file"` (D6).

- [ ] **Step 4: Run to verify green** — `uv run pytest tests/test_fidelity.py tests/test_importer_base.py tests/test_fidelity_real_shape.py tests/test_fidelity_history.py -q`. NOTE: `test_every_rule_is_exercised_by_a_fixture_row` (real-shape module) will fail until a synthetic ACAT row is added to its fixture table — add one, shaped by sign-and-column with invented values, in the same style as its neighbours.

- [ ] **Step 5: Commit** — `git commit -m "feat(importers): recognise outbound ACAT transfers; inbound shapes refuse the file"`

---

### Task 8: commit_batch writes transfers; cmd_import reports them

**Files:**
- Modify: `db/importing.py` (`CommitResult`, `commit_batch`, `_transfer_dedupe_hashes` beside `_cash_dedupe_hashes`), `cli.py` (`cmd_import`'s commit report)
- Test: `tests/db/test_importing.py`, `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `CanonicalTransfer`/`ImportBatch.transfers` (Task 7), `insert_transfers` (Task 2).
- Produces: `CommitResult` gains `transfers_inserted: int = 0` and `transfers_skipped: int = 0`; `commit_batch` upserts each transfer's instrument, hashes with the same `content_hash` normalisation family as cash (kind `"transfer_out"`, include occurred_at/symbol/quantity/market_value), inserts via `insert_transfers`; `cmd_import` prints a transfers line beside its fills/cash counts.

- [ ] **Step 1: Write failing tests:**

```python
# tests/db/test_importing.py -- follow its existing commit_batch test setup
async def test_commit_batch_writes_transfers_and_dedupes_on_reimport(conn):
    # batch with one CanonicalTransfer (invented values): first commit ->
    # transfers_inserted == 1; identical second commit -> transfers_inserted
    # == 0, transfers_skipped == 1; asset_transfer holds exactly one row,
    # instrument upserted by symbol.

# tests/db/test_cli.py -- follow an existing cmd_import invocation test
async def test_cmd_import_reports_the_transfer_count(...):
    # drive cmd_import over a synthetic file containing the two ACAT row
    # shapes (invented values); output contains a 'transfers' line with count 1.
```

- [ ] **Step 2: Run to verify fail** — `set -a && . ./.env && set +a && uv run pytest tests/db/test_importing.py -q` (then the cli file in Step 4; keep runs per-file).

- [ ] **Step 3: Implement.** `_transfer_dedupe_hashes` mirrors `_cash_dedupe_hashes` including the occurrence-index tie-break (two identical same-day transfers in one batch must not collapse). In `commit_batch`, after the cash loop: upsert instrument, build `AssetTransfer` rows, `insert_transfers`, extend `CommitResult`. In `cmd_import`, find where `CommitResult.cash_inserted` is rendered and add the transfers line in the same format.

- [ ] **Step 4: Run to verify green** — `set -a && . ./.env && set +a && uv run pytest tests/db/test_importing.py -q` then `... uv run pytest tests/db/test_cli.py -q` (separate foreground runs; test_cli is the slowest file).

- [ ] **Step 5: Commit** — `git commit -m "feat(import): commit ACAT transfers with dedupe and report the count"`

---

### Task 9: `deadband transfers list`

**Files:**
- Modify: `cli.py`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `fetch_transfers` (Task 2).
- Produces: `cmd_transfers` — `deadband transfers list [--account <uuid>]` printing occurred-at date, account name, symbol, quantity, market value, note; exit 0; "no transfers" message when empty.

- [ ] **Step 1: Write failing test** in `tests/db/test_cli.py` (same `_FakePool` monkeypatch pattern as `cmd_trades` tests): seed one transfer via `insert_transfers`, run `cli.cmd_transfers(argparse.Namespace(account=None))`, assert the symbol and quantity appear in captured output and rc == 0; a second test asserts the empty-account message.

- [ ] **Step 2: Run to verify fail** — `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -q` (target the two new tests by full node id to keep the run short while iterating, but finish with the whole file).

- [ ] **Step 3: Implement** `cmd_transfers` + subparser wiring, following `cmd_trades`'s structure (pool acquire, optional account filter, aligned column print; join instrument symbol inside `fetch_transfers`'s SQL or resolve per-row — pick whichever `cmd_trades` does for symbols and stay consistent).

- [ ] **Step 4: Run to verify green** — full `tests/db/test_cli.py` foreground run.

- [ ] **Step 5: Commit** — `git commit -m "feat(cli): transfers list"`

---

### Task 10: Acceptance against real files, gap ledger, docs

**Files:**
- Create: `tests/test_fidelity_real_files.py`
- Modify: `docs/known-gaps.md`, `README.md` (command inventory section, if it lists verbs)
- Test: the new file itself

**Interfaces:**
- Consumes: everything above.
- Produces: the spec §7 acceptance criterion as a permanent, hygiene-safe test.

- [ ] **Step 1: Write the acceptance test** (it should PASS immediately if Tasks 1–9 are correct — its failure is the signal Task 7 missed a shape; this is the one test in this plan written after its implementation, because it verifies integration, not new behavior):

```python
"""Every real export parses with zero blocking rows (spec acceptance, gap #31).

Reads imports/ (gitignored; present only on the owner's machines) behind a
skip guard. Asserts COUNTS ONLY -- a failure message must never carry row
text, amounts, or account refs from the real files."""
import pathlib

import pytest

from importers.fidelity import FidelityImporter

IMPORTS = pathlib.Path(__file__).resolve().parents[1] / "imports"

pytestmark = pytest.mark.skipif(
    not IMPORTS.exists(), reason="imports/ not present; real-file acceptance is owner-local"
)


def test_every_real_export_parses_with_zero_blocking_rows():
    files = sorted(IMPORTS.glob("*.csv"))
    assert files, "imports/ exists but is empty"
    blocked = {}
    for path in files:
        batch = FidelityImporter().parse(path.read_bytes())
        if batch.blocking:
            blocked[path.name] = len(batch.blocking)
    assert blocked == {}, f"files with blocking rows (name: count only): {blocked}"
```

Adapt the `parse(...)` call to the real signature — read how `cmd_import` invokes it (bytes vs text vs path) and mirror that exactly; do not guess.

- [ ] **Step 2: Run it** — `uv run pytest tests/test_fidelity_real_files.py -q`. Expected: PASS with all files clean. If any file still blocks, the reasons name the missed shape — fix in Task 7's code with a new unit test first, then re-run.

- [ ] **Step 3: End-to-end import of the previously blocked file.** Manual acceptance (not a test): with `.env` loaded, run the real CLI import of the blocked account's 2024 history file with `--dry-run`/preview if `cmd_import` offers one, else against the TEST database, and confirm exit 0 and a transfers count of 2 legs (1 transfer + 1 cash). Do not paste row contents anywhere; report counts only.

- [ ] **Step 4: Update `docs/known-gaps.md`:** strike gap #31's remainder (cite this branch), and add new gaps in the table's established voice: (a) transfer×spinoff interplay is undesigned — `adjust_transfers` skips spinoffs while `adjust_fills` mints children per-fill regardless of later outflows (adjacent to gap #53); (b) `asset_transfer.market_value` is stored but consumed by nothing; (c) manual trades and transfers have no interaction — transfers participate only in the auto pass; (d) equity series drop at transfer date reads as a loss at a glance (spec §9). Re-verify every symbol named in edited entries still exists before committing (gap-citation staleness memory).

- [ ] **Step 5: Full-suite verification, then commit** — run `tests/db` as ONE foreground command (`set -a && . ./.env && set +a && uv run pytest tests/db -q`, ~9 min, within the ceiling — verified 2026-08-19) and the pure suite as another (`uv run pytest tests --ignore=tests/db -q`). Read both summary lines; expect zero failures and zero unexplained skips (the real-files test skips only where imports/ is absent). `git commit -m "docs+test: real-file acceptance for branch B; close gap #31"`

---

## Final integration

- [ ] Push `feat/acat-transfer-out`, open a PR against `main` describing: the two-leg model, D1–D7 with the sign-convention deviation from spec §5 noted, the acceptance result (all files parse clean; counts only), and the new gaps recorded.
- [ ] Request code review per house practice before merge.

