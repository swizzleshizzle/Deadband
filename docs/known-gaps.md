# Known gaps carried out of A-1

Everything here was found during A-1's implementation, adjudicated deliberately, and
deferred rather than forgotten. Each entry says what is wrong, whether it is reachable
today, and when it must be fixed.

Ordered by when they must be addressed, not by severity.

---

## Must land before subsystem C

### Fee allocation on partially-closed trades is wrong

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

- **Cross-batch same-day identical trades still collapse.** Two genuinely distinct
  identical trades on one day, split across exports that never both contain the pair,
  dedupe into one. Unfixable without a venue-supplied intra-day ordinal. Documented in
  `commit_batch`'s docstring.
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
