# Deadband — `reconcile`: comparing the ledger against a broker statement

**Closes known gap #13.** Decided 2026-08-10.

---

## 1. Context

`ledger/reconcile.py` has existed since A-1: a pure `reconcile()` that values positions
at their marks, adds cash, and compares the total to a broker statement, returning a
`Drift`. It has ten tests. It has never had a caller.

Gap #13 recorded two reasons. Both are real, and there is a third nobody had named:

1. **No `account_snapshot` write path.** The table exists in `db/schema.sql`; nothing
   writes it.
2. **A type mismatch.** `db/positions.py::open_positions` returns `OpenPosition`;
   `reconcile()` consumes `Position`. An `OpenPosition` carrying an `unvaluable_reason`
   has no honest `Position` representation, because `Position` requires a concrete
   quantity and cost basis.
3. **`computed_cash` has no implementation.** `reconcile()` takes it as a parameter.
   `OUTFLOW_KINDS` exists in `importers/base.py` as the shared sign convention, with a
   docstring anticipating "a consumer that needs to net cash movements" — and no such
   consumer was ever written. There is no cash-balance query anywhere in the codebase.

### What this is for

A **post-import correctness check**. The user imports a statement or syncs a venue, then
reconciles against the balance the broker reports for that date. The question it answers
is *"did my import capture everything?"* — a drift means a missing fill, a double-counted
cash movement, or a wrong multiplier.

It is not a periodic health check and not a trend view. That shapes two things: snapshots
do not need a history UI, and the command's value is in being trustworthy on a single run
rather than fast or scriptable.

---

## 2. Decisions

| # | Decision | Reasoning |
|---|---|---|
| R1 | An unvaluable position makes the whole reconciliation **UNRELIABLE**; the command still reports the numbers but can never claim a clean pass. | Refusing outright would let one orphaned trade disable the check for an entire account — the same shape as the `size_in_quote` fill that currently blocks `sync --commit`. Valuing it at cost basis would be worse: that is a guess about a *quantity*, not a stale *price*, so the resulting equity figure is fiction rather than staleness. |
| R2 | Snapshots are **stored**, via a separate `snapshot add` command. | Re-running reconcile must not mean re-typing the figures, and what you compared against should be auditable later. `UNIQUE (account_id, as_of)` already models exactly this. |
| R3 | `reconcile()` **learns about unvaluable positions**; the judgement stays in the pure layer. | Matches how `positions` works — `ledger/positions.py` holds all the judgement and is exhaustively testable. Putting it in `cli.py` would leave the one decision that matters in the one layer this codebase does not test at the judgement level. |
| R4 | `Drift` exposes a single **`verdict`** field that callers render. | A caller reading `is_within_tolerance` alone would print a clean pass on an unreliable account. That is precisely the misuse the gap #12 note already warns about for `unvaluable_reason`. A single enum cannot be half-read. |
| R5 | `reconcile()` keeps `Position` rather than switching to `OpenPosition`. | `Position`'s minimalism is a feature. `OpenPosition` carries display concerns (`symbol`, `trade_count`) that reconciliation has no business depending on. |
| R6 | Cash is derived from **movements *and* fills**. | A buy spends cash as a fill, not a movement. Cash from `cash_movement` alone would omit every trade, making the cash line meaningless and the equity line wrong. |
| R7 | An account whose movements or instruments span **more than one currency** is refused. | v1 does not model FX. Summing across currencies produces a confident wrong number, which is the failure class this project exists to avoid. |
| R8 | **Manual entry only** — no statement parsing. | Two numbers off a statement. A PDF parser is a separate subsystem and is not what gap #13 asks for. |

---

## 3. Scope

### In scope

- `deadband snapshot add --account --as-of --equity --cash [--note]`
- `deadband reconcile --account [--as-of] [--tolerance]`
- A pure cash-netting function, finally consuming `OUTFLOW_KINDS`
- `reconcile()` extended for unvaluable positions and a verdict
- `db/snapshots.py` and `db/cash.py`

### Out of scope

- Parsing a broker statement (R8)
- Multi-currency accounts and FX (R7 — refused, not handled)
- Snapshot history, trend views, or a "when did this drift appear" query
- Running reconcile automatically after an import
- Editing or deleting a snapshot once written

### Acceptance bar

`deadband reconcile` on a real account, against a real statement figure, either agrees to
within a cent or names what it could not account for. It never prints a clean pass while
any part of the account is unvalued.

---

## 4. Data model

No schema change. `account_snapshot` already has exactly the needed columns:
`account_id`, `as_of`, `cash_balance`, `total_equity`, `source` (default `'statement'`),
`note`, and `UNIQUE (account_id, as_of)`.

`source` stays `'statement'` for manually-entered figures. The column exists for a future
automated source; nothing in this work sets anything else.

---

## 5. Cash derivation

```
computed_cash = Σ movements(signed)  +  Σ sell_proceeds  −  Σ buy_costs

  movements(signed):  −amount if kind ∈ OUTFLOW_KINDS else +amount
  sell_proceeds:      quantity × price × contract_multiplier − fee
  buy_costs:          quantity × price × contract_multiplier + fee
```

**The multiplier is load-bearing.** Two option contracts at $3.50 with a ×100 multiplier
cost $700, not $7. Omitting it understates cash by a factor of a hundred on every option
trade, and the resulting equity figure would be wrong in a way that looks like a plausible
drift.

**A DRIP nets correctly without special handling.** The dividend arrives as a
`CASH(dividend)` movement and the reinvestment spends it as a `FILL` with
`funding_source='reinvestment'`. Both legs are recorded, so the two cancel to the small
residual that genuinely stayed in cash. Do not add a special case; adding one would
double-count.

**Sweep rows are already absent.** `importers/fidelity.py` classifies a sweep-fund
reinvestment as `INTERNAL` precisely so it is not counted twice (A2-9). Cash netting
inherits that correctness and must not try to re-derive it.

The netting rule lives in `ledger/cash.py` — pure, taking plain rows — because the sign
convention is judgement, not plumbing. `db/cash.py` fetches and delegates.

---

## 6. The verdict

```python
class ReconcileVerdict(StrEnum):
    OK = "ok"                  # numbers agree within tolerance, nothing unvalued
    DRIFT = "drift"            # everything valued, but the numbers disagree
    UNRELIABLE = "unreliable"  # something could not be valued; the comparison cannot be trusted
```

`UNRELIABLE` **outranks** `DRIFT`. If an account has both an unvaluable position and a
numeric disagreement, the verdict is `UNRELIABLE`, because the disagreement cannot be
attributed: it may be entirely the missing position, or it may hide a real defect on top.
Reporting `DRIFT` there would imply a precision the data does not support.

`Drift` keeps `is_within_tolerance`, documented as answering only *"do the numbers
agree"* — a component of the verdict, never the answer. `Drift` gains
`unvaluable_positions: tuple[UnvaluableRef, ...]`, each carrying the instrument id (or
grouping key), a symbol for display, and the reason string produced by
`aggregate_positions`.

### The number that will alarm you

For an `UNRELIABLE` account, `computed_equity` **excludes** the unvaluable position
entirely, so the drift reads as a large negative figure. That is expected and is not
itself evidence of a defect. **The rendered output must say so on the same screen** —
otherwise the first real use of this command sends someone chasing a phantom.

---

## 7. Data flow

`reconcile`:

1. Resolve the account. **Unknown id refuses** (see §8).
2. `latest_snapshot(conn, account_id, as_of)` — most recent on or before `--as-of`,
   defaulting to now. **No snapshot refuses.**
3. `open_positions(conn, account_id)` → `OpenPosition[]`.
4. Partition: `unvaluable_reason is None` → adapt to `Position`; otherwise collect as an
   `UnvaluableRef`. The partition is on `unvaluable_reason`, never on `direction`.
5. `latest_marks(conn, [valuable instrument ids])`.
6. `net_cash(...)` over the account's movements and fills.
7. `reconcile(snapshot, positions, marks, computed_cash, unvaluable, tolerance)` → `Drift`.
8. Render by `verdict`. Exit 0 only on `OK`.

`snapshot add` writes one row and prints what it stored. Re-adding the same `as_of`
updates it — the same upsert-on-conflict reasoning as `set_mark`: manually correcting a
mistyped figure is the whole point, and the table has no history columns.

---

## 8. Failure policy

| Condition | Outcome |
|---|---|
| Unknown account id | Refuse, exit 2 |
| No snapshot for the account on or before `as_of` | Refuse, exit 2 |
| Movements or instruments spanning more than one currency | Refuse, exit 2, naming the currencies |
| Any position with `unvaluable_reason` set | Report, verdict `UNRELIABLE`, exit 1 |
| Numbers disagree beyond tolerance, everything valued | Report, verdict `DRIFT`, exit 1 |
| Agreement within tolerance, everything valued | Report, verdict `OK`, exit 0 |
| An unmarked but valuable position | Valued at cost basis, listed in `unmarked_instruments`; does **not** make the run unreliable |

The last row is existing behaviour and is deliberately unchanged. An unmarked position has
a known quantity and a missing *price*; cost basis is a defensible stale proxy for a
price. An unvaluable position has an unknown or meaningless *quantity*, for which no
proxy exists. Conflating the two is the single most likely way to get this wrong.

**Refusing an unknown account id differs from `positions`**, which prints "no open
positions" for a well-formed but nonexistent UUID. That divergence is deliberate here —
reconcile's entire purpose is to be trustworthy about absence — and is recorded as a gap
so the two commands are eventually made consistent rather than silently differing.

---

## 9. Testing

- **Verdict truth table**, in the pure layer: every combination of (within tolerance,
  has unvaluable) → expected verdict, including that `UNRELIABLE` outranks `DRIFT`.
- **Cash netting**: each `kind`'s sign; a buy and a sell including the contract
  multiplier; a DRIP pair netting to its residual rather than to zero or to double; a
  fee-only movement.
- **The multiplier is gated**: an option fill's cash effect must be wrong by ×100 if the
  multiplier is dropped. This is the mutation that matters most in this spec.
- **`latest_snapshot`** returns the most recent on or before the date, not the
  last-inserted — the same ordering hazard `latest_marks` has, and it must be tested the
  same way, by writing an older snapshot after a newer one.
- **Refusals write nothing** and exit non-zero: unknown account, no snapshot, mixed
  currency.
- **An `UNRELIABLE` run never exits 0**, even when the numbers agree to the cent.
- Every new test gated against a mutant. The test database is shared and persistent:
  scope every assertion to rows the test created.

---

## 10. Known gaps this spec creates

1. **No statement parsing.** Figures are typed by hand, so a typo produces a false drift.
   Mitigated only by the figures being stored and re-readable.
2. **Multi-currency accounts are refused, not handled.** The moment a non-USD account
   exists, reconcile stops working for it entirely.
3. **No snapshot history view.** Snapshots accumulate but nothing surfaces the trend, so
   "when did this drift first appear" needs SQL.
4. **A snapshot cannot be deleted**, only overwritten at the same `as_of`. A snapshot
   entered against the wrong account stays there.
5. **`reconcile` refuses an unknown account id while `positions` does not.** Deliberate
   here; the inconsistency should be resolved by making `positions` stricter, not by
   loosening this.
6. **Cash correctness depends on every fill being present**, which is the thing reconcile
   exists to check. A missing fill shows up as drift — that is the point — but a missing
   fill in an account that *also* has an unvaluable position produces an `UNRELIABLE`
   verdict that hides it. Reconcile cannot distinguish those two causes.
