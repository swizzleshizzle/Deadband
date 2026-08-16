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
`refusing to commit -- row(s) below block the commit (see each reason)`, naming a
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
- **No schema counterpart to the `resulting_instrument_id != instrument_id` guard.**
  `CorporateAction.__post_init__` (`ledger/corporate.py:57-61`) rejects a self-referential
  corporate action in Python, but `db/schema.sql`'s `corporate_action` table has no
  matching `CHECK`. There is no `db/corporate_actions.py` repository module yet, so nothing
  enforces this on the write side today — but the day one exists and inserts via raw SQL
  rather than by constructing a `CorporateAction`, it could write a row the *read* path then
  refuses to construct: a table the system can write but cannot load. Whoever builds that
  module should add `CHECK (resulting_instrument_id IS DISTINCT FROM instrument_id)` to
  `corporate_action` in the same change, not treat the Python guard as sufficient on its own.

---

## Fix in A-2

> [!note] Strike entries with evidence, in the same commit that closes them
> Five items were carried as open across three sessions before anyone struck
> them: #2, #5 and #7 are struck below, this session. #10 and #11 were
> carried stale through last plan too; a strike for them exists but lives on
> a separate, unmerged branch, so this file still shows them open below until
> that branch lands — do not strike them here, to avoid a worse merge
> conflict. Each stale entry costs a re-investigation — paid by whoever plans
> next, not by whoever could have struck it for free while the fix was
> already in front of them.

| # | Gap | Why it matters |
|---|---|---|
| 1 | ~~`upsert_instrument` repaints only `symbol`; a wrong `contract_multiplier`, `strike` or `expiry` stored on first insert is never corrected~~ — FIXED 2026-08-09. `upsert_instrument` (`db/instruments.py:14,56-67`) now repaints `contract_multiplier`, `root`, `chain` and `contract_address` from `EXCLUDED` on every upsert, not just `symbol`. **Correction to this entry's own claim:** `strike` and `expiry` were never actually at risk — for an option both sit *inside* `instrument_natural_key` (`ledger/types.py`), so a different value mints a different row instead of drifting on an existing one. `contract_multiplier` was the only field outside the key that could go stale, and it is now the one fixed. | Repainting has a cost of its own — see the new gap below. |
| 2 | ~~No `CHECK (contract_multiplier > 0)` or `CHECK (mark.price >= 0)`~~ — already closed, not by this plan. `instrument_multiplier_chk` (`db/schema.sql`) and `mark_price_chk` (`db/schema.sql`) both exist. | Stale entry: the constraints were already shipped when this was last written down. |
| 3 | ~~Spinoff **child** dedupe-key clearing has no test~~ — FIXED 2026-08-09. `test_spinoff_child_clears_dedupe_keys` (`tests/test_corporate.py:715`) now covers it, alongside the parent's existing `test_spinoff_parent_clears_dedupe_keys` (`:691`). The code being tested already cleared the child's keys correctly — the gap was in coverage, not behaviour. | |
| 4 | ~~Self-referential corporate action (`resulting_instrument_id == instrument_id`) accepted~~ — FIXED 2026-08-09. `CorporateAction.__post_init__` (`ledger/corporate.py:57-61`) rejects it unconditionally: the check sits outside the `MERGER`/`SPINOFF`/`SYMBOL_CHANGE` conditional, so it fires for *any* action type carrying a resulting id — including `SPLIT`, which requires none. `test_a_corporate_action_may_not_produce_its_own_instrument` (`tests/test_corporate.py:172`) is parametrised over all four action types for exactly that reason. | |
| 5 | ~~`fill.updated_at` is never written (no triggers)~~ — already closed, not by this plan. `set_updated_at()` (`db/schema.sql`) and `fill_set_updated_at` (`db/schema.sql`) both exist. **This strikes only "does the trigger exist."** A separate, still-open item — that the trigger has no *behavioural* test, i.e. nothing proves it actually fires — is recorded under "Carried into A-2 part 2 and beyond" below, and striking this row does not touch it. | |
| 6 | ~~`content_hash`'s `side` escaping has no isolating test~~ — FIXED 2026-08-09, but not the way this entry assumed. A black-box collision test is **impossible** here: `_canon()` and `str(occurrence)` can never emit the `\|` delimiter and `symbol` is escaped separately, so the three rightmost payload tokens are always `(qty, price, occurrence)` — `side` is uniquely recoverable whether or not it is escaped, so no pair of inputs can be built that collides with escaping removed. `test_hash_escapes_delimiter_in_side` (`tests/test_importer_base.py:86-136`) is instead a white-box structural pin on the whole 7-field payload, sensitive to any change in field order or count. | |
| 7 | ~~Preview cannot report duplicates (spec §7 requires it)~~ — already closed, not by this plan. `--check-duplicates` (pre-flight routing guard `cli.py:188-209`, the reporting that actually fulfils §7 at `:254-262`, flag registered `:1697`) shipped in PR #3. | Stale entry, same shape as #2 and #5. |
| 8 | ~~No property test for spec §9's "sum of per-trade realized P&L equals total from fills"~~ — FIXED 2026-08-09. `test_sum_of_per_trade_realized_pnl_equals_the_total_from_fills` (`tests/test_grouping_properties.py:342-380`) checks it against `gross_realized_from_fills` (`:253-308`), an independent oracle in exact `Fraction` arithmetic sharing no code with the production path. **Closing this row is not the same as closing the hole it names — see the new gap below, which matters more than this row did.** | |
| 9 | `MarkSource` protocol (spec D7) does not exist | Still open. The `mark` table exists; the interface the spec names as the mechanism keeping D out of A was never written. Whether a one-implementation `Protocol` earns its keep in a repo with no type checker is a live question here, not a foregone conclusion. |
| 10 | ~~`open_quantity` / `open_cost_basis` are computed but never persisted~~ — CLOSED in A-2 part 1, recorded 2026-08-08 | `unrealized_pnl()` cannot obtain its inputs from the database without re-running the grouper. |
| 11 | ~~`fill.is_estimated` never propagates to `trade`~~ — CLOSED in A-2 part 1, recorded 2026-08-08 | Spec §4 requires opening-balance trades to be excluded from R-multiple and win-rate stats. C cannot be built without it. |
| 12 | ~~`positions` command missing (spec §3, plan's Task 14 interfaces)~~ — CLOSED, recorded 2026-08-08 | Replaced by `trades` during implementation and never recorded as a deferral. |
| 13 | ~~`reconcile` CLI command not implemented — unblocked, not closed~~ — CLOSED 2026-08-10. `deadband snapshot add` and `deadband reconcile` both ship (`cli.py:889-985`, `:988-1235`), per [the reconcile design spec](superpowers/specs/2026-08-10-reconcile-design.md). **The gap named two missing pieces; there were three.** (1) The `account_snapshot` write path: `db/snapshots.py`'s `add_snapshot` (`:13-49`) and `latest_snapshot` (`:52-70`). (2) The `OpenPosition`/`Position` type mismatch: the adapter lives in `cmd_reconcile` itself (`cli.py:1080-1142`), partitioning `open_positions`' rows on `unvaluable_reason` — never on `direction` — into `Position`s and `UnvaluableRef`s. (3) **`computed_cash` had no implementation at all, and the gap never named it.** `OUTFLOW_KINDS` (`importers/base.py:54`) had sat there since A-1 with a docstring anticipating "a consumer that needs to net cash movements" (`:50-53`), and no such consumer was ever written. Cash also cannot come from `cash_movement` alone — a buy spends cash as a *fill*, not a movement — so `ledger/cash.py::net_cash` (`:31-57`) derives it from both, with the contract multiplier applied to every fill's notional (`:55`). | The gap also posed a design question — what should `reconcile` do with a position it cannot value — and it is answered as neither excluded-silently nor refuse-the-account: the run reports its numbers and returns verdict `UNRELIABLE` (`ledger/reconcile.py:15-18`), which outranks `DRIFT` (`:139-147`), and `cmd_reconcile`'s rendering names the excluded positions on the same screen (`cli.py:1202-1213`) so the resulting large negative drift is not mistaken for a defect. |
| 14 | Unrealized P&L is only as fresh as the last manual mark | There is no price feed, and that is deliberate (spec §3 keeps market data out of the foundation) — `positions` shows each mark's date alongside the valuation so staleness is visible rather than assumed. |
| 15 | Repainting `contract_multiplier` on `upsert_instrument` (new, from #1's fix) restates every existing fill's realized/unrealized P&L on that instrument, with no record that the multiplier ever changed | Better than a permanently-wrong, uncorrectable multiplier — which is what shipped before this fix — but real: the next upsert silently revalues every historical fill against the new multiplier, and nothing records the old value, the new value, or when the change happened. An audit trail, or at minimum a warning on change, is a larger design question, deliberately not taken here — `db/instruments.py:47-54` says so in its own docstring. **The risk cuts both ways, not just one:** repainting is now last-write-wins, and `Instrument.contract_multiplier` defaults to `Decimal(1)` — correct for equity and crypto spot, silently 100x wrong for an option. A future caller that mints an `AssetClass.OPTION` instrument without explicitly passing the multiplier overwrites a correct `100` with `1` on that very upsert, retroactively revaluing every fill on the instrument, with no error and no log line. `Instrument.__post_init__` (`ledger/types.py`) now guards this at construction by requiring an explicit `contract_multiplier` for OPTION instruments, converting the omission into a crash — but that only protects a future `Instrument(...)` call site, not a hand-built row or any other route to constructing one. |
| 16 | The spec §9 property (closed as #8, above) does not close the hole its own motivation describes: an allocation that conserves quantity but misattributes value **between two fully-closed trades** is still undetected by any test in the repo | For a fill set where every trade closes flat, gross realized reduces algebraically to `Σ sells − Σ buys`, independent of how the fills are partitioned into trades — the total is partition-invariant, so a property that only checks the total is structurally blind to fully-closed misattribution, not blind by an oversight in how it was written. Verified twice with a concrete counterexample: `BUY 1@10, BUY 1@100, SELL 1@20, SELL 1@300` repartitions to per-trade grosses `[10, 200]` under one grouping and `[290, -80]` under another — a 280-unit misattribution between the two trades — while both sum to the identical total, `210`. The property is sensitive only where a residual open position survives the fill set, because that residual's cost basis is the one quantity that does not cancel out of the sum. Closing this needs a per-trade oracle, not a total — materially more test than #8 was. It also has no wide-magnitude variant, unlike every sibling property in `tests/test_grouping_properties.py`: above roughly 1e60, `Decimal.quantize` raises and the property's absolute-tolerance model stops holding. **This entry undersells how narrow the blind spot actually is, in the other direction:** working the algebra further, total gross = `Σsells − Σbuys ± residual_basis`, and `residual_basis` is fully determined by the final open segment — so *any legal partition* gives the same total, not merely any partition among the closed trades. The property can therefore only fail on an *illegal* partition that also keeps every closed trade flat, allocates each fill exactly once, leaves at most one open trade, keeps direction matching the earliest fill, and moves value into or out of that final open segment. That class is non-empty (constructed by hand), so the gap still earns its place — but of four grouping mutations tried against it, the one that reddened this property by assertion also reddened `test_direction_matches_opening_fill`, meaning that class of bug may already be caught elsewhere. |
| 17 | `upsert_instrument` is annotated `-> UUID` but would return `None` if its `ON CONFLICT` clause were ever changed to `DO NOTHING` | `RETURNING id` (`db/instruments.py:82`) yields no row under `DO NOTHING`, since no row is written or fetched. Unreachable today — the clause is `DO UPDATE` (`:76-81`) — but a trap for whoever "simplifies" it later: the return type would keep claiming `UUID` after the behaviour stopped delivering one. |
| 18 | No statement parsing (spec §10.1) | Figures are typed by hand — `snapshot add --equity --cash` (`cli.py:1760-1761`) — so a mistyped `$52,340` entered as `$523,40` reads identically to a real numeric disagreement; `reconcile` has no way to tell them apart. Mitigated only by `add_snapshot` storing what was entered (`db/snapshots.py:13-49`) so a bad figure can be found and re-typed correctly, not by anything that catches it at entry. A PDF/statement parser is a separate subsystem and is not what gap #13 asked for. |
| 19 | Multi-currency accounts are refused, not handled (spec §10.2, R7) | The moment a non-USD account exists, `reconcile` stops working for it entirely — `account_cash` (`db/cash.py:40-91`) raises `MixedCurrencyError` rather than attempting FX conversion, and `cmd_reconcile` turns that into a clean exit-2 refusal (`cli.py:1154-1159`). Correct for v1 (it does not model FX; summing across currencies is a confident wrong number, spec line 49) but it is a hole left open, not a design closed off. |
| 20 | No snapshot history view (spec §10.3) | Snapshots accumulate in `account_snapshot` but nothing surfaces the trend — `latest_snapshot` (`db/snapshots.py:52-70`) only ever returns the single most recent row on or before a date. "When did this drift first appear" needs hand-written SQL against the table. |
| 21 | A snapshot cannot be deleted, only overwritten at the same `as_of` (spec §10.4) | `add_snapshot`'s `ON CONFLICT (account_id, as_of) DO UPDATE` (`db/snapshots.py:42-49`) is the only way to change a stored figure. A snapshot entered against the wrong account, or with a wrong `as_of`, stays there under whatever key it was actually written to — the same shape as `set_mark` having no delete, extended to a second table. |
| 22 | `reconcile` refuses an unknown account id while `positions` does not (spec §10.5) | Deliberate here — reconcile's whole purpose is to be trustworthy about absence (`cli.py:1048-1055`) — but it leaves the two commands inconsistent: `positions` (`cli.py:637-649`) prints "no open positions" (`:736`) for a well-formed but nonexistent UUID rather than refusing. The inconsistency should be resolved by making `positions` stricter, not by loosening `reconcile`. **Subsumed and extended by #26 below** — the spread was three behaviours when #26 was written; `snapshot add`'s raw traceback has since been fixed, so it is back to the two this row names. |
| 23 | Cash correctness depends on every fill being present; a missing fill in an account that also has an unvaluable position produces an `UNRELIABLE` verdict that hides it (spec §10.6) | A missing fill on its own shows up as drift — that is the point of the command. But once an account also carries an unvaluable position, the verdict is `UNRELIABLE` regardless (`ledger/reconcile.py:139-147`), and reconcile has no way to tell "the gap is entirely the unvaluable position" from "the gap is partly a missing fill on top of it." The two causes are indistinguishable from the output alone. |
| 24 | ~~`fill.fee_currency` is an unchecked third currency source~~ — FIXED 2026-08-11, in this PR's (#8) CodeRabbit review round. `account_cash`'s fetch now selects `f.fee_currency` alongside `i.quote_currency` (`db/cash.py:57-58`) and folds it into the currency set (`:68-72`), so a fee denominated differently from its instrument trips `MixedCurrencyError` instead of being subtracted from a balance in another currency (`ledger/cash.py:56`). **The fix is narrower than "check every fill's fee_currency," deliberately:** only NONZERO fees are considered, because `fill.fee_currency` is `TEXT NOT NULL DEFAULT 'USD'` (`db/schema.sql:73`) and a zero-fee fill on a EUR instrument therefore carries a meaningless `'USD'` — checking it would refuse a perfectly single-currency account, a false refusal being as damaging as a false pass for a command whose whole value is trustworthiness. A zero fee also adds zero in any currency, so its denomination cannot make the sum wrong. Both halves are pinned: `test_a_nonzero_fee_in_another_currency_is_refused` and `test_a_zero_fee_in_another_currency_is_not_refused` (`tests/db/test_cash.py`). **This widened the spec's own wording, which is why it was deferred at execution time.** R7 as originally written covered movements and instruments only. The refusal now genuinely covers three sources, so everything describing it was widened in step rather than left under-promising: `MixedCurrencyError`'s docstring, the raised message, `README.md`, and — after adjudication in the same review round — the design record itself, both R7 (`docs/superpowers/specs/2026-08-10-reconcile-design.md:53`) and §8's failure-policy row (`:181`), each marked amended 2026-08-11 with a header note at `:5`. **Spec, code, README and this entry now agree; there is no narrower authority left to fall back to.** The spec was editable here for a reason worth recording: it is *this branch's own* design record (committed in `ffcb8cb` on `feat/a2-reconcile`), not a settled artifact being reached into from outside, and a spec describing a narrower refusal than what ships is the same defect class as an over-claiming docstring or a stale citation. §10's own gap 2 needed no change — it says "multi-currency accounts are refused, not handled," which was never the narrow claim. Deferring it during execution was right (do not widen a settled spec mid-flight) and closing it in review was right for the same reason in reverse — the spec is open for revision here, and the hole was real. | Was: a cross-currency fee summed straight into the balance without tripping the refusal — the same confident-wrong-number the refusal exists to prevent, narrower in reach because it needs a cross-currency fee specifically. |
| 25 | `cash_movement.amount` has no `CHECK (amount > 0)` | `OUTFLOW_KINDS`'s docstring (`importers/base.py:44-53`) and `CashMovementRow`'s (`ledger/cash.py:17-19`) both state that `amount` is always positive and that direction lives entirely in `kind` — but nothing enforces it at the database level (`db/schema.sql:239`, a bare `NUMERIC NOT NULL`). A negative stored amount would silently flip a movement's direction inside `net_cash` (`ledger/cash.py:51`) rather than raise. A DB `CHECK` is the sounder fix than a read-side guard, since the convention is meant to hold for every writer, not just this one reader. |
| 26 | Two different behaviours for an unknown account id (was three) | **The `snapshot add` third of this row is FIXED, 2026-08-10.** `cmd_snapshot_add` now runs the same `get_account`-then-check-`None` refusal `cmd_reconcile` uses, before `add_snapshot` is called at all (`cli.py:949-958`), so an unknown id exits 2 with a message naming it instead of escaping as a raw `asyncpg.ForeignKeyViolationError` traceback past a `main()` that catches only `OSError`. Pinned by `test_snapshot_add_refuses_an_unknown_account` (`tests/db/test_cli.py`). **What remains is a two-way inconsistency, not three:** `reconcile` refuses cleanly, exit 2 (deliberate, spec §8; `cli.py:1052-1055`) and `snapshot add` now matches it, while `positions` still prints "no open positions" (`cli.py:736`) for a well-formed but nonexistent UUID. The resolution direction is unchanged from #22 — make `positions` stricter, do not loosen the other two — and unchanged in status: still open, still not done here. This subsumes and extends #22 above; cross-reference the two rather than treating them as unrelated. |
| 27 | `reconcile` discards every mark's timestamp, so a verdict computed against six-month-old prices is rendered identically to one computed against this morning's | `latest_marks` returns `(price, as_of)` per instrument precisely so a caller can show a mark's age — its own docstring says the timestamp is "returned, not discarded, so a caller can show a mark's age. A month-old mark rendered identically to a fresh one is a quiet way to mislead" (`db/marks.py:41-43`). `cmd_positions` honours that, rendering `price @YYYY-MM-DD` in the mark column (`cli.py:705`). `cmd_reconcile` throws it away: the dict comprehension at `cli.py:1161` (`{instrument_id: price for instrument_id, (price, _as_of) in raw_marks.items()}`) drops the timestamp on the floor, because `reconcile()` takes a bare `Mapping[UUID, Decimal]`. The result is a `DRIFT` verdict with no hint that the prices behind it are stale — reintroducing exactly the blindness **gap #14** credits `positions` with designing away ("unrealized P&L is only as fresh as the last manual mark… `positions` shows each mark's date alongside the valuation so staleness is visible rather than assumed"). Worse here than in `positions`, because a drift figure is a number a reader will act on, and stale marks are one of the likeliest innocent explanations for it. Deliberately NOT built in the 2026-08-10 fix wave: it is a rendering-and-signature change (either `reconcile()` learns to carry mark ages, or `cmd_reconcile` renders them alongside the `unmarked_instruments` line it already prints), which is scope for its own change, not a review fix. Note the related-but-different case already handled: an instrument with NO mark falls back to cost basis and is counted in `unmarked_instruments` and reported (`ledger/reconcile.py`, `cli.py:1197-1201`). The gap is the *stale* mark, not the missing one. |
| 28 | `net_cash` treats any unrecognised `cash_movement.kind` as an INFLOW, and nothing links the schema's list of kinds to `OUTFLOW_KINDS` | `ledger/cash.py:51` is `total += -m.amount if m.kind in OUTFLOW_KINDS else m.amount` — an `else` branch, not an exhaustive match, so a kind nobody classified is silently added. The three lists that ought to agree do not: `db/schema.sql:236-238` permits **ten** kinds (`deposit`, `withdrawal`, `fee`, `funding`, `interest`, `dividend`, `payout`, `rebate`, `tax`, `return_of_capital`), `OUTFLOW_KINDS` (`importers/base.py:54`) names **three** (`withdrawal`, `fee`, `tax`), and `tests/test_cash.py:25-34` parametrises **seven** — `funding`, `payout` and `return_of_capital` are untested in either direction. The dangerous shape is not today's arithmetic (all three untested kinds are plausibly inflows) but the coupling: a future outflow kind added to the schema's `CHECK` and not to `OUTFLOW_KINDS` is added rather than subtracted, silently, with a 2x error in the wrong direction and no test that would notice. A structural fix — deriving one list from the other, or a membership test asserting every schema kind is classified — is the sound version; a bare parametrise widening would only cover today's ten. |
| 29 | `--as-of` selects WHICH snapshot to compare against; it does not filter the ledger side of the comparison, so cash movements, fills, positions and marks are all read at CURRENT state no matter what date is passed | Raised as CodeRabbit's one Major finding on PR #8 and **declined there, deliberately** — recorded here rather than fixed. The hazard is real: a fill, cash movement or mark written *after* the selected snapshot is included in the computed side while the reported side is the older statement, which can manufacture a false `DRIFT` (a week of ordinary trading since the statement) or mask a real one (a later correction happening to cancel an earlier error). `latest_snapshot` is the only consumer of `as_of` (`db/snapshots.py:52-70`, bound at `cli.py:1060`); `account_cash` (`db/cash.py:40-91`), `open_positions` (`db/positions.py`) and `latest_marks` (`db/marks.py:36-38`) take no `as_of` parameter at all. **Why a partial cutoff would be WORSE than the current honest current-state read, which is the actual reason for declining:** positions cannot be reconstructed at a past instant here at *all*. `db/positions.py` reads current persisted `trade` rows, and the schema keeps no trade history — no valid-time columns, no append-only revision of `open_quantity`/`open_cost_basis`, and `regroup_account` REWRITES those columns in place (`_TRADE_UPSERT_BODY`, `db/trades.py`). Filtering cash and marks by `as_of` while positions stayed current would produce an equity figure that is *more* internally inconsistent than today's, not less: cash as of the statement date, market value as of now, and no way for the reader to tell which line is which. A half-cutoff is the confident-wrong-number shape this project exists to avoid, arrived at by trying to fix a real problem. **What closing it would actually require**, and why it is a subsystem and not a review fix: either trade history (temporal columns on `trade`, or an append-only ledger of grouping results that `open_positions` could read at a timestamp), or point-in-time position reconstruction (re-run the grouper over only the fills executed on or before `as_of` — plausible, since `fill.executed_at` exists, but it means `open_positions` growing an `as_of` parameter and a regroup on the read path, which changes the cost model of every caller). Marks and cash would follow the same cutoff, and only then would the whole comparison share one clock. **What mitigates it today:** the output prints the two clocks separately and labelled — `statement as of <snapshot.as_of>` and `ledger as of <now>` (`cli.py:1186-1187`), pinned by `test_reconcile_labels_the_statement_and_ledger_clocks_separately` (`tests/db/test_cli.py`) — so the mixed-time comparison is visible on the same screen as the verdict rather than implied by a single misleading "as of" header. That makes the gap legible, not absent. | Same family as **#27** (`reconcile` discards each mark's timestamp, so a verdict computed against six-month-old prices renders identically to a fresh one): both are the command being less time-aware than the numbers it prints imply, and both are mitigated only by rendering, not by arithmetic. #27 is the narrower and cheaper of the two — it needs a rendering change, this one needs trade history — so fix #27 first; it also makes this gap more visible while it stays open, since a stale mark is one of the likeliest ways the current-state read produces a surprising number. |

> [!note] Gaps #10 and #11 were done and left on the list
> Both landed in A-2 part 1, not part 2 — they were completed and never struck. The
> evidence: `db/trades.py:377` rolls up `fill.is_estimated` across a trade's allocations
> with `any()` (not `all()` — a single estimated fill taints the whole trade, per spec
> §4); `db/trades.py:55` persists `open_quantity` and `open_cost_basis` on insert and
> `db/trades.py:75-77` repaints them (alongside `is_estimated`) on every regroup's
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
> `positions` groups open trades by **(account, instrument)**, not by instrument alone
> (quantity-weighted average cost basis across trades sharing both an account and an
> instrument) and rolls `is_estimated` up with the same `any()` convention as `trade`.
> `--account` *filters* which rows are shown; it no longer changes what a row means, so an
> unscoped listing can show one instrument more than once -- once per account holding it --
> distinguished by an account-name column. This replaced an earlier instrument-only
> grouping that blended a taxable account's cost basis with a retirement account's (no tax
> consequence in one, the whole tax position in the other -- a blended figure answers no
> question either account actually has) and could manufacture a "mixed direction" position
> that exists nowhere in reality: long in one account and short in another is two ordinary,
> individually valuable positions, not one unvaluable one. Mixed direction *within* a single
> account is still real and is still reported exactly as before. Two separate mechanisms
> decide what a row shows, and they must not be conflated:
>
> - **Structurally unvaluable**, decided by `aggregate_positions` (`ledger/positions.py`)
>   without ever looking at a mark — marks are not even passed to it. It sets
>   `unvaluable_reason` for exactly two conditions: an unknown open quantity (an orphaned
>   trade whose instrument is unreachable), or a direction the contributing trades disagree
>   on (spread, or genuinely mixed long/short). These rows render `n/a (<reason>)`.
> - **Valuable but unmarked**, decided separately in `cli.py`'s `cmd_positions`: when
>   `unvaluable_reason` is `None` but no manual mark exists for the instrument, the row
>   renders `--` in both the mark and unrealized-P&L columns. `unvaluable_reason` stays
>   `None` here — this is not a reason, it is an absence of data.
>
> Both kinds of row are listed rather than hidden — nothing is silently dropped — but only
> the first kind carries a reason. A future reader (`reconcile`, most likely) that filters
> on "does `unvaluable_reason` say anything" and treats everything else as valuable would
> silently mishandle the unmarked case, since it produces no error and no reason string,
> only a missing price.
>
> `reconcile` (gap #13) was unblocked by this work and is now BUILT — `cli.py`'s
> `cmd_reconcile` ships, and gap #13 is struck above. It reads `unvaluable_reason`, never
> `direction`, exactly as the paragraph above warns a future reader must.

---

## Found while designing Fidelity option expiry (2026-08-14)

`Outcome.EXPIRY` (`importers/fidelity.py:125`) closes an expired option at price zero on
its expiry date, and `Outcome.UNSUPPORTED` (`:131`) refuses `ASSIGNED`/`EXERCISED` rather
than guessing at the resulting stock leg. Full design:
[`docs/superpowers/specs/2026-08-14-option-expiry-design.md`](superpowers/specs/2026-08-14-option-expiry-design.md).
Three gaps came out of that work, deliberately deferred rather than fixed here:

| # | Gap | Why it matters |
|---|---|---|
| 30 | **An expiry whose opening fill is absent from the ledger** makes the grouper treat the closing fill as an *opening* one, creating a phantom position at zero cost basis. | `build_expiry_fill` (`importers/fidelity.py:396-476`) trusts that the position it is closing already exists — it has no way to check, since it only ever sees one row at a time. `regroup_account` (`db/trades.py`) pairs fills within an account purely by iterating them in order, so a closing fill with no prior opener becomes the *opener* of a new trade, at a zero cost basis that was never real. Deliberately not defended against: 0 of 27 `EXPIRED` rows across three real accounts and five years are orphaned this way, and because `regroup_account` recomputes every trade from every fill on each run, importing an account's files out of order self-heals the moment the missing year arrives — the phantom trade is silently replaced by the correct one. It is permanent only if one year of an account is imported and the earlier ones never are, which is a real possibility for a five-year account imported piecemeal. |
| 31 | **Corporate actions remain unhandled.** The two long-term real accounts contain `MERGER`, `REVERSE SPLIT`, `NAME CHANGED`, `DISTRIBUTION`, `TRANSFER OF ASSETS ACAT`, `IN LIEU OF`, and a `BUY CANCEL OPENING TRANSACTION`. `ledger/corporate.py` already models several of these action types but is not wired to the importer at all. | An earlier draft of this gap claimed these rows "pass silently while changing share counts" — that is false, and worth correcting rather than quietly not repeating. `reject()` (`importers/fidelity.py:298-321`) blocks on `_carries_money(quantity) OR _carries_money(amount)`, and *quantity* counts, not only amount. Checked directly against the real shapes: `MERGER`, `NAME CHANGED` and `REVERSE SPLIT` each carry a nonzero quantity and so all block; `IN LIEU OF`, whose quantity is zero, blocks on its nonzero amount instead. (The quantities and amounts themselves are deliberately not quoted — this repository is public, they are real transaction figures, and what the claim rests on is which column is nonzero, not what it held.) Every corporate action found in the real exports blocks the commit, and `cmd_import` refuses the entire import with exit 2 (`cli.py:314-325`) the moment one appears — nothing is reclassified into money or silently dropped. The gap is real and it is a hard one: **the two accounts holding these actions cannot be imported at all** until corporate actions are modelled and wired in. That is the safe failure, not the harmful one, and it is why this is a blocker rather than a corruption risk — the same distinction §7 of the option-expiry design draws between this and the pre-fix `EXPIRED` hazard. |
| 32 | **Backdated `as of` correction rows** (`REINVESTMENT as of …`, `FEE CHARGED as of …`) appear in one real account and are not modelled. | Their effect on dating is unexamined — `REINVESTMENT`'s ordinary rule (`importers/fidelity.py:149-150`) and `FEE CHARGED`'s (`:175`) both read `Run Date` as the event's own date, and whether a backdated correction should instead date to the date it corrects is an open question this work did not have real examples of `ASSIGNED`/`EXERCISED` to answer, so it stayed out of scope by the same E1 decision that kept assignment out. |

---

## Found while building corporate-action support (2026-08-15)

Corporate action storage and a cumulative preview (`db/corporate.py`), application at read
time inside `regroup_account` (`db/trades.py`), and `deadband corporate
add`/`list`/`remove` (`cli.py`) all shipped in this branch — for `split` and
`reverse_split` only. The other three `ActionType` members were computed correctly by
`ledger/corporate.py` at the time and **refused by the CLI**, for the reason gap #39
originally recorded, which is why the design's C3 ("all five action types are supported
by the CLI") did not describe what shipped here.

> [!note] The refusal above is gone — 2026-08-16, `feat/materialise-identity-actions`
> `corporate add` now accepts all five `ActionType` members; gaps #38 and #39 below are
> struck or narrowed accordingly, and the section further down
> ("Found while materialising identity-changing corporate actions") records what
> shipped and what is still open. This paragraph is left describing this branch's own
> shipped scope as it stood on 2026-08-15, per this file's convention of keeping a
> session's findings legible rather than silently rewriting them once later work moves
> on — six of the seven rows in the table immediately below are still open; only #39 is
> now closed.

Full design:
[`docs/superpowers/specs/2026-08-15-corporate-actions-design.md`](superpowers/specs/2026-08-15-corporate-actions-design.md).
Seven gaps came out of that work — the five named in the design's §9, plus two the
implementation reviews surfaced — deliberately deferred rather than fixed here:

| # | Gap | Why it matters |
|---|---|---|
| 33 | **Corporate actions still cannot be imported.** | The two long-term accounts remain unimportable (gap #31) until the export's `FROM`/`TO` pairs can be parsed — which needs CUSIP resolution the `instrument` table cannot express, ratio derivation from paired quantities, and a three-row merger case. This branch gives the ledger somewhere to *record* a corporate action once one is known; it does nothing for the importer that would need to *recognise* one from a real export row in the first place, which is the harder half of gap #31 and is exactly as open as it was before. |
| 34 | **Manual trades are not split-adjusted.** | A fill wholly owned by a manual trade is excluded from `regroup_account`'s auto pass — and therefore from `adjust_fills` — before corporate actions are ever applied. `manual_held` computes how much of each fill a manual trade already holds, and a fill whose remaining quantity is exhausted is dropped from the pass entirely (`manual_held` and the loop below it, `db/trades.py:136-153`); the code's own comment names the consequence directly: "fills WHOLLY owned by a manual trade never reach this point... so manual groupings are not split-adjusted" (`db/trades.py:173-175`). A user who hand-groups a trade and then imports a stock split finds that trade's quantity frozen at the pre-split value forever, silently disagreeing with every auto-grouped trade on the same instrument. Fixing it means deciding what a permanent user grouping means across a restatement — does a manual trade's quantity get rewritten at read time the same way a fill's does, or does "manual" mean "frozen as entered," by design? — and that decision was kept out of scope here. |
| 35 | **Merger cash is not modelled.** | `CorporateAction.cash_component` (`ledger/corporate.py:48`) is stored — `add_action` (`db/corporate.py`) will happily persist whatever a caller constructs, though the CLI does not even expose a flag to set it — and never read: `adjust_fills`'s `MERGER` branch (`ledger/corporate.py:184-194`) scales quantity and price by the stored ratio and touches nothing else. A merger paying part cash, part stock — the common real shape — understates the cash received by exactly that amount, with no warning, and the field's mere existence in both the dataclass and the schema invites the assumption that recording it does something. <br><br>**Reachability, updated (2026-08-16, `feat/materialise-identity-actions`).** Before Task 4 of that branch, `corporate add --type merger` was refused outright, so this omission was reachable only by constructing a `CorporateAction` directly or writing to `corporate_action` by hand. Task 4 removed that refusal — `corporate add` now accepts `merger` — so the same defect is reachable by typing a real command: a user recording a real cash-plus-stock merger today gets the stock leg correctly rescaled by the ratio and the cash leg silently missing, with no flag even offered to record it (`cli.py`'s `corporate add` parser has no `--cash-component`). |
| 36 | **No audit trail on restatement — what changed, only that something did.** | `corporate_action` does carry `created_at` (`db/schema.sql:272`), so *when* a row was inserted is recorded — but `remove_action` (`db/corporate.py`) is a hard `DELETE`, so removing an action erases that fact along with the row itself, and `cmd_corporate_list`'s rendering (`cli.py`) doesn't print `created_at` even while the row still exists. More basically, nothing records *what* an action changed: `_print_effect` (`cli.py`) prints the before/after diff once, to stdout, at the moment of `add`/`remove`, and that is the only place the diff is ever visible. This is the same shortcoming gap #15 records for `contract_multiplier` repainting, where the next upsert silently revalues every historical fill with no record of the old value, the new value, or when the change happened — here it recurs one layer up, at the CLI rather than the storage layer. Once the terminal output scrolls away, the only way to reconstruct what a since-modified or since-removed action did to a position is to rebuild the state by hand. |
| 37 | **No database-level uniqueness** on `(instrument_id, ex_date, action_type)`. | `find_duplicate` (`db/corporate.py`) is the only thing standing between a double keypress and a silently wrong position — its own docstring says so: "There is no UNIQUE constraint on the table... so this is an application-level guard." `corporate_action` (`db/schema.sql`) carries `CHECK` constraints on its ratio columns but no uniqueness constraint at all; direct SQL, a future importer, or a bug in `cmd_corporate_add` that skips the check could still double-enter the same split and silently double-adjust every fill it touches. Adding the constraint is a migration and was kept out of scope. |
| 38 | **An action recorded against the *result* of an earlier action regroups nothing.** | Both the CLI's `--commit` path (`_regroup_holders`, `cli.py`) and `preview_effect` (`db/corporate.py`) scope the affected accounts by `fill WHERE instrument_id = <the action's own instrument>`. Because raw fills are never rewritten — `regroup_account` applies `adjust_fills` to an in-memory copy and never writes it back to the fill table (`regroup_account`, `db/trades.py`) — a symbol change from one instrument to another leaves every fill permanently carrying the *source* instrument's id. A later action recorded against the *resulting* instrument therefore matches no fills: the preview reports that nothing is affected and no account is regrouped, while the stored action does genuinely move positions the next time anything else triggers a regroup on that account. Preview and commit agree with each other, so the inconsistency is silent rather than contradictory — there is no error, and no screen where the two disagree. <br><br>**The position-reporting half of this gap is FIXED, 2026-08-16 (`feat/materialise-identity-actions`, Task 2).** `open_positions` now resolves a position's instrument via `COALESCE(t.effective_instrument_id, f.instrument_id)` (`db/positions.py:71`) rather than the raw opening fill alone, and `regroup_account` writes `effective_instrument_id` from the trade's *adjusted* opening fill, not the raw one (`db/trades.py:384-389`). A mark set on the resulting symbol now prices the position, and `deadband positions` agrees with `deadband trades` -- both asserted rather than merely reasoned about, by `test_a_mark_on_the_new_symbol_prices_the_position_after_a_symbol_change` and `test_trades_and_positions_agree_on_the_symbol_after_a_symbol_change` (`tests/db/test_cli.py`), added 2026-08-16. What remains above is the scoping half only — unchanged. <br><br>**Reachability, updated.** The refusal that made this scoping bug unreachable by typing is gone: `corporate add` now accepts `merger`, `spinoff` and `symbol_change` (see gap #39 below for what materialising them still leaves open). A `symbol_change` from one instrument to another followed by any later action recorded against the resulting instrument is therefore a sequence a user can type today with the shipped CLI, not only something reachable via `db.corporate.add_action` called from outside it or via direct SQL. |
| 39 | ~~Identity-changing actions (merger, spinoff, symbol change) cannot be materialised into `trade`, and the CLI refuses them~~ — **FIXED, 2026-08-16** (`feat/materialise-identity-actions`). Both halves named below are now false: `trade.effective_instrument_id` (Task 1's schema, Task 2's write path) closes the reporting half — see gap #38's FIXED note above — and spinoff children are persisted to a new `derived_fill` table (Task 1's schema, Task 3's write path) instead of raising `ForeignKeyViolationError`, with provenance recovered by inverting `_spinoff_fill_id` over a lazily-expanded closure so chained spinoffs resolve too (`db/trades.py:199-268`). `cmd_corporate_add` no longer refuses `merger`, `spinoff` or `symbol_change`; all five `ActionType` members write and regroup. What is left is what materialising a derived fill leaves unaddressed, not whether it can be done at all. | **(a) `derived_fill` is invisible to the CLI.** `cli.py` never references `derived_fill`, `derived_from_fill_id` or `opening_derived_fill_id` — a user looking at a spun-off position in `deadband positions` or `deadband trades` has no command that explains where it came from, only `corporate list` (`cmd_corporate_list`, `cli.py`), which prints the *action* (id, ex-date, symbol, ratio, resulting symbol, basis allocation) but nothing naming which trade or position it produced. **(b) The derived-id invariant is a convention, not a constraint.** `regroup_account` identifies a derived fill by set difference against the ids it fetched — `derived = [f for f in fills if f.id not in real_ids]` (`db/trades.py:183-197`) — which is sound only while spinoff is the sole action type `adjust_fills` lets mint a new id; `merger` and `symbol_change` both keep a fill's original id under `dataclasses.replace` (`ledger/corporate.py:174-194`), and nothing in the schema states that only spinoff may do otherwise. `test_only_a_spinoff_mints_a_fill_id` (`tests/db/test_trades.py`) pins the assumption with a test — which catches a regression — but does not make it a constraint the schema can express: a future action type that mints an id without also updating this comment and the set-difference logic would have its output silently misfiled as a real fill. |

---

## Found while materialising identity-changing corporate actions (2026-08-16)

`derived_fill` (Task 1's schema), `trade.effective_instrument_id` (Task 1's schema, Task
2's write path) and persisted spinoff children with recovered provenance (Task 3) let
`corporate add` accept all five `ActionType` members — gaps #38 and #39 above are struck
or narrowed accordingly. Full design:
[`docs/superpowers/specs/2026-08-16-materialising-identity-changing-actions-design.md`](superpowers/specs/2026-08-16-materialising-identity-changing-actions-design.md).
Its own §9 names five gaps; three of them are this project's existing #33 (importing is
still out of scope), #35 (merger cash) and #36 (no audit trail). The other two are the
residue recorded as #39 above. Accepting `merger` did change #35's reachability from
theoretical to real — recorded as a Reachability paragraph on #35 itself, matching #38's
structure, rather than as a separate row, since it is a change to an existing gap and not
a new one. What follows is new: two invariants the implementation reviews surfaced that
the design didn't anticipate.

| # | Gap | Why it matters |
|---|---|---|
| 40 | **For a chained spinoff, `derived_fill.derived_from_fill_id` records the lineage root, not the immediate parent.** | A spinoff whose source instrument is another spinoff's resulting instrument applies to the first spinoff's synthetic child as well as to any real holding of it (`test_a_spinoff_off_a_spun_off_child_is_attributed_to_the_lineage_root`, `tests/db/test_trades.py:1397-1458`). What gets stored for the grandchild is the real fill the chain started from, not the intermediate derived fill — because `derived_from_fill_id` references `fill(id)` (`db/schema.sql:305`), and an intermediate derived fill has no `fill` row behind it: storing the immediate parent directly is not merely undesirable but unstorable, verified to raise `ForeignKeyViolationError` on `derived_fill_derived_from_fill_id_fkey` (`db/trades.py:241-250`). Nothing is lost that can't be reconstructed — `corporate_action_id` names the exact action, and the stored action set gives every intermediate step — but recovering the immediate parent directly would need a `derived_from_derived_fill_id` column with a `num_nonnulls(...) = 1` CHECK (`db/schema.sql:215`, `trade_fill_one_source_chk`), mirroring `trade_fill`'s existing one-source pattern. |
| 41 | **The synthetic id hash is not injective in the action.** | `_spinoff_fill_id` hashes only `(parent_fill_id, resulting_instrument_id, ex_date)` (`ledger/corporate.py:32-37`) and not the source instrument, so two spinoff actions that differ only in their source but share a resulting instrument and ex-date mint the same synthetic id for a given parent. The provenance map is first-writer-wins by construction — `if child_id in derived_provenance: continue` (`db/trades.py:259-260`) — so it would attribute the child to whichever action sorts first in `actions_with_ids_for_instruments`'s `ORDER BY ex_date` fetch (`db/corporate.py`). No test in this branch constructs that case — it is recorded as a gap, not a reproduced failure. Disambiguating it means adding `instrument_id` to the hash, which re-mints every existing derived fill's id, since that id is `derived_fill`'s primary key and the `ON CONFLICT` target that keeps a live derived fill's identity stable across regroups (`db/schema.sql:291`, `db/trades.py:306`) — closing this is a migration, not a patch. |

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
