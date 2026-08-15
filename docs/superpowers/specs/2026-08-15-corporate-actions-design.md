# Deadband — Recording and applying corporate actions

**Date:** 2026-08-15
**Status:** Design approved
**Depends on:** Subsystem A (ledger), `ledger/corporate.py`
**Scope:** Storing corporate actions and applying them to grouped trades. Importing them
from a broker export is **out of scope** — see §9.

---

## 1. Context

`ledger/corporate.py` is finished. It models five action types (`SPLIT`, `REVERSE_SPLIT`,
`MERGER`, `SPINOFF`, `SYMBOL_CHANGE`), it has a `CorporateAction` dataclass with real
validation, and `adjust_fills(fills, actions)` is covered by roughly forty tests. The
`corporate_action` table exists in `db/schema.sql` with every column those need.

And none of it is connected to anything. There is no `db/corporate.py`, no CLI command,
and — verified by grep — **`adjust_fills` is never called anywhere in production code.** It
is exercised only by its own test file. This is the same shape gap #13 had before
`reconcile`: a complete pure layer, a table, and no wiring.

The cost is concrete. Michael's real exports contain six corporate actions across three
accounts and four years: two reverse splits, two name changes, one merger and one spinoff.
Because the importer does not recognise those rows, and because every one carries a nonzero
quantity, each lands in `ImportBatch.blocking` and refuses the entire import (gap #31). Two
of his three accounts therefore cannot be imported at all. Even once they can be, a reverse
split that is recorded but never applied leaves the ledger believing he holds 1,800 shares
of something he holds 300 of.

### What this design deliberately is not

Parsing corporate actions out of the Fidelity export is a **separate, larger problem** and
is not attempted here. The rows come in `FROM`/`TO` pairs identified by **CUSIP, not
ticker**, usually with an empty `Symbol` column; the `instrument` table has no CUSIP field;
the ratio is stated nowhere and must be derived from the paired quantities; and a merger
appears as three rows rather than two. That work depends on this one and gets its own spec.

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| C1 | The adjustment is **derived at read time**, never baked into stored fills. | Fills are ground truth; a corporate action is a separate fact; the adjusted view is a consequence of both. It is also what makes removal a genuine undo rather than a second restatement. |
| C2 | `adjust_fills` is wired into **`regroup_account` only**, between `fetch_fills` and `group_fills`. | See §4. Adjusting inside `fetch_fills` would also adjust callers that must see raw truth. |
| C3 | **All five action types** are supported by the CLI. | `adjust_fills` already implements them and is heavily tested, so the marginal cost is argument validation, not domain logic. Four of Michael's six real actions are splits or name changes; deferring merger and spinoff would leave two of his positions permanently wrong. |
| C4 | `add` **previews by default** and writes only with `--commit`; `list` and `remove` exist. | A corporate action is global and silently restates history. This project has recorded that failure shape twice — gap #15 (repainting a multiplier restates every fill with no record) and gap #21 (a snapshot cannot be deleted). A third instance, this one spanning every account holding the instrument, is not acceptable. |
| C5 | The preview shows the **cumulative** effect, not the new action in isolation. | See §5. An isolated preview would render a duplicate entry indistinguishable from a first entry. |
| C6 | `add` **refuses a duplicate** `(instrument_id, ex_date, action_type)`. | Entering the same 1:6 reverse split twice applies it twice — a 1:36 restatement that looks plausible at every individual step. |
| C7 | `add --commit` and `remove` **regroup every account holding the instrument**, in one transaction. | Positions come from materialised `trade` rows, so they are silently stale otherwise. Matches `import --commit`, which already wraps insert-plus-regroup. |
| C8 | **Merger cash is not modelled.** | `CorporateAction.cash_component` exists as a field that `adjust_fills` never reads. Making it work means changing the one complete, battle-tested piece of this subsystem, which is a different risk from wiring it up. Recorded as a gap. |

---

## 3. What ships

| File | Responsibility |
|---|---|
| `db/corporate.py` | **new** — `add_action`, `list_actions`, `remove_action`, `actions_for_instruments` |
| `db/trades.py` | modify: `regroup_account` fetches actions and calls `adjust_fills` |
| `cli.py` | modify: `corporate add` / `corporate list` / `corporate remove` |
| `docs/known-gaps.md`, `README.md` | modify |

No schema change. `corporate_action` already has `id`, `instrument_id`, `action_type`,
`ex_date`, `ratio_numerator`, `ratio_denominator`, `resulting_instrument_id`,
`cash_component`, `basis_allocation`, `note`.

---

## 4. Applying the adjustment

`regroup_account` currently: resolves the account, computes `manual_held` (how much of each
fill a manual trade already claims), fetches fills, reduces each by the manual holding,
drops any reduced to zero, and hands the remainder to `group_fills`.

The adjustment goes **after the manual reduction and before `group_fills`**:

```
fetch_fills → reduce by manual_held → adjust_fills(remainder, actions) → group_fills
```

**The order is load-bearing and the reverse is a bug.** `trade_fill` quantities were
recorded in the units that existed when the manual grouping was made — pre-split units. If
adjustment ran first, a fill reduced from 1,800 shares to 300 would then be compared
against a manual holding of 1,800, yield a negative remainder, and be dropped entirely. The
fill would vanish from the ledger.

**Manual trades are therefore not split-adjusted**, because fills wholly owned by one never
reach the grouper at all — `regroup_account` skips them before this point. That is a real
limitation and is recorded as a gap rather than solved here: correcting it means deciding
what a permanent user grouping *means* across a restatement, which is a design question of
its own.

`actions_for_instruments(conn, instrument_ids)` fetches every action for the instruments the
account's fills touch. Actions are global — the table has no `account_id`, which is correct,
since a split affects every holder.

---

## 5. The preview

`corporate add` without `--commit` opens no write transaction and prints what would change:

```
$ deadband corporate add --type reverse_split --symbol ZXCO \
      --ex-date 2026-03-02 --ratio 1:6

  ZXCO — reverse split 1:6, ex 2026-03-02
  3 fill(s) affected across 1 account
    1800 sh @ 0.05  ->  300 sh @ 0.30
  preview only — rerun with --commit to write
```

`--ratio NEW:OLD` maps to `ratio_numerator:ratio_denominator`, in the direction
`adjust_fills` already consumes: a quantity is scaled by `numerator / denominator`. So a
1-for-6 reverse split is `--ratio 1:6` and scales 1,800 shares to 300; a 3-for-1 forward
split is `--ratio 3:1`. Stating this is not pedantry — inverting it turns a reverse split
into a 6× forward split, and the resulting position would be wrong by a factor of 36 while
every individual step still looked plausible.

**The figures are the cumulative difference**, computed as `adjust_fills(fills, existing +
new)` against `adjust_fills(fills, existing)` — not the new action applied to raw fills.
The distinction is the whole point: with an isolated preview, entering the same reverse
split a second time would print exactly the same plausible `1800 → 300` while the stored
state silently became `1800 → 50`. C6's duplicate refusal makes that specific case
impossible, but the cumulative framing is what keeps the preview honest for *any*
interacting pair of actions — a split and a later symbol change on the same instrument, for
instance.

`corporate list` prints stored actions with their ids, filtered by `--symbol` if given.
`corporate remove <id>` deletes one and regroups, and its own preview shows the reverse
difference.

---

## 6. Failure policy

| Condition | Outcome |
|---|---|
| Unknown or ambiguous `--symbol` | Refuse, exit 2. Reuses `db/marks.py`'s `resolve_instrument_by_symbol`, which already refuses ambiguity by naming every candidate — `instrument.symbol` is not unique. |
| `--type` requires a resulting instrument (`merger`, `spinoff`, `symbol_change`) and none given | Refuse, exit 2, before opening a write transaction. |
| `spinoff` without `--basis-allocation` | Refuse, exit 2. `CorporateAction.__post_init__` already enforces this; the CLI must fail cleanly rather than surface a `ValueError`. |
| Ratio component not a positive finite `Decimal` | Refuse, exit 2. Catch `InvalidOperation`, which is not a `ValueError` subclass, and reject non-finite with `is_finite()` — the same pair of guards `cmd_marks_set` and `cmd_snapshot_add` already carry. |
| Duplicate `(instrument_id, ex_date, action_type)` | Refuse, exit 2, naming the existing action's id so it can be inspected or removed. |
| `remove` with an unknown id | Refuse, exit 2. |
| Action on an instrument with no fills | Allowed. Prints that nothing is affected — a legitimately pre-recorded future action. |

Refusals write nothing and open no transaction.

---

## 7. Testing

- **The wiring is what is new; the arithmetic is not.** `adjust_fills` is already tested.
  The tests that matter here are that a stored action *reaches* it and that the result
  reaches `trade`: a fill of 1,800 shares plus a stored 1:6 reverse split yields a position
  of 300, and removing the action returns it to 1,800.
- **The ordering against `manual_held`** — a fill partly claimed by a manual trade, plus a
  split, must not vanish. Reversing the two steps must turn this red.
- **The cumulative preview** — previewing a second action on an instrument that already has
  one shows the incremental difference, not the new action against raw fills.
- **Duplicate refusal**, and that the refusal writes nothing.
- **Regroup reaches every account holding the instrument**, not only one.
- Every refusal in §6 exits 2 and leaves the database untouched.
- The test database is shared and persistent: scope every assertion to rows the test
  created, and probe only through the transaction-rolled-back `conn` fixture.
- Every new test is gated against a mutant.

---

## 8. Hygiene

The repository is public and `imports/` holds real exports. Fixtures use **fabricated**
symbols. The deny-list guards identifiers, not values, so before committing anything
derived from a real export, diff its numeric tokens against that export. A real option
symbol reached a tracked spec on the previous branch precisely because it looked like an
illustration; the ratios and quantities in this document are drawn from the real data's
*shape* and must not be reproduced with their real instruments attached.

---

## 9. Known gaps this design creates

1. **Corporate actions still cannot be imported.** The two long-term accounts remain
   unimportable (gap #31) until the export's `FROM`/`TO` pairs can be parsed — which needs
   CUSIP resolution the `instrument` table cannot currently express, ratio derivation from
   paired quantities, and a three-row merger case.
2. **Manual trades are not split-adjusted**, because fills wholly owned by one never reach
   the grouper. Fixing it requires deciding what a permanent user grouping means across a
   restatement.
3. **Merger cash is not modelled.** `cash_component` is stored and ignored. A merger paying
   cash understates cash by that amount, and the field's existence invites the assumption
   that it works.
4. **No audit trail on restatement.** Adding or removing an action silently changes every
   affected position and realised figure. `list` shows what is currently stored, but nothing
   records when an action was added or what it changed — the same shortcoming gap #15
   records for `contract_multiplier` repainting.
5. **No database-level uniqueness** on `(instrument_id, ex_date, action_type)`. C6's refusal
   is enforced in application code only; a concurrent writer or direct SQL could still
   double-enter. Adding the constraint is a migration and was kept out of this scope.
