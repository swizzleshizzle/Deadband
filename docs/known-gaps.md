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

## Found by the real-shape fixture (2026-08-07)

Spec §9 asked for "an anonymized fixture derived from a real export". Part 2a shipped
without it, and two reviewers concluded it was the artifact that would have caught part
2a's Critical before review. It now exists as `tests/fixtures/fidelity/real_shape_activity.csv`,
with `tests/test_fidelity_real_shape.py` as its acceptance test.

It found both of these on its first run — against the rule table part 2a had already
merged, six per-task reviews and a whole-branch review clean. F1 is now fixed; F2 is a
decision still open.

> [!note] Same non-specificity rule as the 2026-08-05 section
> Shapes, never specimens. The synthetic fixture is the reproduction case.

| # | Gap | Why it matters |
|---|---|---|
| F1 | ~~**An employer-plan `Investment Gain/Loss` row matches no rule and therefore BLOCKS every commit**~~ — **FIXED 2026-08-08**, mapped to `INTERNAL`. | Listed as unhandled in R4 back on 2026-08-05, but part 2a's rule table closed the other eight actions in that list and not this one, so an item recorded as "vocabulary to cover" became a hard stop: under §8 an unmapped row carrying money refuses the commit, and a real export holds several — it could not be imported at all. The row is periodic market-value change, not a transaction, so `CASH` would inject money that never moved *and* double-count appreciation the ledger already derives from positions × price. `INTERNAL` — recognised, deliberately produces nothing — is the honest description. Verified against a real export: blocking went from several to zero with fill and cash counts unchanged, so nothing was reclassified into money. **The verb must stay narrow.** Broadening it to a bare `INVESTMENT` prefix survived the entire suite, and the venue emits other `INVESTMENT …` actions — `INVESTMENT ADVISORY FEE` is real money leaving. An over-broad `INTERNAL` is the worst available outcome: it loses money with no warning at all, where an unmapped row at least blocks. `test_investment_gain_loss_does_not_swallow_its_prefix_neighbours` is the guard. |
| F2 | **Employer-plan rows carry a unit quantity and no price, and the rule table maps them to `CASH` — so the position held inside the plan is invisible to the ledger.** | A plan `Contributions` row is the *purchase* of fund units at an implied price (amount ÷ quantity), with the `Price ($)` column left empty; `RECORDKEEPING FEE` is the same shape in reverse, a fee paid by selling units. Recorded as pure cash, every plan holding has no basis, no quantity and no trade. This is not a mis-mapping of one verb — the whole plan dialect is modelled as cash flow. Deciding it needs an answer to "what instrument is this?", since the export supplies a fund name in `Description` and **no ticker at all**. Subsystem-shaped; do not fix inside an importer task. |

**The dialect split is the underlying shape, and it is the project's named recurring
defect again.** A real export contains two row grammars, not one: brokerage rows write an
empty field as `""`, set `Type`, use SHOUTED compound action prose, and carry a ticker;
employer-plan rows write empties bare, leave `Type` and `Symbol` empty, use Title-case
bare verbs (`Contributions`, `Dividends`, `Investment Gain/Loss`), and identify the
security only in `Description`. Every guard added from here should be checked against
**both**, which is now mechanical: they are both in the one fixture.

**Also worth not re-litigating** (verified while building the fixture): the two
`.strip()` calls defending a whitespace-padded option symbol — parse()'s call site and
`parse_option_symbol`'s own — are *mutually redundant*. Removing either alone leaves the
suite green; only removing both is caught. Neither is "the" load-bearing one, and no
real-shape input can separate them, because the real export's only whitespace-padded
symbol is that option row.

**Hygiene note:** the deny-list covers real tickers but not the plan's *fund names*,
which appear in `Description` and are equally "what Michael decided". Worth adding
before any further work quotes a plan row.

---

## Found during A2 part 2b1: Coinbase API import (2026-08-08)

### UNVERIFIED: the Coinbase fill dedupe key may not be unique

This is the gap in this document most likely to lose money. It has its own heading
because it previously sat buried inside the cash-export gap below, where a heading skim
or a generated table of contents could not see it — and it is the only record that the
"confirm `trade_id` is unique" task was never actually run.

> [!warning] UNVERIFIED — the fill dedupe key may not be unique.
> `importers/coinbase_api.py` keys every Coinbase fill on the API response's `trade_id`,
> because the spec (A2-16) says "the API supplies a venue trade id." But the same
> response also carries an `entry_id` field, and Coinbase's own documentation does not
> state which of the two — if either — is guaranteed unique per fill.
>
> If `trade_id` can repeat across two genuinely distinct fills, `fill_venue_id_uniq`
> (`UNIQUE (account_id, venue_fill_id)`) will not reject the second one as a duplicate —
> from the schema's point of view it *is* the first one. One of the two real fills would
> then disappear from the ledger silently: no error, no warning, no log line. That is a
> money-losing failure mode, not a cosmetic one.
>
> This could not be checked here: **no Coinbase CDP credentials exist on this machine**,
> so `fetch_all_fills()` has never been run against a real account, and the assumption
> has never been tested against a real response.
>
> **Partially mitigated 2026-08-08 (finding C2).** The importer now refuses the whole
> batch if two fills in one parsed document share a `trade_id`, naming both positions and
> telling the reader to re-key on `entry_id`. So the assumption is now self-checking
> rather than merely written down. **This is a reduction in exposure, not a closure:** the
> check sees repeats only *within one parsed document*. A full sync (no `--start`) pulls
> the whole history into a single merged document and therefore sees the entire set, but a
> windowed `--start`/`--end` sync can straddle a repeat, and a repeat split across two
> separate syncs is invisible to it. The key is deliberately NOT switched to `entry_id`
> pre-emptively: if `entry_id` were the unstable field, that would break idempotency and
> re-insert the entire history on the next sync — trading a possible silent loss for a
> certain silent double-count.
>
> **The check, so whoever has credentials can settle it in one command:**
>
> ```python
> fills = <parse the string fetch_all_fills() returns, into a list of fill dicts>
> len(fills)                                   # total fills returned
> len({f["trade_id"] for f in fills})          # distinct trade_id
> len({f["entry_id"] for f in fills})          # distinct entry_id
> ```
>
> If all three numbers agree, `trade_id` is safe as the dedupe key over that sample (not
> a proof for all time, but strong evidence, and stronger the more fills the account has).
> If the `trade_id` count is lower than the total, `trade_id` **can** repeat, the current
> dedupe key is wrong, and `fill_venue_id_uniq` is silently losing fills right now for any
> account with enough volume to hit a repeat.
>
> Do not downgrade this to a routine follow-up. It stays open until someone with
> credentials runs the check above and records the result.

### One `size_in_quote` fill disables `sync --commit` for that account, permanently

`importers/coinbase_api.py` refuses a fill whose `size_in_quote` flag is set: the flag
flips the meaning of `size` from base units to quote currency, and no conversion is
available from the fill alone, so recording it as a quantity would produce a position
wrong by a factor of the price. Refusing rather than guessing is the right call. The
consequence is not obvious, though, and needs saying out loud:

**Symptom.** `deadband sync coinbase --commit` exits 2 with
`refusing to commit -- unmapped row(s) carry money and no rule matched them`, naming a
fill with `size_in_quote`. It does this **every time**, and nothing else commits either —
the refusal is all-or-nothing by design (a partial commit that silently drops a
money-carrying row is the defect that policy exists to prevent). Preview is unaffected.

**Why it is unbounded.** A `size_in_quote` fill is not exotic: it is what Coinbase's
retail "buy $50 of BTC" market order produces. One such fill anywhere in an account's
history is enough, and since the CSV importer no longer emits fills at all, there is no
second route — **that fill cannot enter the ledger by any path this system offers**, and
neither can any fill synced alongside it.

**Workaround.** Two (or more) windowed syncs that straddle the bad fill, e.g.
`deadband sync coinbase --end 2026-05-13 --commit` and
`deadband sync coinbase --start 2026-05-14 --commit`. Everything outside the window
commits normally; the `size_in_quote` fill itself stays out of the ledger, and its money
is simply missing from the account until this is fixed properly.

**Real fix, unbuilt.** Convert with the fill's own `price`: base quantity ≈
`size / price`, minus whatever fee convention Coinbase applies. That needs verification
against a real `size_in_quote` response before it can be trusted, and no credentials
exist here — same blocker as the dedupe-key gap above.

### Coinbase non-trade cash still requires a manual CSV export

The Advanced Trade API surface has no endpoint for deposits, withdrawals, transfers,
rewards income, staking income, or interest — only fills. Verified against Coinbase's
REST endpoint index on 2026-08-08. A2-16's claim that the API "replaces the CSV importer"
holds only for fills (see the update above); every non-trade cash movement still has to
arrive via a CSV export, through the Coinbase CSV importer's cash path (it emits no fills
any more, but still emits cash rows).

A Coinbase App API v2 `transactions` source would close this — but that is a separate,
unbuilt integration with a different base URL, different auth, and a different data
shape, not an extension of the Advanced Trade client this phase built.

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
| 10 | ~~`open_quantity` / `open_cost_basis` are computed but never persisted~~ — CLOSED in A-2 part 1, recorded 2026-08-08 | `unrealized_pnl()` cannot obtain its inputs from the database without re-running the grouper. |
| 11 | ~~`fill.is_estimated` never propagates to `trade`~~ — CLOSED in A-2 part 1, recorded 2026-08-08 | Spec §4 requires opening-balance trades to be excluded from R-multiple and win-rate stats. C cannot be built without it. |
| 12 | ~~`positions` command missing (spec §3, plan's Task 14 interfaces)~~ — CLOSED, recorded 2026-08-08 | Replaced by `trades` during implementation and never recorded as a deferral. |
| 13 | `reconcile` CLI command not implemented | Needs the `account_snapshot` write path. Deliberately not stubbed. |
| 14 | Unrealized P&L is only as fresh as the last manual mark | There is no price feed, and that is deliberate (spec §3 keeps market data out of the foundation) — `positions` shows each mark's date alongside the valuation so staleness is visible rather than assumed. |

> [!note] Gaps #10 and #11 were done and left on the list
> Both landed in A-2 part 1, not part 2 — they were completed and never struck. The
> evidence: `db/trades.py:115` rolls up `fill.is_estimated` across a trade's allocations
> with `any()` (not `all()` — a single estimated fill taints the whole trade, per spec
> §4); `db/trades.py:128` persists `open_quantity` and `open_cost_basis` on insert and
> `db/trades.py:145-147` repaints them (alongside `is_estimated`) on every regroup's
> `ON CONFLICT DO UPDATE`. Three tests in `tests/db/test_trades.py` guard the rollup
> specifically: `test_a_trade_containing_an_estimated_fill_is_itself_estimated`,
> `test_a_trade_of_only_exact_fills_is_not_estimated`, and
> `test_a_trade_with_an_estimated_closing_fill_is_also_estimated`.
>
> A gap list that carries closed items is worse than no list — it reads as still-true and
> costs the next reader a re-investigation to discover otherwise. That is exactly what it
> cost here: this task opened assuming both were still open, checked the code, and found
> both had been done a phase earlier. Worth writing down rather than quietly deleting the
> two rows, for the same reason the rest of this file keeps closed entries visible instead
> of pruning them.

> [!note] Gap #12, closed — what `positions` actually does
> `positions` groups an account's open trades per instrument (quantity-weighted average
> cost basis across trades in the same instrument), values a position only where a manual
> mark exists, and rolls `is_estimated` up with the same `any()` convention as `trade`. A
> position that cannot be priced — the open quantity spans both a long and a short, an
> orphaned trade's quantity is unknown, or simply no mark has ever been recorded for that
> instrument — is still listed, carrying an `unvaluable_reason` instead of a number, rather
> than being silently dropped from the view. That was a deliberate choice: a position
> missing from a list reads as "flat", which is a worse failure than a position present
> with an honest "can't price this" note.
>
> `reconcile` (gap #13) is now unblocked by this work, though still unbuilt: it needs the
> `account_snapshot` write path, but the position query it would consume (`db/positions.py`'s
> `open_positions`) and `ledger/reconcile.py` both exist already.

---

## Accepted permanently

- **Cross-batch same-day identical trades still collapse** — *for CSV sources only; see
  below.* Two genuinely distinct identical trades on one day, split across exports that
  never both contain the pair, dedupe into one. Unfixable without a venue-supplied
  intra-day ordinal. Documented in `commit_batch`'s docstring.

  > [!note] Still permanent for Coinbase CSV — API cut-over decided but deferred (2026-08-05)
  > This was recorded as unfixable because no *export* carries an intra-day ordinal. That
  > remains true today: the CSV importer is what Coinbase ships, and it carries no ordinal
  > and no venue fill id, so this gap is currently in force for Coinbase exactly as it is
  > for Fidelity.
  >
  > A path off this exists on paper. The Coinbase Advanced Trade API would return a venue
  > trade id (`GET /orders/historical/fills`); the schema already has `fill_venue_id_uniq`
  > and the import path already prefers `(account_id, venue_fill_id)` over `content_hash`
  > where the venue supplies an id — that path exists and is unused for Coinbase only
  > because the CSV omits one. Adopting the API (spec A2-16) **would** retire this gap for
  > Coinbase. But A2-16 is DECIDED and DEFERRED to part 2b, not implemented in this phase —
  > the API importer does not exist, so nothing has actually retired the gap yet. Treat
  > this note as a plan, not a status change, until A2-16 ships.
  >
  > It remains permanent for **Fidelity** regardless of A2-16's outcome, whose exports
  > carry no fill id and for which no viable API transport exists (spec A2-15).
  >
  > Worth noting how this was found: the gap was accepted as unfixable given the *inputs
  > then in hand*. It was never re-tested against a different input. "Unfixable" is a
  > claim about a data source, not about the problem -- and for Coinbase specifically it
  > is a claim that will stop being true the day A2-16 actually ships, not one that is
  > false today.
  >
  > **Update 2026-08-08 — A2-16 shipped, for fills only.** The Coinbase API importer
  > (`importers/coinbase_api.py`, driven by `deadband sync coinbase`) now exists. Every
  > Coinbase fill imported from here forward is keyed on the venue's own `trade_id`
  > (`venue_fill_id`), not `content_hash`, so the collision described above is no longer
  > reachable for new Coinbase fills.
  >
  > This also **closes spec §10 gap 6** — but it closed differently than the
  > spec anticipated. The spec worried about a wholesale cut-over: retiring the CSV
  > importer while historical CSV-imported fills already existed, needing either an
  > explicit reconciliation step or a decision about which key wins. What actually shipped
  > splits the cut-over **by row kind, not wholesale**. Fills now come only from the API,
  > keyed on `venue_fill_id`. Cash movements still come only from the CSV importer, keyed
  > on `content_hash` — the CSV importer no longer emits fills at all, it reports each
  > trade row and points at `sync coinbase` instead. A2-16's claim that the API "replaces
  > the CSV importer" is therefore true for fills and **false for cash** — see the new gap
  > below.
  >
  > **Correction 2026-08-08 (review finding I3).** This entry previously said "No fill is
  > reachable by two paths, so the hazard the spec was worried about is gone with no
  > reconciliation code written, because there is nothing to reconcile." That is true only
  > of a database with **no pre-cut-over Coinbase fills in it** — which was verified for
  > this owner's database (0 fills) at the time and is stated unconditionally nowhere else.
  > For anyone else's database it was false and dangerous:
  >
  > The two partial unique indexes are disjoint by construction — `fill_venue_id_uniq` is
  > `WHERE venue_fill_id IS NOT NULL`, `fill_content_hash_uniq` is
  > `WHERE content_hash IS NOT NULL` — and `db/importing.py` gives each fill exactly one of
  > the two keys. A CSV-imported Coinbase fill has `venue_fill_id NULL, content_hash SET`;
  > the *same trade* re-arriving via `sync` has `venue_fill_id SET, content_hash NULL`.
  > Neither index sees the other. Both rows land, both feed `regroup_account`, and the
  > account's position and realized P&L **double**, silently.
  >
  > `cli.py`'s commit path now refuses this: before opening the write transaction, a batch
  > whose fills carry a `venue_fill_id` is rejected if the target account already holds
  > fills with `content_hash IS NOT NULL AND venue_fill_id IS NULL`, with a message naming
  > the remedy. It is venue-neutral — any future CSV-to-API cut-over inherits it — and it
  > costs one `SELECT count(*)` per target account on the commit path only. It is a
  > **refusal, not a reconciliation**: closing the gap properly still needs a real
  > migration that matches old rows to new ones and keeps one of each.
  >
  > **Spec §10 gap 5 (Coinbase API credentials) is now LIVE**, not a future concern:
  > `COINBASE_API_KEY` and `COINBASE_API_SECRET` are a real operational dependency the
  > moment `sync coinbase` is run against a live account, not a thing to plan for later.
  > See the README's "Coinbase fills" section for the operational contract.
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
- **Slack that exceeds the defect** (added 2026-08-07, three instances in one sitting).
  All three were in the real-shape fixture's own tests, and all three were the same
  mistake in different clothes: an assertion loose enough that the thing it watched for
  could happen without moving it.
  - A row-accounting check written as `>= 23` against a total that also counted nine
    disclaimer rows. It summed to 32 and would have stayed above the bar however many
    dated rows were silently dropped. Fixed by making the count **exact** — and the
    exact version then failed immediately, twice, on real problems the `>=` version had
    been hiding.
  - Two row filters that accepted anything date-*shaped*: `[:2].isdigit()` swallowed a
    document id (`9900001.1.0`), and an unanchored `\d{2}/\d{2}/\d{4}` search swallowed
    the footer's "Date downloaded …". Both over-counted by exactly one, which an
    inequality would never have surfaced.

  The generalisation is not "prefer `==` to `>=`" but: **when an assertion has slack,
  the slack must be smaller than the smallest defect it is meant to catch** — and if the
  slack cannot be bounded, the assertion is decoration. A useful tell is that an exact
  assertion fails loudly while you are still writing it; a slack one goes green
  immediately and feels finished.

Also worth naming: **the deny-list guards identifiers, not values.** The real-shape
fixture was first written with faithfully-copied SHAPES and faithfully-copied NUMBERS —
real amounts, real dates, the export's real document id — and passed the deny-list scan
cleanly, because the list holds account numbers and tickers and those had all been
fabricated. Caught by diffing the fixture's tokens against the real export's, which is
the check that actually matches the rule ("real values that look boring are still real"):

```
python3 - <<'EOF'   # run before committing anything derived from a real export
import re, pathlib
real = pathlib.Path('imports/Accounts_History.csv').read_text(encoding='utf-8-sig')
fix  = pathlib.Path('tests/fixtures/fidelity/real_shape_activity.csv').read_text(encoding='utf-8-sig')
nums = lambda t: set(re.findall(r'-?\d+\.\d+|\b\d{2,}\b', t))
print(sorted(nums(real) & nums(fix)))   # expect only calendar/structural fragments
EOF
```

Also: synthetic fixtures cannot catch defects that live in a file format's real-world
packaging. A UTF-8 BOM would have made a real Coinbase export import at 0%, and preamble
lines would have broken a real Fidelity export wholesale — neither was visible against
hand-written fixtures.
