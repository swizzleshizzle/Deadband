# Known gaps carried out of A-1

Everything here was found during A-1's implementation, adjudicated deliberately, and
deferred rather than forgotten. Each entry says what is wrong, whether it is reachable
today, and when it must be fixed.

Ordered by when they must be addressed, not by severity.

---

## Must land before subsystem C

### ~~Fee allocation on partially-closed trades is wrong~~ — FIXED 2026-08-06

**Fixed in A-2 part 1**, pulled forward from "before subsystem C" because it needed a
schema migration and was cheapest while the database still held disposable data. The
description below is retained because the reasoning still explains the convention.

Verified against the case recorded here: `gross 1750`, `fees_total 390`,
`fees_realized 161.25`, `realized 1588.75` (was 1360), `open_cost_basis 61305` — all four
figures reproduced exactly.

> [!warning] The fix prescribed below was direction-blind, and that caused a second bug
> "Fold the residual into `open_cost_basis`" is correct only for **long** trades. For a
> short, `open_cost_basis` accumulates *sale proceeds*, and a fee **reduces** net
> proceeds — so the capitalised entry fee must be **subtracted**, not added. Implemented
> literally, it produced an error of twice the unamortised fee with inverted sign on
> every open short (191 of 200 property-check cases), made worse by `unrealized_pnl`
> computing `open_cost_basis − mark_price` for shorts.
>
> Caught in review, fixed, and now guarded permanently by a conservation property test
> parametrised over both directions:
> `realized_pnl + unrealized_pnl(mark) == gross_at_mark − fees_total`.
> That test was verified to fail when the bug is reintroduced.
>
> This is the same shape as the recurring A-1 defect named at the bottom of this file:
> **an invariant applied correctly in one place and not in its twin.** It is worth noting
> that the twin here was not a second code path but a second *direction*.

The original description follows.

`ledger/pnl.py` pro-rates each fill's fee by that allocation's share **of the fill**, so a
fill wholly inside a trade contributes 100% of its fee no matter how little of the
position has closed.

Measured on the Coinbase fixture — a trade 25% closed:

```
reported : realized_pnl = 1360
correct  : realized_pnl = 1588.75      (basis 61305/unit, removed 15326.25, exit fee 85)
error    : 228.75, i.e. 17% of the reported figure
```

Spec D6 mandates average-cost basis, under which acquisition fees are part of the basis
of the units acquired and are recognised as those units are sold. The current code does
neither — it does not capitalise the entry fee into `open_cost_basis` (computed purely
from price) and does not amortise it across closes. It expenses all of it immediately,
which matches no accounting convention.

**Self-corrects when the trade closes flat**, so the error is confined to the
partially-closed window — but that window is unbounded. A long-term hold trimmed once
stays wrong forever, and it is exactly the state a journal is read in.

**Fix:** keep `fees_total` meaning "all fees paid on this trade" (what the column name
says and what a journal should display). Net only the fee attributable to closed quantity
into `realized_pnl`: exit fees in full, plus entry fees × (`qty_closed` / `qty_opened`).
Fold the residual into `open_cost_basis`. This breaks the currently-tested identity
`realized_pnl == gross_realized_pnl − fees_total`, so add a `fees_realized` column and
restate it as `realized = gross − fees_realized`. Needs a schema migration.

**Why before C:** C's expectancy and R-multiple denominators are built on `realized_pnl`.
Changing the convention afterwards means recomputing history.

**Post-fix note.** Existing rows keep the old convention until regrouped — the migration
adds the columns but cannot recompute them, since the new figure requires the grouper.
Run a regroup for every account after migrating, or one column carries two meanings
across rows.

---

## Must land before A-2's manual-grouping UI — sequence it first

### `group_fills` needs quantity-aware exclusion

`db/trades.py` excludes a manual trade's fills from the auto pass **whole**, but a
zero-crossing fill may be only *partly* that trade's. The remainder is then never
regrouped and is reaped:

```
SELL 1 @100, BUY 5 @90   ->  closed short 1, open long 4
hand-mark the closed trade manual, regroup:
  fill qty=5  allocated=1     *** the open long of 4 vanishes permanently
```

Unreachable today — the only writer of `grouping_mode='manual'` is the protection step,
which drops its allocations first — and guarded by a `NotImplementedError` in
`regroup_account` that was verified load-bearing and not over-broad.

**Sequence it as A-2's first task, not alongside the UI.** The guard converts the failure
into a hard error for the *entire* regroup, so the day the UI creates the first partial
allocation, imports stop working for that account until the pure-layer fix lands.

**Fix:** exclude only the quantity a manual trade actually holds, rather than the fill id.

---

## Found by the first real Fidelity export (2026-08-05)

A genuine Fidelity account-activity export was run through the preview path. The large
majority of its rows failed to map, and **every monetary value parsed as zero**. Details
below; the header defect is fixed, the rest are open.

> [!note] Deliberately non-specific
> Findings from real exports are recorded as shapes, never as specimens. No tickers,
> position details, account counts or amounts appear here — this repository is public.
> Reproduction cases belong in `docs/ops/`, which is not version-controlled.

**Fixed already:** real exports suffix money columns with a currency parenthetical
(`Price ($)`, and — per the export's own disclaimer text — sometimes `Fees($)` with no
space). The importer looked up bare `price`/`commission`/`fees`/`amount`, missed every
one, and `_decimal(None)`'s `Decimal("0")` silently replaced them. No warning: dates,
quantities, symbols and option terms all survived, so the output looked entirely
plausible while every price, fee and cash amount was zero. `_normalize_field` now strips
the qualifier structurally rather than aliasing observed spellings.

That defect is worth remembering as a *class*, not an incident: **a missing column is
indistinguishable from a zero value** anywhere `_decimal` is fed a `.get()`. Coinbase
has the same shape. The general fix is a parse-level guard that a fill-shaped row
(quantity *and* price columns present) resolving to a zero price is reported, not
accepted.

| # | Gap | Why it matters |
|---|---|---|
| R1 | `REINVESTMENT` rows are dropped — the large majority of the file's fill-shaped rows | These are genuine acquisitions. **Decided 2026-08-05:** money-market sweep rows (priced at exactly `1.00`) are cash, never fills — importing them would invent a position in a cash sweep. Real-security dividend reinvestment becomes a fill with real basis, tagged `funding_source='reinvestment'`, so `contributed_capital` can exclude it while `cost_basis` stays tax-correct. Zero-basis DRIP was considered and rejected: it overstates every later gain and contradicts the 1099-B. |
| R2 | The multi-account warning is blind to accounts whose rows are *all* unmapped | It under-reported the account count, missing a retirement-plan account whose rows are entirely unhandled actions. A safety check that cannot see the account it most needed to flag. |
| R3 | `external_ref` receives the account *nickname*, not an identifier | Real exports carry both an `Account` (nickname) and a separate `Account Number` column. The fixture had only the latter, under the former's name. |
| R4 | `_CASH_ACTIONS` covers 4 actions; the real vocabulary is far wider and *compound* | Unhandled: `CONTRIBUTIONS`, `INVESTMENT GAIN/LOSS`, `RECORDKEEPING FEE`, `FOREIGN TAX PAID`, `FEE CHARGED`, `RETURN OF CAPITAL`, `REVENUE CREDIT`, `DIVIDENDS`, `EXCHANGED TO`. Action text is action + security name + ticker + settlement type concatenated into one field, so exact-match mapping cannot work — the leading verb phrase is the only reliable signal. |
| R5 | Activity & Orders exports cap at **90 days** | Bulk history needs a different source — per-account statement exports, annual Realized Gain/Loss files (which carry cost basis directly), or the OFX/Direct Connect feed. "Repeated 90-day CSVs" and "one historical bulk load" are different import designs; settle this before A-2 fixes its model. |

**Confirmed working against real data**, and worth not re-litigating: UTF-8 BOM stripped;
blank preamble lines skipped; header located case-insensitively; the trailing disclaimer
block falls out as unmapped rows with warnings exactly as `_locate_header`'s docstring
claimed (that was the untested half of the twin, and it holds); and a real option
contract parsed correctly into underlying, expiry, right and strike with the ×100
multiplier applied.

---

## Carried into A-2 part 2 and beyond

Recorded here rather than only in an execution ledger, because ledgers are scratch and
this file is the project's memory.

- **`OUTFLOW_KINDS` needs `tax`.** `importers/base.py` lists `{"withdrawal", "fee"}`. The
  schema now accepts `tax` and `return_of_capital`, but nothing emits them until part 2's
  rule table. A `tax` movement not registered as an outflow counts as an *inflow* in any
  net-cash calculation — silently, and in the wrong direction.
- **Subsystem C must not filter win-rate eligibility on `is_estimated = FALSE` alone.**
  A protected (orphaned) trade carries `FALSE` because the column is `NOT NULL`, not
  because its P&L is exact. C needs `realized_pnl IS NOT NULL` as well, or protected
  trades land in the denominator.
- **Column ordinal positions differ between a fresh and a migrated database.** New
  columns land mid-table in `schema.sql` and appended in a migrated one. Nothing uses
  positional access today and the equivalence guard sorts by name, so there is no live
  bug — but Postgres cannot reorder columns, so this is permanent and grows with every
  migration. Any future `INSERT … SELECT *`, `COPY`, or positional row access will behave
  differently on a migrated database than on a fresh one. Do not attempt to "fix" the
  ordering; just never rely on it.
- **The `fill.updated_at` trigger has no behavioural test.** The equivalence guard proves
  both schemas *define* it; nothing proves it *fires*, and no application code updates a
  fill yet. When a test is written, note that the obvious version is vacuous: `now()` is
  `transaction_timestamp()`, so within one transaction `updated_at` after an UPDATE is
  byte-identical to the `DEFAULT now()` set at INSERT. The test must set the column
  explicitly and assert the trigger overrides it, or commit across transactions.
- **The equivalence guard compares CHECK *clauses* but not constraint *names*.** A
  name-only divergence would pass, and would then make a future migration's
  `DROP CONSTRAINT IF EXISTS` silently no-op on one of the two shapes. Adding
  `constraint_name` naively fails, because `information_schema.check_constraints` includes
  Postgres's OID-derived NOT NULL pseudo-constraints whose names differ per database;
  filter those out first.

---

## Fix in A-2

| # | Gap | Why it matters |
|---|---|---|
| 1 | `upsert_instrument` repaints only `symbol`; a wrong `contract_multiplier`, `strike` or `expiry` stored on first insert is never corrected | Unreachable in A-1 (only the Fidelity importer mints option rows, always ×100). Becomes silent, permanent, 100×-wrong option P&L the moment a manual-entry form can create an instrument first. |
| 2 | No `CHECK (contract_multiplier > 0)` or `CHECK (mark.price >= 0)` | A zero or negative multiplier silently zeroes or inverts option P&L. |
| 3 | Spinoff **child** dedupe-key clearing has no test | Must land before `adjust_fills` output is ever persisted — a duplicate `venue_fill_id` violates `fill_venue_id_uniq`. |
| 4 | Self-referential corporate action (`resulting_instrument_id == instrument_id`) accepted | Terminates safely today; one validation line. |
| 5 | `fill.updated_at` is never written (no triggers) | Wrong the day a fill-edit path ships. |
| 6 | `content_hash`'s `side` escaping has no isolating test | Proven non-exploitable in the current 6-field layout, but the proof depends on field *order*; adding a field after `side` reopens it silently. |
| 7 | Preview cannot report duplicates (spec §7 requires it) | Preview never opens a connection by design, so it structurally cannot. Needs an optional read-only dedupe probe. |
| 8 | No property test for spec §9's "sum of per-trade realized P&L equals total from fills" | The only property tying grouping to P&L; would catch an allocation that conserves quantity but not value. |
| 9 | `MarkSource` protocol (spec D7) does not exist | The `mark` table exists; the interface the spec names as the mechanism keeping D out of A was never written. |
| 10 | `open_quantity` / `open_cost_basis` are computed but never persisted | `unrealized_pnl()` cannot obtain its inputs from the database without re-running the grouper. |
| 11 | `fill.is_estimated` never propagates to `trade` | Spec §4 requires opening-balance trades to be excluded from R-multiple and win-rate stats. C cannot be built without it. |
| 12 | `positions` command missing (spec §3, plan's Task 14 interfaces) | Replaced by `trades` during implementation and never recorded as a deferral. |
| 13 | `reconcile` CLI command not implemented | Needs the `account_snapshot` write path. Deliberately not stubbed. |

---

## Accepted permanently

- **Cross-batch same-day identical trades still collapse** — *for CSV sources only; see
  below.* Two genuinely distinct identical trades on one day, split across exports that
  never both contain the pair, dedupe into one. Unfixable without a venue-supplied
  intra-day ordinal. Documented in `commit_batch`'s docstring.

  > [!note] No longer permanent for Coinbase (2026-08-06)
  > This was recorded as unfixable because no *export* carries an intra-day ordinal. The
  > Coinbase Advanced Trade API does: `GET /orders/historical/fills` returns a venue trade
  > id. The schema already has `fill_venue_id_uniq` and the import path already prefers
  > `(account_id, venue_fill_id)` over `content_hash` where the venue supplies an id —
  > that path exists and is unused for Coinbase only because the CSV omits one. Adopting
  > the API (spec A2-16) retires this gap for Coinbase entirely.
  >
  > It remains permanent for **Fidelity**, whose exports carry no fill id and for which no
  > viable API transport exists (spec A2-15).
  >
  > Worth noting how this was found: the gap was accepted as unfixable given the *inputs
  > then in hand*. It was never re-tested against a different input. "Unfixable" is a
  > claim about a data source, not about the problem.
- **The purity checker is evadable** via `getattr(datetime, 'now')()` or `builtins.open`.
  It guards accidental I/O by implementers, not a motivated adversary.
- **`localcontext()` inherits `Emax` / `traps` / `rounding`** from the caller; only `prec`
  is pinned. One caller, and a hostile context is not a real threat model.
- **PostgreSQL 15+ is required.** `db/schema.sql` uses the column-scoped
  `ON DELETE SET NULL (opening_fill_id)` form; a plain `ON DELETE SET NULL` on an older
  server would null `account_id` too and violate its `NOT NULL`.
- **Unindexed single-column foreign keys.** Single-user data volumes; premature.
- **No advisory lock in `migrate.apply()`.** Revisit if A-2 ever runs migrations from a
  container entrypoint with more than one replica.
- Spec says `contract_expiry` where the schema says `expiry`. The schema is the better
  name; amend the spec.

---

## Lessons worth carrying into A-2's plan

Nearly every significant defect in A-1 was the same shape: **an invariant applied
correctly in one place and not in its twin.**

- `instrument_natural_key` escaped its delimiter; `content_hash` did not.
- `Fill.__post_init__` normalised to UTC and rejected naive datetimes; `content_hash`
  looked like it did and did neither.
- Fidelity's fill branch guarded `_decimal`; its cash branch did not.
- Coinbase guarded `quantity == 0` but not `quantity < 0`.
- Four separate pure modules each needed an explicit `localcontext` precision pin, and
  each was missed until reviewed.

Two test-side failure modes recurred often enough to name:

- **Assertions that cannot fail.** Ten shipped during A-1 — a fixture whose intermediates
  were exactly representable, an assertion on a *ratio* where the bug scaled numerator
  and denominator alike, a cross-precision agreement check that passes at any pinned
  precision. Specifying *what* to assert is not enough; the value has to be one the bug
  can actually move. Gate every new test against a mutant before accepting it.
- **Fixtures that cannot fail.** A `_FakePool.close()` that was `pass` made a deadlock
  structurally unobservable regardless of how the assertion was written.

Also: synthetic fixtures cannot catch defects that live in a file format's real-world
packaging. A UTF-8 BOM would have made a real Coinbase export import at 0%, and preamble
lines would have broken a real Fidelity export wholesale — neither was visible against
hand-written fixtures.
