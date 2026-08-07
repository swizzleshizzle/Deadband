# Deadband — A-2: Ledger Completion (Subsystem A, phase 2)

Status: approved
Date: 2026-08-05
Supersedes nothing. Extends `2026-08-04-trade-position-ledger-design.md`.

---

## 1. Context

A-1 shipped the ledger core: pure domain logic, schema, migration runner,
repositories, two CSV importers, and a CLI. It is merged and tested.

A-2 was originally conceived as "the rest of subsystem A" — the FastAPI layer, the
React dashboard, deployment, and thirteen deferred gaps. That is four specs of work.
This document covers only the first chunk.

The trigger for scoping it this way was running a **real Fidelity export** through the
preview path on 2026-08-05, before any A-2 planning. It found that the importer,
which passed 244 tests against hand-written fixtures, mapped 19% of a real file and
silently valued the rest at zero. That result reorders the work: the foundation is not
as finished as the test count suggested, and building an API over it would have
propagated the errors upward rather than exposing them.

### What the real export established

| Finding | Status |
|---|---|
| Money columns are named `Price ($)`; the importer read `price`, and `_decimal(None)` returned `Decimal("0")` for every one | **Fixed** before this spec |
| 13 of 16 fill-shaped rows (`REINVESTMENT`) dropped entirely | This spec |
| One export file spans **five accounts**; the importer merges all rows into one | This spec |
| The multi-account warning cannot see an account whose rows are *all* unmapped | This spec |
| `external_ref` receives the account nickname, not the account number | This spec |
| Action text is compound and open-ended; exact-match mapping cannot work | This spec |
| Activity & Orders exports cap at 90 days | Deferred — separate question |

Confirmed working against real data, and not to be re-litigated: UTF-8 BOM stripping,
preamble skipping, case-insensitive header location, the trailing disclaimer falling
out as unmapped rows, and real option-symbol parsing with its ×100 multiplier.

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| A2-1 | A-2 is ledger completion only. No HTTP, no UI. | The import path is the thing that is wrong. An API over a wrong importer ships wrong data faster. |
| A2-2 | Pull the fee-allocation fix (known gap #1) forward from "before subsystem C" | It needs a schema migration and changes the meaning of `realized_pnl`. The database currently holds disposable test data; after a UI ships it means restating history that matters. Cheapest now. |
| A2-3 | Defer `MarkSource` (gap #9) to subsystem D | Designing an interface with no second implementation to validate it against is guesswork. D supplies the real one. |
| A2-4 | Defer `account_snapshot` write path and `reconcile` CLI (gap #13) to A-4 | Reconciliation needs a way to enter statement balances, which is an entry-UI concern. `ledger/reconcile.py` stays written, tested, and unwired. |
| A2-5 | Imports auto-route by account, and **refuse to commit** if any row routes to an unknown account | D5 keeps accounts separate; the multi-account export makes silent merging the default failure. Refusing is the only outcome that cannot corrupt quietly. |
| A2-6 | Accounts carry an explicit `ignore_on_import` state | A direct consequence of A2-5: without it, an account the user never intends to import makes every import fail permanently. Registered-and-skipped must be distinguishable from unknown. |
| A2-7 | Retirement-plan accounts with no instrument identity are registered as ignored, not modelled | Their rows carry a quantity and an amount but **no symbol and no price**. There is no instrument to recover, so importing them would require inventing identity. Deadband is a trading performance journal (D6); payroll contributions into unnamed funds produce no trades. |
| A2-8 | Dividend reinvestments into real securities are recorded as fills with real basis, tagged `funding_source='reinvestment'` | Gives two separately correct figures: `cost_basis` including reinvestment (tax-correct, matches the 1099-B) and `contributed_capital` excluding it (true out-of-pocket, the performance view). Zero-basis DRIP was considered and rejected — it overstates every subsequent gain. |
| A2-9 | Money-market sweep funds are cash, never positions | A sweep balance is idle cash, not a trade. Modelling it as a position fills the trade log with meaningless rows. |
| A2-10 | Sweep funds are identified by an explicit symbol set, never by `price == 1.00` | A real security can trade at exactly $1.00, and the heuristic would silently convert a genuine position into cash. |
| A2-11 | Sweep reinvestment and inter-sweep exchange rows are `INTERNAL` — recognised, recorded as nothing | Under A2-9 these are the offsetting leg of an already-recorded dividend. Recording them too would double-count the money. |
| A2-12 | An unmatched row **with financial content** blocks the commit; an unmatched row without one warns | The action vocabulary is open-ended, so unknown actions are guaranteed. Blocking everything is unworkable (the disclaimer block is permanently unmapped by design); blocking nothing is exactly how the silent-zero defect looked like success. |
| A2-13 | Action classification is a declarative ordered rule table keyed on **action *and* symbol** | The reinvestment rule (A2-8/A2-9) cannot be expressed by action alone. A pure action→kind table is structurally insufficient, not merely inelegant. |
| A2-14 | `return_of_capital` is recorded under its own kind but **not yet applied to cost basis** | Applying it is corporate-action-shaped work. Recording it distinctly makes the deferral visible; aliasing it to `dividend` would make it silently wrong. Filed as a known gap. |
| A2-15 | **Fidelity stays CSV.** Bulk history is repeated custom-range downloads, not a different transport | Researched 2026-08-06. The 90-day cap is per *download*, not a data limit — Activity & Orders accepts a custom range and roughly five years are retained, so full history is ~20 sequential files. Every alternative is worse: OFX/Direct Connect is being retired in favour of a "Fidelity Access" protocol third-party tools do not yet support, was *itself* capped at 90 days, and Fidelity states third-party money-management software voids their lost-funds replacement guarantee. The Realized Gain/Loss export carries per-lot cost basis over arbitrary ranges and is a good **reconciliation and opening-balance** source, but it is FIFO tax lots — D6 chose average cost, so it is not a fills source. Design consequence: the importer must handle repeated, overlapping-range files gracefully, which the existing idempotent dedupe already does. |
| A2-16 | **Coinbase moves to the Advanced Trade API, replacing CSV import — amending D8. DECIDED, but DEFERRED to part 2b: this phase does not implement it. The CSV importer continues to ship and was itself extended in this phase (finding B, header normalization).** | D8 said "manual and CSV first, API once the schema has proven itself against real trades." That condition is now met: the schema survived a real multi-account export and the correctness work of part 1. The decisive argument for the eventual cut-over is not convenience but **dedupe correctness**. `GET /orders/historical/fills` (`view` scope, genuinely read-only, consistent with §3's permanent no-write-path rule) returns a venue trade id. The schema already has `fill_venue_id_uniq` and §7 already prefers `(account_id, venue_fill_id)` over `content_hash` — a path that exists and is unused for Coinbase purely because the CSV supplies no id. Adopting it will retire a gap currently recorded as permanently unfixable for the CSV path (see §10 and `docs/known-gaps.md`) — but has not yet, since the API importer does not exist. Credentials live only in the deployment environment, never the repository, whenever this is built. |

---

## 3. Scope

### In scope

- `group_fills` quantity-aware exclusion (sequencing-critical; A-2's first task)
- Fee allocation correctness and the `fees_realized` restatement
- Persisted `open_quantity` / `open_cost_basis`
- `is_estimated` propagation from fill to trade
- Schema CHECK constraints and the `fill.updated_at` trigger
- Multi-account routing, ignored accounts, and account-number `external_ref`
- The declarative action rule table, sweep classification, and DRIP funding source
- Unknown-money-row blocking, and a shared zero-price guard across both importers
- Coinbase audit for the same silent-zero defect class (CSV importer; the Advanced
  Trade API source, A2-16, is DECIDED but DEFERRED — see "Out of scope" below)
- Preview duplicate reporting (gap #7)
- `positions` CLI command (gap #12)
- Residual A-1 gaps: `upsert_instrument` repaint, self-referential corporate action
  validation, `content_hash` side-escaping test, spinoff-child dedupe test,
  §9 property test

### Out of scope

- Any HTTP layer, any UI, any deployment work
- `MarkSource` (A2-3), reconciliation wiring (A2-4)
- Applying `return_of_capital` to cost basis (A2-14)
- Modelling balance-only accounts (A2-7); revisit when the Dashboard is built
- **Fidelity API sync of any kind** (A2-15) — no viable transport exists
- Ingesting Fidelity's Realized Gain/Loss export (A2-15). It is the right source for
  reconciliation and opening balances, but it is lot-level and D6 chose average cost;
  wiring it belongs with `account_snapshot`, deferred to A-4
- Automating the ~20-file Fidelity backfill. The importer must *tolerate* repeated
  overlapping ranges, which idempotent dedupe already delivers; driving the downloads is
  a scripting concern, not a design one
- **The Coinbase Advanced Trade API source (A2-16).** DECIDED, but DEFERRED to part 2b —
  not built in this phase. The CSV importer is what Coinbase ships today, and it was
  itself extended here (finding B: header normalization, matching Fidelity's). Two
  questions stay open future work rather than settled by this deferral: the actual
  API cut-over (credentials, fetch/parse/dedupe against `venue_fill_id`), and how to
  reconcile the two ingest paths once CSV-imported Coinbase history and API-imported
  history briefly coexist (see `docs/known-gaps.md`'s known gap #6 equivalent, "Two
  Coinbase ingest paths will briefly coexist," in §10 below). Until the API importer
  lands, the cross-batch same-day-duplicate limitation (§10, "Accepted permanently" in
  `docs/known-gaps.md`) remains in force for Coinbase exactly as it does for Fidelity.

### Acceptance bar

A single testable outcome: **a real multi-account Fidelity export imports end to end** —
routed to its brokerage accounts, retirement plan cleanly ignored, zero unmapped money
rows, correct prices and fees, and hand-verifiable P&L.

---

## 4. Ordering

Dependencies force most of this sequence.

| # | Work | Why here |
|---|---|---|
| 1 | `group_fills` quantity-aware exclusion | Documented as A-2's first task; cheapest while already inside the grouping code. |
| 2 | Migration wave — all new columns, constraints, triggers | One schema pass, so everything downstream codes against the final shape. |
| 3 | Fee allocation + `realized = gross − fees_realized` | Requires #2. |
| 4 | `is_estimated` propagation | Small; schema already touched. |
| 5 | Importer core — rule table, routing, sweep, DRIP, blocking policy | The bulk. Requires #2. |
| 6 | Coinbase audit + shared zero-price guard | Same defect class as #5; do both while the guard is fresh. |
| 7 | Residual gaps — `upsert_instrument` repaint, self-referential corporate action validation, `content_hash` side-escaping test, spinoff-child dedupe test, §9 property test, `positions` command, preview duplicate reporting | Independent of each other; parallelizable. |

---

## 5. Data model

### New columns

| Table | Column | Notes |
|---|---|---|
| `trade` | `fees_realized NUMERIC` | Fees attributable to closed quantity |
| `trade` | `open_quantity NUMERIC` | Gap #10 |
| `trade` | `open_cost_basis NUMERIC` | Gap #10; carries the unamortised entry fee |
| `trade` | `is_estimated BOOLEAN NOT NULL DEFAULT FALSE` | `any(fill.is_estimated)`; spec §4 excludes these from R-multiple and win-rate |
| `fill` | `funding_source TEXT NOT NULL DEFAULT 'external'` | `CHECK IN ('external','reinvestment')` |
| `account` | `ignore_on_import BOOLEAN NOT NULL DEFAULT FALSE` | A2-6 |

### Constraints and triggers

- `CHECK (contract_multiplier > 0)` on `instrument` (gap #2)
- `CHECK (price >= 0)` on `mark` (gap #2)
- `updated_at` trigger on `fill` (gap #5)
- `cash_movement.kind` CHECK expanded with `tax` and `return_of_capital`;
  `tax` added to `OUTFLOW_KINDS`

### The migration hazard, and its guard

`migrate.apply()` re-executes `schema.sql` unconditionally, then applies
`db/migrations/*.sql` once each. `db/migrations/` is currently **empty** — A-2 is the
first work to use it and therefore sets the pattern.

Because `schema.sql` is idempotent, `CREATE TABLE IF NOT EXISTS` will not add a column
to an existing table. **Every change here must therefore be written twice** — in
`schema.sql` for fresh databases, and in a numbered migration for existing ones. Missing
one makes a fresh install diverge from a migrated install, silently, in a way no
existing test catches. A-2 pushes six columns and three constraints through that path.

The guard, built as part of step 2: **a test that constructs one database from
`schema.sql` alone and another from the prior schema plus all migrations, then asserts
the resulting schemas are identical** — columns, types, nullability, constraints. This
makes divergence impossible to ship rather than something to remember.

### Migration is not self-sufficient

Existing `realized_pnl` values were computed under the old fee convention. The new
figure requires the grouper, so **a regroup of every account is a required
post-migration step**, not optional cleanup. Without it one column carries two
different meanings across rows, which is worse than either convention alone.

---

## 6. Pure layer

**`group_fills`** excludes only the quantity a manual trade actually holds, rather than
the whole fill. Today a zero-crossing fill partly belonging to a manual trade has its
remainder silently reaped. The `NotImplementedError` guard in `regroup_account` is
removed only when this lands.

**Fee allocation** (`ledger/pnl.py`), per spec D6's average-cost basis:

```
fees_total     = all fees paid on the trade          (meaning unchanged)
fees_realized  = exit fees in full
               + entry fees × (qty_closed / qty_opened)
realized_pnl   = gross_realized_pnl − fees_realized
```

The unamortised remainder folds into `open_cost_basis`. This **breaks the currently
tested identity** `realized_pnl == gross_realized_pnl − fees_total`, which must be
restated against `fees_realized` deliberately — that test is load-bearing and is
updated, never deleted.

**The grouper** additionally emits `open_quantity`, `open_cost_basis`, and the
`is_estimated` rollup for persistence.

---

## 7. Importer architecture

### The rule table

An ordered list, first match wins. Each rule is a predicate over **action and symbol**
(A2-13) yielding one of three outcomes:

- `FILL` — becomes a canonical fill
- `CASH(kind)` — becomes a canonical cash movement
- `INTERNAL` — recognised, deliberately produces nothing

| Match | Outcome |
|---|---|
| `YOU BOUGHT` / `YOU SOLD` | `FILL`, `funding_source='external'` |
| `REINVESTMENT` + sweep symbol | `INTERNAL` |
| `REINVESTMENT` + security symbol | `FILL` buy, `funding_source='reinvestment'` |
| `EXCHANGED TO` + sweep symbol | `INTERNAL` |
| `DIVIDEND RECEIVED`, `DIVIDENDS` | `CASH(dividend)` |
| `INTEREST EARNED` | `CASH(interest)` |
| `RETURN OF CAPITAL` | `CASH(return_of_capital)` |
| `FOREIGN TAX PAID` | `CASH(tax)` |
| `FEE CHARGED`, `RECORDKEEPING FEE` | `CASH(fee)` |
| `REVENUE CREDIT` | `CASH(rebate)` |
| `ELECTRONIC FUNDS TRANSFER RECEIVED` / `PAID` | `CASH(deposit)` / `CASH(withdrawal)` |
| `CASH CONTRIBUTION`, `CO CONTR`, `PARTIC CONTR`, `CONTRIBUTIONS` | `CASH(deposit)` |
| *no match* | unmapped — see §8 |

### Why `INTERNAL` exists

A sweep dividend appears as two rows: the dividend itself, and a reinvestment of that
dividend back into the sweep fund. Under A2-9 the sweep *is* cash, so these are the two
legs of one event — cash in, then cash into the sweep. Recording both as inflows counts
the money twice. The dividend leg records; the sweep-reinvestment leg is `INTERNAL`.
An inter-sweep exchange is the same case.

Real-security DRIP is the opposite, and **both legs record**: the dividend as cash in,
then the reinvestment as a `FILL` spending that cash. The small residual between the
two is real and belongs in the ledger.

### Sweep identification

An explicit symbol set (A2-10), held as a named constant in the venue's importer so
membership is data rather than logic and can be reviewed at a glance. It covers the
money-market and FDIC-insured deposit sweep funds the venue uses.

Paired with a staleness guard: a symbol in the set whose row price deviates from $1.00
by more than $0.01 emits a warning rather than being silently treated as cash. Sweep
funds hold a $1.00 net asset value by construction, so a deviation means either the set
has acquired a symbol that is not a sweep fund, or a genuine sweep has broken the buck —
both of which need a human, and neither of which should pass unremarked.

### Account routing

Each row's account number is matched against `account.external_ref` within the venue.

- Preview reports every account found, its mapped state, and its row count — including
  accounts whose rows are *entirely* unmapped, which today's warning cannot see.
- `--commit` refuses if any row routes to an unknown account.
- Accounts with `ignore_on_import` route successfully and skip, reported explicitly as
  skipped rather than failed.
- **A NULL `external_ref` is unroutable, never a wildcard.** `UNIQUE (venue,
  external_ref)` does not constrain NULLs in Postgres, so multiple accounts may have
  none; treating NULL as a match would make the first such account a silent catch-all.

---

## 8. Failure policy

| Condition | Outcome |
|---|---|
| Unmatched row with a valid date and non-zero quantity or amount | **Blocks the commit** |
| Unmatched row with no financial content | Warning only |
| Fill-shaped row resolving to a zero price | **Reported**, both importers |
| Any row routing to an unknown account | **Blocks the commit** |
| Row belonging to an `ignore_on_import` account | Skipped, reported as skipped |

The zero-price guard is what makes the defect that motivated this spec unrepeatable: a
missing column and a genuine zero are indistinguishable downstream of `_decimal`, so the
check has to live where the distinction still exists.

---

## 9. Testing

Beyond per-rule unit tests:

- **Every rule in the table must be exercised by at least one fixture row.** A test
  that fails on any unreachable or shadowed rule. Without it, a dead rule is invisible —
  the A-1 "assertions that cannot fail" failure mode applied to the table itself.
- **Schema equivalence** — `schema.sql` alone versus prior schema plus migrations (§5).
- **An anonymized fixture derived from a real export** — same row shapes, same action
  strings, same column naming, with fabricated numbers and account references. Lives in
  `tests/fixtures/`, the only place a tracked CSV may exist. Real exports never enter
  the repository; see the public-repo-hygiene skill.
- **Double-counting assertion** — total cash inflow for a sweep dividend equals the
  dividend amount, not twice it.
- **§9 property test** (gap #8) — sum of per-trade realized P&L equals the total
  computed from fills.
- **Fee-allocation restatement** — the identity test updated to `realized = gross −
  fees_realized`, with a partially-closed fixture whose error the old convention
  actually moves.

Every new test is gated against a mutant before acceptance.

---

## 10. Known gaps this spec creates

Recorded here so they are deferred rather than forgotten:

1. **`return_of_capital` is recorded but not applied to cost basis** (A2-14). Basis
   reads high, so the eventual realized gain reads low, for any position receiving one.
2. **Balance-only accounts are unmodelled** (A2-7). Any aggregate-equity figure will
   omit ignored accounts, which matters when the Dashboard is built.
3. ~~**Bulk history beyond 90 days is unsolved.**~~ **Settled for Fidelity, 2026-08-06**,
   by A2-15: repeated custom-range CSV downloads, roughly twenty for five years, with
   idempotent dedupe absorbing the overlaps; OFX is a dead end and the Realized Gain/Loss
   export is lot-level, so it serves reconciliation rather than fills. **Not settled for
   Coinbase.** A2-16 (the Advanced Trade API, which has no window limit at all) is
   DECIDED but DEFERRED to part 2b — it has not been built. Until it lands, Coinbase's
   only bulk-history path is the CSV export, same as today.
4. **The sweep symbol set requires maintenance.** The staleness guard makes drift
   visible but does not correct it.
5. **Coinbase API credentials will become an operational dependency once A2-16 ships**
   (still deferred to part 2b, see #3 above). A read-only `view` key is still a
   credential: it discloses complete position and balance history if leaked. It must
   live only in the deployment environment, never in this repository, and the importer
   must fail loudly rather than silently falling back to an unauthenticated or empty
   result when the key is missing or rejected — a sync that reports success while
   fetching nothing is the same failure shape as the silent-zero defect that motivated
   this spec. Recorded now, ahead of the work, so it isn't rediscovered under time
   pressure when A2-16 is eventually implemented.
6. **Two Coinbase ingest paths will briefly coexist, once A2-16 ships** (still deferred
   to part 2b — not a concern today, since the API importer doesn't exist yet).
   Retiring the CSV importer while historical CSV-imported fills already exist would mean
   some Coinbase fills are keyed on `content_hash` and later ones on `venue_fill_id`. A
   fill imported by both paths would not dedupe against itself. This needs an explicit
   reconciliation step or a clean cut-over, decided before the (still-unbuilt) API
   importer would ever run against a database holding CSV-imported Coinbase history.
