# Deadband — Thesis Lifecycle (Subsystem B)

**Date:** 2026-08-05
**Status:** Design approved, pending user review of this document
**Depends on:** Subsystem A (trade & position ledger)
**Scope:** Subsystem B only.

---

## 1. Purpose

A owns *what happened*. B owns *why you thought it would*, and whether you were right.

The distinction B exists to enforce: **a thesis can be correct while the trade loses money,
and wrong while it wins.** A journal that records only P&L cannot tell those apart, so it
reinforces luck and punishes good reads. B records the two independently.

A thesis is a first-class entity, not an annotation on a trade. It can exist with no trade
attached — an idea you planned and never triggered — and one thesis can cover several
trades. The trades you did *not* take are frequently the more instructive record.

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| B1 | Thesis is independent of trades; trades attach to it | Captures planned-but-untriggered ideas, and lets one thesis cover several trades. |
| B2 | Falsifiable claim with a verdict separate from trade P&L | The only way to distinguish good process from good outcome — the entire point of journaling. |
| B3 | Prose reasoning **plus** optional structured assertions | Theses that reduce to a price predicate get auto-evaluated; the ones that do not still get written. Nothing is forced into a shape it does not fit. |
| B4 | `setup` is a first-class entity with a definition | Free-text tags cannot be aggregated — `ORB` and `orb breakout` become two setups and C's per-setup numbers become fiction. |
| B5 | Untraded theses get outcome tracking, computed from price data | "I was right and did not take it" is a real and expensive pattern. Depends on D; interface now, implementation later. |
| B6 | Conclusion is manual, with prompting | Judgment stays the user's. Prompting stops theses rotting open forever. |
| B7 | Verdicts are **suggested**, never auto-applied | The system can see whether assertions resolved; only the user can say what that means. |
| B8 | A trade belongs to at most one thesis | Prevents C double-counting P&L across theses. Revisit if hedging structures make it painful. |
| B9 | Lessons are tagged, not free prose | Forty free-text notes saying "cut it early" are unreadable; forty rows tagged `cut_winner_early` are a finding. |

---

## 3. Scope

### In scope

- `thesis` entity and its lifecycle
- `thesis_assertion` — machine-checkable conditions with confirming/invalidating polarity
- `setup` as a first-class entity; migration of A's free-text `strategy_tag` to a FK
- `thesis_trade` links
- Verdict, execution note, and tagged lessons
- Verdict suggestion from resolved assertions
- The review queue that surfaces theses needing conclusion
- Assertion evaluation **logic**, tested against synthetic price series

### Out of scope

- Aggregation and analytics across theses — subsystem C
- The price data that drives automatic assertion evaluation — subsystem D
- Strategy backtesting — subsystem E

### The D boundary

B defines an `AssertionEvaluator` interface and ships one implementation: manual, where the
user marks an assertion met or failed. D later supplies a price-driven implementation and
nothing in B changes.

**The evaluation logic itself is built and tested now**, against synthetic price series.
Only the data source waits. This is the same discipline A uses for `MarkSource`: the
dependency is isolated to an interface, not deferred as an unwritten feature.

---

## 4. Data model

### `setup`

| Column | Notes |
|---|---|
| `id` | pk |
| `name` | unique |
| `description` | what this setup is |
| `qualifying_conditions` | what must be true for a situation to *be* this setup |
| `typical_risk` | usual stop placement / sizing character |
| `is_active` | retire a setup without deleting its history |
| `created_at` | |

Referenced by `thesis.setup_id` and by `trade.setup_id`.

**Amends A:** `trade.strategy_tag` (free text) becomes `trade.setup_id`, a nullable FK.
Delivered as a migration; A's plan needs no rework since the column already exists and
regroup never writes it.

### `thesis`

| Column | Notes |
|---|---|
| `id` | pk |
| `title` | short handle |
| `status` | `idea` / `planned` / `active` / `concluded` / `never_triggered` / `abandoned` |
| `setup_id` | nullable FK |
| `scope_symbol` | what it is about; a thesis may concern an underlying generally, not one instrument |
| `reasoning` | prose, markdown |
| `conviction` | 1–5 |
| `horizon_start`, `horizon_end` | when it should play out |
| `planned_entry`, `planned_stop`, `planned_target`, `planned_risk` | the plan, recorded before the fact |
| `created_at`, `activated_at`, `concluded_at` | |
| `verdict` | `correct` / `wrong` / `partial` / `unresolved`; null until concluded |
| `verdict_source` | `manual` / `suggested_accepted` / `computed` |
| `execution_note` | what went right or wrong in *executing* it — separate from whether the read was right |
| `created_by_review` | flags a thesis written retrospectively, so pre-commitment is not overstated |

**`thesis` never stores P&L.** Trade outcome is read from A through `thesis_trade`.
Storing both in one place is exactly the conflation this subsystem exists to prevent.

### `thesis_assertion`

| Column | Notes |
|---|---|
| `id`, `thesis_id` | |
| `symbol` | free text, resolved to an instrument when possible |
| `operator` | `lt` / `lte` / `gt` / `gte` / `crosses_above` / `crosses_below` / `never_above` / `never_below` |
| `level` | numeric |
| `deadline` | timestamptz |
| `polarity` | **`confirming`** or **`invalidating`** |
| `status` | `pending` / `met` / `failed` / `expired` |
| `evaluated_at`, `evaluated_by` | `manual` / `computed` |
| `note` | |

**Polarity is what makes an assertion meaningful.** "SPY closes below 520 by Aug 22"
confirms the thesis; "SPY never closes above 524" invalidates it. Without the distinction,
knowing an assertion was met tells you nothing about whether you were right.

`never_above` / `never_below` are path-dependent over the window rather than point-in-time,
which is why evaluation needs a price *series* and not a single quote.

### `thesis_trade`

`thesis_id`, `trade_id`. Unique on `trade_id` (B8).

### `thesis_lesson`

`thesis_id`, `tag`, `note`.

Tagged so recurring mistakes become countable. The output that justifies the whole
subsystem is a sentence like *"you have cut a winner early 14 times; on 11 of them the
thesis was correct"* — which tagged rows can produce and prose cannot.

Tags are a controlled vocabulary, editable, seeded with common patterns
(`cut_winner_early`, `sized_too_small`, `moved_stop`, `chased_entry`, `no_plan`,
`ignored_invalidation`, `revenge_trade`, `held_past_thesis`).

### `thesis_needing_review` (view, not a table)

Theses with `status = 'active'` where the horizon has passed, or every linked trade is
closed, or an invalidating assertion has fired. A view so there is no state to drift out of
sync.

---

## 5. Verdict suggestion

When every assertion has resolved, the system computes a suggestion:

- All confirming met, no invalidating triggered → **correct**
- Any invalidating triggered → **wrong**
- Mixed, or some expired unresolved → **partial**
- No assertions at all → no suggestion; verdict is manual

The suggestion is presented, never applied. The user accepts it (`verdict_source =
suggested_accepted`) or overrides it (`manual`). The system can see whether conditions
resolved; only the user can say what that means.

---

## 6. What B unlocks for C

Because verdict and P&L are recorded separately, C gains a 2×2 that most journals
structurally cannot produce:

| | Trade won | Trade lost |
|---|---|---|
| **Thesis right** | Repeatable — do more of this | **Execution problem** — the most actionable quadrant |
| **Thesis wrong** | Luck; dangerous because it feels like skill | Working as intended — bad read, correct process |

Combined with `thesis_lesson` tags and `setup`, C can answer: which setups do I read
correctly but execute badly, and which am I simply wrong about?

---

## 7. UI

Added to A-2's screens, same rules — fixed layouts, no configurability.

1. **Thesis list** — filter by status, setup, verdict, conviction.
2. **Thesis detail** — reasoning, assertions with live status, linked trades with their
   P&L pulled from A, conclusion form.
3. **New thesis** — prose first, assertions optional and added inline.
4. **Review queue** — the prompt. Theses whose horizon passed or whose trades all closed.
5. **Setups** — define, edit, retire.

The trade detail screen from A-2 gains a thesis link.

---

## 8. Testing

- **Pure unit tests** for verdict suggestion across every assertion combination.
- **Assertion evaluation against synthetic price series**, including the path-dependent
  `never_above` / `never_below` operators, deadline expiry, and a series that touches a
  level intraday without closing through it.
- **DB-gated integration tests** for lifecycle transitions and the review view.
- A test asserting that regrouping trades in A **preserves** thesis links — the failure
  mode that the `opening_fill_id` upsert in A exists to prevent.

---

## 9. Deferred

- Automatic assertion evaluation from live prices — arrives with D (B5)
- Cross-thesis aggregation and the 2×2 report — subsystem C
- Whether a trade may serve two theses — revisit if hedging makes B8 painful
- Thesis templates per setup, so a recurring setup pre-fills its usual assertions
