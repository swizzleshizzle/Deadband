# Unblocking the Fidelity history importer: four non-corporate verbs

**Status:** design, approved 2026-08-18.
**Issue:** [#17](https://github.com/swizzleshizzle/Deadband/issues/17) — the gap #31 remainder.
**Scope:** branch A of two. Branch B (`TRANSFER OF ASSETS ACAT`) has its own spec and
is deliberately not designed here — see §8.

## 1. What the real exports actually contain

Every claim in this section came from running `FidelityImporter.parse()` over all
eleven real history files and reading the rows it refused, not from reasoning about
fixtures. That distinction is not stylistic: the single highest-value check on the
preceding branch disproved a claim that had survived design, spec, and four
implementation tasks, and it was a real-data run that did it.

**Nine rows block, in five of the eleven files, across five verb families.** Four of
those families are in scope here; the fifth (`TRANSFER OF ASSETS ACAT`) is branch B.

| Verb prefix | Rows | Quantity | Amount | What it is |
|---|---|---|---|---|
| `ROLLOVER CASH CHECK` | 3 | zero | inflow | cash arriving in a retirement account |
| `EARLY DIST` | 1 | zero | outflow | cash leaving one |
| `DISTRIBUTION` (no `SPINOFF`) | 2 | positive | positive | **shares** received; see §1.1 |
| `BUY CANCEL OPENING TRANSACTION` | 1 | negative | inflow | one leg of an amendment; see §1.2 |
| `TRANSFER OF ASSETS ACAT` | 2 | one negative, one zero | outflow | branch B |

Figures are described by sign and column rather than quoted. This repository is
public and these are real transaction values; what every claim below rests on is
which column is non-zero and in which direction, never what it held.

### 1.1 A plain `DISTRIBUTION` delivers shares, not money

The row carries a positive `Quantity` **and** a positive `Amount ($)`, which reads at
first glance as shares and cash together. It is not. Verified by cash-balance
continuity in both accounts independently: across the distribution row the
`Cash Balance ($)` column does not move by the stated amount — in one account it does
not move at all, and in the other the only movement that day is an unrelated dividend
that accounts for the change exactly. **`Amount ($)` on this row is the market value
of the shares received.** No money changed hands.

Two further facts confirm the reading. The same event appears in both accounts on the
same date with different share counts, and — verified by recomputing each account's
holding from its own file rather than assumed — the shares received stand in exactly
the same proportion to the shares held in both. That is a ratio-based distribution,
not a per-account payment. And in the
account whose entitlement did not land on a whole share, a separate
`IN LIEU OF FRX SHARE` row follows days later paying the fractional remainder in cash,
at a per-share value consistent with the distribution's own implied price. A cash
distribution would not need one.

So this is a share distribution — an ADR ratio change — and belongs to the split
family, not to the cash rules.

### 1.2 The cancelled buy is one leg of a three-row amendment

The blocking row is not a stray. It sits in the middle of a cluster on one option
contract, all three rows carrying the same `as of` date:

1. the original `YOU BOUGHT OPENING TRANSACTION`, on the as-of date;
2. a `BUY CANCEL … CXL … CANCELLED TRADE as of <date>`, nineteen days later,
   reversing it exactly — same price, negated quantity, negated fees;
3. a `YOU BOUGHT … CORR … CORRECTED CONFIRM as of <date>`, same day as (2),
   re-booking it with a corrected fee.

The net truth is **one** buy, on the as-of date, at the corrected fee.

**This cluster is a live correctness defect today, independent of the blocking
policy.** `classify()` returns `None` for the `CORR` row, but the dedicated
`YOU BOUGHT`/`YOU SOLD` branch matches it on leading text anyway, so the importer
already emits a **third** fill for this contract — two buys and one sell where one buy
and one sell occurred. Nothing gates it: the blocking check never sees the row,
because the row is not unmapped. The only reason a phantom open contract has not
reached the ledger is that the sibling `CXL` row blocks the whole file.

Unblocking `BUY CANCEL` naively makes this worse rather than better. The cancel is
dated nineteen days after the buy it reverses, and the position's real closing sell
falls between the two — so a literal import produces `buy, sell, cancel, re-buy`,
which `regroup_account` pairs into a completed trade followed by a **phantom short**
with fabricated P&L.

Across all eleven files this cluster is a singleton: one `CXL`, one `CORR`. The wider
`as of` population is 45 rows, of which 27 are `EXPIRED` and already handled and 16
are non-blocking cash rows (§8).

## 2. Decisions

**D1 — Retirement cash flows map to the generic kinds.** `ROLLOVER CASH CHECK` →
`deposit`, `EARLY DIST` → `withdrawal`. Four existing rules (`CASH CONTRIBUTION`,
`CO CONTR`, `PARTIC CONTR`, `CONTRIBUTIONS`) already collapse retirement-flavoured
verbs onto the generic kinds, and `cash_movement.kind` is a CHECK constraint that
admits no retirement-specific value. A later tax-reporting feature would want the
distinction back; it is recoverable from the stored note, and inventing a kind now
would mean a migration this branch is otherwise free of.

**D2 — A plain `DISTRIBUTION` is proposed as a `split`, and never stored.** Total cost
basis is unchanged and spreads over more shares; no gain is realised on receipt. This
inherits the preceding branch's propose-don't-store posture wholesale: a corporate
action silently restates history across every account holding the instrument, and
`corporate add` previews by default and refuses duplicates precisely to force a human
decision first.

The alternative — treating it as a taxable stock dividend, with the received shares
entering at market value as fresh basis and that value booked as income — is defensible
accounting and may even be the correct tax treatment. It is rejected here because it
requires an income concept the ledger does not have, and because it changes realised
P&L on every subsequent sale of the instrument. Choosing the basis-preserving reading
keeps this branch free of both a migration and a P&L change.

**D3 — Amendment clusters are netted, and the survivor is dated to its `as of` date.**
The ledger states what happened, not what the broker's paperwork did. Rejected: a
literal import of all three rows, which produces a same-day round trip whose P&L is
pure fee noise and whose same-day ordering decides whether you get that or a phantom
short; and dropping the `CXL`/`CORR` pair while keeping the original, which silently
loses the fee correction and leaves the ledger disagreeing with the broker's own cash
balance.

**D4 — Netting degrades to blocking, never to guessing.** A `CXL` with no matching
original, or a `CORR` with no matching `CXL`, is not netted. Those rows stay unmapped
and block the import, exactly as they do today. The matcher is fitted to a single real
cluster, so the failure mode it is allowed to have is refusing to act — not acting on
a partial match.

**D5 — Recognition is shape-guarded where the verb alone is not evidence.** Only a
plain `DISTRIBUTION` carrying a **positive quantity** is a share distribution. A
`DISTRIBUTION` with zero quantity has never been observed in the real exports and must
keep blocking rather than being proposed as a split derived from no shares. Two
observed rows is thin evidence to generalise a verb from; the guard is what keeps the
generalisation honest.

## 3. Recognition

Three rules are added to `RULES` in `importers/fidelity.py`:

```python
Rule("rollover_deposit",   "ROLLOVER CASH CHECK", Outcome.CASH, cash_kind="deposit"),
Rule("early_distribution", "EARLY DIST",          Outcome.CASH, cash_kind="withdrawal"),
Rule("share_distribution", "DISTRIBUTION",        Outcome.CORPORATE_ACTION),
```

**Ordering is load-bearing for `share_distribution`, and only for it.** `classify()`
matches with `startswith` and returns the first hit, and `"DISTRIBUTION"` is a proper
prefix of `"DISTRIBUTION SPINOFF"`. The new rule must therefore sit **after**
`spinoff_distribution`; placed before it, every spinoff in every export silently
reclassifies as a share distribution. This is unlike the existing corporate-action
block, whose comment records that its position within `RULES` is not load-bearing —
that comment must not be read as covering the new rule.

`test_every_rule_is_reachable` is the existing guard against shadowing. The mutation
that proves it guards *this* pair is swapping the two rules and confirming a test
fails.

`ROLLOVER CASH CHECK` is used as the verb rather than the shorter `ROLLOVER`: both
observed variants (one of which carries a trailing `MOBILE DEPOSIT`) share that
prefix, and the narrower prefix does not speculate about other `ROLLOVER` verbs the
exports have never shown.

## 4. Netting an amendment cluster

A new cross-row pass in `parse()`, running beside `_group_corporate_actions` and
before fills are emitted. It operates on rows, not on already-built fills, because the
rows it suppresses must never reach `build_fill`.

**Identify.** A candidate carries an `as of <date>` marker together with a
cancellation marker (`CXL` or `CANCELLED TRADE`) or a correction marker (`CORR` or
`CORRECTED CONFIRM`).

**Match.** A cancellation is matched to its original on the tuple
`(symbol, as-of date, absolute quantity, price)`, and a correction is matched to the
cancellation on `(symbol, as-of date)`. Both matches must be unique; an ambiguous
match is treated as no match.

**Emit.** On a complete original → cancel → correct chain: the original and the
cancellation are suppressed, and the correction is emitted as an ordinary fill dated
to its **as-of date**, carrying its own corrected price and fees.

**Otherwise.** Any incomplete or ambiguous chain is left entirely alone. Its rows
reach the existing paths and, being unmapped and money-carrying, block the import
(D4).

A consequence worth stating: once the pass suppresses the `CORR` row's own path, the
duplicate-buy defect of §1.2 is fixed. Any test written for this section must
therefore assert the **fill count** for the affected contract, not merely that the
import stops refusing — a test that only checks the refusal would pass while the
duplicate persisted.

## 5. Proposing the share distribution

The share distribution reuses the preceding branch's proposal path unchanged in shape.
It produces a `CorporateActionProposal` with `kind="split"`, and its ratio cannot come
from the row: the row states the shares **received**, never the holding they were
received on. The ratio is therefore completed from the ledger, exactly as spinoff
ratios already are:

```
ratio = (quantity held at ex-date + quantity received) : (quantity held at ex-date)
```

reduced to lowest terms, and read under the same `HAVING SUM(...) > 0` long-position
rule that the existing completion uses — a distribution is received on shares you are
long, and a net-short or flat holding does not qualify.

When the holding is not in the ledger — typically because the year-file containing the
purchase has not been imported — the completion **reports and stops**: the ratio
renders as `UNAVAILABLE` with the reason, and the command renders `--ratio <FILL IN>`.
It never substitutes a guess. This mirrors the existing spinoff behaviour rather than
inventing a second convention.

Rendered shape, with a fabricated symbol and a fabricated ratio:

```
corporate add --type split --symbol <SYMBOL> --ex-date 2026-03-02 --ratio 5:3
```

`corporate_action.action_type` already admits `'split'`, so no schema change is needed.

The fractional-share cash that accompanies such a distribution already lands in
`ImportBatch.cash_in_lieu` and prints under its own heading with gap #43's caveat that
it is recognised but not applied. That path is unchanged.

## 6. Reporting

No new output section. The share distribution joins the existing corporate-action
report; the two retirement verbs become ordinary cash movements and are reported the
way every other cash movement is; the netted amendment produces one fill and no
commentary beyond the ordinary import summary.

One addition is required: when a cluster is netted, the import summary must say so —
which rows were suppressed and which date the survivor was assigned. A netting that
happens silently is indistinguishable from rows being dropped.

## 7. Testing

**Fixtures.** Rows for all four verbs are added to
`tests/fixtures/fidelity/real_shape_history.csv`, using fabricated symbols only. The
amendment cluster gets its own three-row group. No real symbol, quantity, price, or
account number appears in any fixture — the deny-list and pre-commit hook guard
identifiers, not values, so this is a judgment the author must make, not one the hook
will catch.

**Mutation gate, on every new test.** At minimum:

- swap `share_distribution` before `spinoff_distribution` (§3)
- relax the D5 shape guard to admit zero quantity
- drop the as-of re-dating so the corrected row keeps its `Run Date`
- break the cancellation↔original match so netting fires on a partial chain
- flip the ratio orientation in §5 from `(held + received):held` to `held:(held + received)`

Each must be CAUGHT. A SURVIVED mutation is reported as such, not quietly re-run.

**The check that decides the branch.** Run the importer over the real `imports/`
directory and assert that four of the five previously-blocked files reach **zero**
blocking rows, and that the remaining file blocks on `TRANSFER OF ASSETS ACAT` alone.
This is not a unit test and does not live in the suite — the real files are
gitignored — but it is the acceptance criterion, and its result is recorded in the
branch report.

**Regression surface.** `regroup_account` is not modified, but the amendment netting
changes which fills reach it, so `tests/db/test_positions.py` and
`tests/db/test_trades.py` run alongside `tests/db/test_cli.py`. Every DB test run
loads `TEST_PG_DSN` first and the summary line is read to confirm no skips — a green
run without it silently skips roughly sixty tests.

## 8. Out of scope

**`TRANSFER OF ASSETS ACAT` — branch B.** The decision is already taken: an outbound
transfer closes the position with **zero realised P&L**, because the cost basis leaves
with the shares. The row's amount is a market value, not a transaction price, so
booking it as a sale would fabricate realised P&L that never occurred. It is excluded
here because it is the only one of the five that cannot be built on the existing
schema: `derived_fill` declares both `corporate_action_id` and `derived_from_fill_id`
`NOT NULL`, so it is bound to corporate actions by construction and a transfer is not
one, and `cash_movement.kind`'s CHECK constraint admits no transfer kind. Branch B is
a new ledger concept plus a migration, and gets its own design.

**The remaining `as of` rows.** D3 settles the as-of dating question for amendment
clusters only. The 16 `REINVESTMENT`, `FEE CHARGED`, `FOREIGN TAX PAID` and
`DIVIDEND RECEIVED` rows carrying `as of` do not block, are not amendments, and keep
`Run Date` unchanged. Gap #32 stays open and stays accurate.

**Incremental imports.** Unchanged and still unsolved; gap #46 covers it.

## 9. Gaps this design creates

1. **The amendment matcher is fitted to one real cluster.** Same fragility class as
   gap #45's `#REOR` grouping: it is derived from a single observed example, and a
   different amendment shape — a cancel with no correction, a correction that changes
   quantity rather than fees, a multi-leg amendment — falls through to blocking rather
   than being handled. That is the designed failure mode (D4), but it is a real limit.

2. **A cash-only `DISTRIBUTION` blocks.** D5's guard is deliberate, and the cost is
   that the first such row encountered refuses an import rather than importing as
   cash. Reversing that judgment needs a real example to reason from.

3. **The split reading of a share distribution is a choice, not a derivation.** D2
   picks basis-preservation over the taxable-stock-dividend treatment. If the
   distribution was in fact taxable income, the ledger's basis and every subsequent
   realised gain on that instrument are wrong in a way nothing in the import will
   surface.

4. **Netting makes the ledger disagree with the broker's row count.** After D3 the
   ledger holds one fill where the export holds three rows. Reconciling a Deadband
   trade back to a broker statement line-by-line now requires knowing that netting
   happened, which is why §6 requires the summary to say so.
