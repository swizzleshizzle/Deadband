# Outbound ACAT transfers: the last blocking verb

**Status:** design, approved 2026-08-19. Spec review pending.
**Scope:** branch B — the counterpart deliberately excluded from
`2026-08-18-importer-blocking-verbs-design.md` §8. Lands before UI milestone 1
(sequencing decision, 2026-08-19), after the #15 fixture fix (this branch is
migration `004`, and #15 is the mechanism that would silently disarm its tests).
**Gap closed:** #31's final remainder (`docs/known-gaps.md`).

## 1. What the rows are

Two rows, same date, in one account's file — the only rows still blocking any of
the eleven real history exports, verified by running `FidelityImporter.parse()`
over all of them. Described by sign and column, never by value (public repo, real
data):

| Row | Symbol | Quantity | Amount ($) | What it is |
|---|---|---|---|---|
| `TRANSFER OF ASSETS ACAT DELIVER <security> (Cash)` | present | negative | negative | shares delivered out |
| `TRANSFER OF ASSETS ACAT DELIVER (Cash)` | empty | zero | negative | residual cash delivered out |

No tracked account's file contains a matching receive — the assets left to a
destination outside this ledger. That makes branch B an outbound-only design by
observation, not assumption.

**The settled accounting (decided before this spec, and load-bearing):** the
position closes at **zero realised P&L**, because the cost basis leaves with the
shares. The row's `Amount ($)` is a **market value**, not a transaction price —
booking the row as a sale would fabricate P&L that never happened.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | New `asset_transfer` table for the share leg; `cash_movement` gains kind `transfer_out` for the cash leg. | Chosen over one generic two-leg transfer table (would make `db/cash.account_cash` union two tables, splitting the single source of cash truth) and over a synthetic `fill` with a computed basis price (fills are broker-reported ground truth; a fill that never happened, priced at a number the broker never printed, violates that). Verified beforehand: `derived_fill` cannot carry it (`corporate_action_id` NOT NULL — a transfer is not a corporate action), and `cash_movement.kind`'s CHECK admits no transfer today. |
| D2 | `direction` is CHECK-constrained to `'out'` alone. | Transfer-in is undesigned: an inbound ACAT arrives with basis this ledger has no source for. The observed data contains no inbound row. Widening the CHECK is a one-line migration on the day data demands it; guessing basis is not. |
| D3 | A transfer-out closes position quantity **at average cost at that moment**, contributing exactly zero realised P&L. | This is the settled accounting made mechanical: exit-at-basis realises nothing, and the basis leaves with the shares. |
| D4 | `avg_exit`, `gross_realized_pnl` and all exit statistics remain **sells-only**; transfer-closed quantity is tracked in a new nullable `trade.qty_transferred`. | An "exit" at a computed basis price is not an exit decision; folding it into `avg_exit` would quietly corrupt every statistic built on it. |
| D5 | The importer **writes transfers directly** (with content-hash dedupe), unlike corporate actions, which remain proposals. | The proposal ceremony exists for identity-changing, account-independent facts needing judgment. A transfer row is account-scoped and mechanical, exactly like the fills and cash rows the importer already writes directly. |
| D6 | An inbound-shaped ACAT row (positive quantity or positive amount) **blocks the whole import**, exit 2, same as today. | The safe failure the importer already has. Direction `'in'` is undesigned (D2); silently guessing would be worse than refusing. |
| D7 | Transfers pass through the **same corporate-action adjustment as fills** before grouping. | A transfer that predates a later split must rescale identically or held quantities stop reconciling. Raw rows are never rewritten (design D1 of the ledger spec); adjustment is read-time, mirroring `adjust_fills`. |

## 3. Migration 004 and schema.sql

Both files change in lockstep (`tests/db/test_schema_equivalence.py` is the
referee), following the repo's established patterns:

- **`asset_transfer`** — new table:

  ```sql
  CREATE TABLE IF NOT EXISTS asset_transfer (
      id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      account_id      UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
      instrument_id   UUID NOT NULL REFERENCES instrument(id),
      occurred_at     TIMESTAMPTZ NOT NULL,
      direction       TEXT NOT NULL CHECK (direction IN ('out')),
      quantity        NUMERIC NOT NULL
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
  ```

- **`cash_movement.kind` CHECK gains `'transfer_out'`.** The existing CHECK is
  inline and auto-named, so both files must convert it to a **named constraint
  dropped and re-added via ALTER** (`cash_movement_kind_chk`), the
  `trade_effective_instrument_fk` pattern — otherwise `schema.sql` and the
  migration disagree on the constraint name and the equivalence test fails.

- **`trade.qty_transferred`** — new nullable NUMERIC. Because an existing
  database skips `CREATE TABLE IF NOT EXISTS trade` whole, `schema.sql` needs the
  UPGRADE-PATH `ALTER TABLE trade ADD COLUMN IF NOT EXISTS qty_transferred NUMERIC;`
  placed before the first statement that names the column — the exact pattern
  (and trap) documented above `trade_account_status_idx` in `schema.sql`.

**This is migration `004` — the first migration after the #15 fix, by design.**
Its tests are the first that the fixed fixture protects from silently degrading
to no-ops.

## 4. Ledger semantics

`ledger/grouping.py` and `ledger/pnl.py` stay pure; transfers are a new input,
not a side channel.

- **`group_fills` gains a transfers parameter.** Within an instrument's event
  stream, a transfer-out at time T reduces the open position by its (adjusted)
  quantity at the average cost prevailing at T. Realised P&L contribution is zero
  by construction (D3); remaining basis scales down proportionally — the basis
  leaves with the shares.
- **Adjustment first (D7):** transfers run through the same corporate-action
  adjustment pipeline as fills before grouping, keyed by `occurred_at` against
  ex-dates, exactly as `adjust_fills` treats `executed_at`.
- **Trade effects:** the allocation reduces open quantity; a trade reaching zero
  this way is `closed` with `closed_at` = the transfer's `occurred_at`. Closing
  quantity is recorded in `qty_transferred`, not `qty_closed`'s exit statistics
  (D4). `realized_pnl`, `avg_exit`, `fees_*` are untouched by the transfer.
- **Over-transfer is a loud error:** a transfer-out exceeding the quantity held
  at its timestamp raises a grouping error, surfaced the way existing integrity
  violations are — never clamped, never partially applied.
- **`regroup_account`** persists the new column; `open_positions` and the P&L
  read paths need no change beyond serving it.

## 5. Importer

Two new `Rule`s on the `TRANSFER OF ASSETS ACAT` verb prefix:

| Shape | Outcome |
|---|---|
| symbol present, quantity negative | `asset_transfer` row: direction `out`, quantity = abs(quantity), `market_value` = abs(amount), `occurred_at` from the run date |
| symbol empty, quantity zero, amount negative | `cash_movement` row: kind `transfer_out`, amount as signed |
| anything else under the prefix (positive quantity, positive amount) | **blocks the import, exit 2** (D6) |

Both write paths carry `content_hash` for idempotent re-import, computed the same
way as the existing fill/cash hashes. The import report gains a transfers count
line. History-dialect routing is unchanged: these files carry no account column
and route via `import --account <uuid>`, as established on the corporate-actions
branch.

## 6. CLI

One addition: `deadband transfers list [--account <uuid>]` — occurred-at,
account, symbol, quantity, market value, note. It exists for post-import
verification and because a stored row type with no read path is invisible.
Nothing else: no add/remove verbs (the importer is the only writer this branch
needs), no UI work (the API spec's typed event lists already have room for a
`transfer` event type).

## 7. Testing

- **Pure layer:** grouping with transfers — the zero-realised-P&L invariant, basis
  conservation (basis before = basis after + basis transferred), transfer closing
  a position entirely vs partially, the split-then-transfer adjustment interplay
  (a pre-split transfer rescales like a pre-split fill), and the over-transfer
  error. Property tests extend `tests/test_grouping_properties.py`'s style.
- **DB layer:** migration `004` exercised against **populated** rows in a
  disposable pre-migration namespace — the #15 fix is what makes this test mean
  something on every run, not only the first. Schema equivalence covers the named
  CHECK conversion.
- **Importer:** fixtures shaped by sign-and-column from §1, values invented,
  hygiene-checked against `imports/` at runtime — never real amounts (the
  standing lesson: changing the ticker does not make a real amount fabricated).
  Cases: both row shapes, dedupe on re-import, and the inbound-shaped row
  blocking with exit 2.
- **Acceptance criterion:** `FidelityImporter.parse()` over all eleven real
  files reports **zero blocking rows**, and the previously-blocked account's 2024
  file commits end to end — closing the year-shaped hole that motivated
  sequencing this branch before UI.
- DB runs use `set -a && . ./.env && set +a && uv run pytest <file>` in the
  foreground, and the summary line is read — a green run without `TEST_PG_DSN`
  skips every DB test and proves nothing.

## 8. Out of scope, named

- Transfer-in (D2) and any basis-on-arrival design.
- Linking internal transfers between two tracked accounts — the data has none.
- UI rendering of transfers (milestone 1 consumes them only via positions/trades;
  a `transfer` activity type slots into the API's typed event lists when needed).
- Gap #53 (`_long_holdings_as_of` raw-vs-adjusted) — adjacent, pre-existing,
  untouched.

## 9. Gaps this design creates

| Gap | Why accepted |
|---|---|
| `market_value` is stored but consumed by nothing. | It is the one number the broker printed; discarding it would be worse. First consumer is likely the trade-detail timeline. |
| `direction` admits only `'out'`, so the table name over-promises. | Honest narrowness (D2). Widening is one migration; a wrong inbound-basis guess is a ledger corruption. |
| An account's equity series drops by the transferred market value at the transfer date, which can read as a loss at a glance. | It is not a loss and the ledger says so (zero realised P&L). Presentation is a UI concern; the data is correct. |
| `transfers list` has no filters beyond account. | One account has exactly one transfer event today. YAGNI. |

