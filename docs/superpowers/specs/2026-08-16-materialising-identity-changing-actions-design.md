# Deadband — Materialising identity-changing corporate actions

**Date:** 2026-08-16
**Status:** Design approved
**Depends on:** PR #10 (`corporate add/list/remove`), `ledger/corporate.py`
**Closes:** issue #11, gap #39. Narrows gap #38.
**Unblocks:** issue #12 (importing corporate actions)

---

## 1. Context

PR #10 wired `adjust_fills` into `regroup_account` and put a CLI in front of it, then
refused three of the five action types because they could not be persisted. `split` and
`reverse_split` ship working. `merger`, `spinoff` and `symbol_change` exit 2 with a
message naming gap #39.

The refusal was correct and the engine is not the defect: `ledger/corporate.py` computes
all five types and is covered by roughly forty tests. What fails is materialising the
last three into `trade` rows, and it fails in two unrelated ways that have been
conflated under one gap number.

**Verified against a real database, each case in its own rolled-back transaction:**

| Type | Behaviour before the refusal landed |
|---|---|
| `spinoff` | `ForeignKeyViolationError` on `trade_opening_fill_fk` |
| `symbol_change` | position reports under the **old** instrument |
| `merger` | position reports under the **old** instrument |

### The two problems

**Reporting.** `open_positions` resolves a position's instrument through
`LEFT JOIN fill f ON f.id = t.opening_fill_id` → `f.instrument_id`
(`db/positions.py:36-37`). Adjustments are derived at read time and never rewrite
`fill`, so that join returns the *source* instrument — while `regroup_account` writes
`trade.primary_underlying` from the *adjusted* fill. `deadband positions` and
`deadband trades` therefore disagree, and because marks are keyed off the position's
instrument, a mark on the new symbol never prices the position. This affects
`symbol_change` and `merger`.

**Identity.** `adjust_fills` emits a second, synthetic fill for a spinoff's child with
`id=_spinoff_fill_id(f.id, action)` — a deterministic `uuid5` (`ledger/corporate.py:215`).
No `fill` row exists for it, and both `trade.opening_fill_id` and `trade_fill.fill_id`
are **non-deferrable composite** foreign keys into `fill (id, account_id)`
(`db/schema.sql:142-144`, `:164-165`). The insert fails. This affects `spinoff` alone.

The distinction matters because `symbol_change` and `merger` use
`dataclasses.replace(f, instrument_id=...)` and **keep the fill's id**. They never
violate a foreign key. Only spinoff mints an identity.

### Root cause

The previous design's C1 (adjustments derived at read time over a `fill` table that is
never rewritten) and C3 (all five types supported) are incompatible for the types that
change a fill's **identity** rather than its **magnitude**. C3's stated justification —
"the marginal cost is argument validation, not domain logic" — is wrong, and is left
unamended in that document as the historical record.

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | `fill` stays **pure**. No derived row is ever written to it, marked or otherwise. | The invariant that survives is the one the schema enforces. "Fills are ground truth unless flagged" is a sentence nobody can apply consistently at 2am. |
| D2 | The effective instrument is a **stored column** on `trade`, written by `regroup_account`. | The alternative resolves the action chain in SQL, reimplementing `_ordered_actions`' ordering and ex-date comparison in a second language where it will drift from the Python that defines it. `regroup_account` is the column's only writer and already recomputes everything. |
| D3 | Spinoff children live in a **new `derived_fill` table**, not in `trade` alone. | Keeping identity only on `trade` (no `trade_fill` rows for the child) hollows out the audit trail for exactly the trades hardest to explain — the ones conjured from an action rather than something the user did. |
| D4 | `derived_fill` is **regenerated on every regroup**, never user-authored. | It is a projection of (fills × actions). Making removal a genuine undo requires that nothing derived outlives the action that produced it. |
| D5 | A symbol change **relabels everything** — open positions, closed trades, history. | Read-time derivation is a restatement, not an annotation. Splitting reporting rules by trade status is more logic and more to explain, for a distinction the ledger does not otherwise make. |
| D6 | Spinoff positions are **materialised as real trades**, not computed in a view. | When the spun-off shares are sold, those are real fills on the resulting instrument. The grouper needs a real opening trade to close them against; a view cannot be closed. |
| D7 | Both halves ship in **one branch, sequenced** — reporting first, identity second. | `symbol_change` and `merger` are usable the moment Half A lands. If spinoff's migration turns ugly it can be dropped late without wasting the rest. |

---

## 3. What ships

| File | Responsibility |
|---|---|
| `db/migrations/003_derived_fills.sql` | **new** — `derived_fill`, three columns, two indexes, one PK rework |
| `db/schema.sql` | modify: the same objects, so a fresh database matches a migrated one |
| `db/trades.py` | modify: `regroup_account` writes and reaps derived fills, routes allocations, records the effective instrument |
| `db/positions.py` | modify: `open_positions` prefers the effective instrument |
| `cli.py` | modify: drop the refusal for all three types, and the "unreachable" comments on the three guards PR #10 left stranded — they become live code again |
| `docs/known-gaps.md`, `README.md` | modify |

`ledger/` is **not** touched. `adjust_fills` is already correct for all five types; this
branch is entirely about persisting its output.

---

## 4. Schema

### 4.1 `derived_fill`

`id` is the `_spinoff_fill_id` uuid5 and is the **primary key**, supplied by the caller
rather than defaulted — that is what makes step 1's `ON CONFLICT (id) DO UPDATE` stable
across regroups. Alongside it: `account_id`, `instrument_id`, `executed_at`, `side`,
`quantity`, `price`, `fee`, `is_estimated`, plus provenance —
`derived_from_fill_id` → `fill(id) ON DELETE CASCADE`, and
`corporate_action_id` → `corporate_action(id) ON DELETE CASCADE`.

**These columns exist for the audit trail, not for reconstruction.** `regroup_account`
never reads them back — it recomputes the derived set from `adjust_fills` on every run.
The table's job is to give the foreign keys something real to point at and to let a
human answer "where did this position come from?". That is why it carries provenance
`fill` does not, and omits `fill` columns (`venue_fill_id`, `content_hash`, `source`,
`funding_source`) that describe how a row arrived from a venue — a derived row did not
arrive from anywhere.

It carries `UNIQUE (id, account_id)`, matching `fill_id_account_uniq`, so composite
foreign keys can target it with the same cross-account guard.

Both provenance FKs cascade deliberately: a derived row has no meaning once either its
parent fill or its action is gone, and regroup would delete it on the next run anyway.
The cascade makes the intermediate state unrepresentable rather than merely transient.

### 4.2 `trade`

- `effective_instrument_id UUID REFERENCES instrument(id)` — nullable. NULL means "no
  identity-changing action applies", and `open_positions` falls back to the opening
  fill's instrument. Pre-existing rows need **no backfill**.
- `opening_derived_fill_id UUID` with a composite FK to `derived_fill (id, account_id)`,
  `ON DELETE SET NULL (opening_derived_fill_id)` — the column-scoped form, for the same
  reason `trade_opening_fill_fk` needs it: a bare `SET NULL` on a composite FK nulls
  `account_id` too and violates its `NOT NULL`.
- `CHECK (opening_fill_id IS NULL OR opening_derived_fill_id IS NULL)` — at most one.
  Both NULL remains legal and keeps its existing meaning: an orphaned trade that kept
  its judgment.
- `CREATE UNIQUE INDEX trade_opening_derived_uniq ON trade (account_id,
  opening_derived_fill_id) WHERE opening_derived_fill_id IS NOT NULL` — the upsert key
  for derived trades, mirroring `trade_opening_fill_uniq`.

### 4.3 `trade_fill`

The sharpest edge in the migration. Today: `PRIMARY KEY (trade_id, fill_id)` with
`fill_id NOT NULL`.

- `fill_id` becomes nullable.
- `derived_fill_id UUID` added, with a composite FK to `derived_fill (id, account_id)`
  `ON DELETE CASCADE`.
- `CHECK (num_nonnulls(fill_id, derived_fill_id) = 1)` — exactly one.
- The primary key is dropped and replaced with a surrogate `id UUID PRIMARY KEY DEFAULT
  gen_random_uuid()`, plus two partial unique indexes —
  `(trade_id, fill_id) WHERE fill_id IS NOT NULL` and
  `(trade_id, derived_fill_id) WHERE derived_fill_id IS NOT NULL` — preserving the
  uniqueness the old PK gave while allowing a NULL in either column.

A composite PK cannot contain a nullable column, so this rework is forced rather than
chosen. It is the one destructive step in the migration and the reason `003` must be
verified against a populated database, not only an empty one.

---

## 5. The regroup lifecycle

`fetch_fills` → reduce by `manual_held` → `adjust_fills` is unchanged, including the
ordering that PR #10 established (adjustment *after* the manual reduction, never
before). Everything new is downstream.

### 5.1 Identifying derived fills

A fill in `adjust_fills`' output is derived exactly when its id is not among the ids
that were fetched. This holds because split, reverse-split, symbol change and merger all
preserve the fill's id; only spinoff mints one.

**This is an implicit invariant and must be pinned by a test** that fails loudly if a
future action type starts minting ids — otherwise new synthetic fills would be silently
mis-filed as real and hit the same foreign-key violation this branch exists to remove.

### 5.2 Write order, forced by the foreign keys

1. Upsert `derived_fill` rows for the derived set — before any trade references them.
   `ON CONFLICT (id) DO UPDATE`, since the uuid5 is stable across regroups.
2. Upsert trades. The opening allocation routes to `opening_fill_id` or
   `opening_derived_fill_id` depending on which set its id belongs to.
   `effective_instrument_id` is taken from the adjusted fill's `instrument_id` — the
   same value, at the same moment, that `primary_underlying` is already derived from.
3. Rewrite `trade_fill`, routing each allocation to the matching column.
4. Reap stale trades.
5. Reap stale `derived_fill` rows — **last**, after the trades referencing them are gone.

### 5.3 The reaping trap

Both the protection `UPDATE` and the final `DELETE` currently treat a trade as stale when:

```sql
opening_fill_id IS NULL OR NOT (opening_fill_id = ANY($2::uuid[]))
```

A spinoff-derived trade has no `opening_fill_id` **by construction**. Under the existing
predicates it would be written and then reaped by the very next statement — or, if it
carried notes, protected into a judgment-only husk with its P&L nulled.

This is the single most dangerous part of the change, because it fails *quietly*: a test
that asserts on trades before the reaping runs would pass, and real use would produce
nothing at all. `seen_openings` becomes two collections, and both predicates must test
membership in the one matching each trade's opening kind. A trade with both columns NULL
is still stale, preserving the orphan path exactly.

### 5.4 Removal remains a genuine undo

`corporate remove` → regroup → no spinoff action → no derived fills produced → the
spinoff's trade is reaped in step 4, its `derived_fill` row in step 5. Nothing
accumulates and nothing survives the action that created it. This property is what makes
delete-and-regenerate safe rather than merely convenient, and it is the reason D4 is
stated as an invariant rather than an implementation note.

---

## 6. Reporting surface

`open_positions` resolves the instrument as
`COALESCE(t.effective_instrument_id, f.instrument_id)` and must `LEFT JOIN instrument`
on that expression rather than on `f.instrument_id`.

Consequences, all of which follow without further change:

- `deadband positions` and `deadband trades` agree after a symbol change or merger.
- Marks price correctly, because `latest_marks` keys off the position's instrument.
- `reconcile` becomes reliable across a restatement for the same reason.
- A spinoff-derived trade has no opening fill, so the existing `LEFT JOIN fill` yields
  NULL — the `COALESCE` is what supplies its instrument at all, not merely a correction.

`deadband trades` reads `primary_underlying`, which is already written from the adjusted
fill and needs no change.

---

## 7. Failure policy

| Condition | Outcome |
|---|---|
| `--type merger`, `spinoff` or `symbol_change` | **Now accepted.** The refusal added in PR #10 (`8292e9e`) is removed. |
| `--resulting-symbol` on a type that does not use it | Still refused, exit 2. Unchanged — it guards a real dependency-graph hazard. |
| `--basis-allocation` on a non-spinoff | Still refused, exit 2. Unchanged. |
| Spinoff whose resulting instrument has no `instrument` row | Refuse at the CLI, exit 2. `derived_fill.instrument_id` is a real FK; failing at resolution is clearer than a constraint violation from inside regroup. |
| A `derived_fill` row whose parent fill or action is deleted | Cascades away. The next regroup would have removed it regardless. |

---

## 8. Testing

- **The reaping trap gets a dedicated test** that regroups *twice* and asserts the
  spinoff position survives the second run. A single-regroup test cannot distinguish a
  correct implementation from one that writes and immediately reaps.
- **Spinoff round-trip**: a BUY, a stored spinoff, regroup → two positions (parent at
  reduced basis, child at the allocated basis). Then `corporate remove` → regroup → one
  position at the original basis, and **zero rows in `derived_fill`**.
- **Selling the spun-off shares**: a real SELL fill on the resulting instrument closes
  against the derived opening — D6's justification, and the case a view-based design
  fails.
- **Symbol change and merger**: `open_positions` reports the new instrument; a mark set
  on the new symbol prices the position; `deadband trades` and `deadband positions`
  agree.
- **The id-minting invariant** (§5.1): a test that fails if any non-spinoff action
  produces a fill id absent from the input set.
- **Migration `003` against a populated database**, not only an empty one — the
  `trade_fill` primary-key rework is the only destructive step and the only one that can
  fail on real rows.
- **Restore the coverage PR #10 deleted.** Three CLI tests were removed there as
  genuinely vacuous once the types were refused — the merger and spinoff
  presence checks, and the non-finite `--basis-allocation` cases. Un-refusing the types
  makes their inputs reachable again, so they must come back rather than be quietly
  forgotten. The invariants they pinned currently survive only in `tests/test_corporate.py`,
  at the pure layer.
- The test database is shared and persistent: scope every assertion to rows the test
  created, and probe only through the transaction-rolled-back `conn` fixture.
- Every new test is gated against a mutant.

---

## 9. Known gaps this design creates

1. **`derived_fill` is invisible to the CLI.** Nothing lists or explains derived rows;
   a user seeing a spinoff position has no command that shows where it came from, only
   the `corporate list` entry for the action.
2. **The derived-id invariant is a convention, not a constraint.** §5.1 identifies
   derived fills by set difference. A test pins it, but the schema cannot express
   "`adjust_fills` only mints ids for spinoffs".
3. **No audit trail on restatement** — carried forward unchanged from gap #36. Adding or
   removing an action still silently changes every affected position, and now also
   creates or destroys `derived_fill` rows, with nothing recording what changed.
4. **Merger cash is still not modelled.** `cash_component` remains stored and never read
   by `adjust_fills` (gap #35). Accepting mergers makes the omission reachable in
   practice rather than theoretical.
5. **Importing corporate actions is still out of scope** (gap #33, issue #12). This
   branch makes the actions storable and correct; parsing them out of a broker export
   remains its own problem.
