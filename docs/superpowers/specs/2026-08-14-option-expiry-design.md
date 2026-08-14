# Deadband — Fidelity option expiry

**Date:** 2026-08-14
**Status:** Design approved
**Depends on:** Subsystem A (ledger), `importers/fidelity.py`
**Scope:** `EXPIRED` rows in Fidelity activity exports. Nothing else.

---

## 1. Context

Real Fidelity exports for three accounts, covering 2022–2026, contain **27 `EXPIRED`
rows** that the importer does not recognise. Every one carries a nonzero `Quantity`, so
each lands in `ImportBatch.blocking`, and `cmd_import` refuses the entire import — exit
code 2, nothing written (`cli.py:317-325`) — the moment one of these files is imported.
**These accounts' files cannot be imported at all until `EXPIRED` is handled.**

That refusal is the safe failure, not the harmful one. The harmful one is hypothetical,
and it is why the fix has to be the correct close rather than just a rule that stops
blocking: were an `EXPIRED` row ever unmapped *without* a money column that happens to
block — the exact gap `Outcome.UNSUPPORTED` (§5) closes for `ASSIGNED`/`EXERCISED` — only
the opening `YOU SOLD` fill would be recorded, and a short call that expired worthless
would **never close**:

- the position stays open forever in `positions`;
- it is a short, so it is valued as a liability that does not exist, understating equity;
- the realised gain — the whole premium, a 100% win — is never realised;
- `reconcile` reports persistent, growing drift attributable to nothing obvious.

Fixing this is worth doing before the first real import for a simpler reason than drift:
these three accounts' `EXPIRED` rows block every file that contains one, so there is no
"import now, reconcile later" option — the accounts stay unimportable until `EXPIRED` is
handled.

### What the real exports established

- Expiry is **not calls-only** — the data contains `EXPIRED PUT` rows.
- The `Symbol` column on an expiry is the **same option symbol as its opener**
  (`-ZXCO261121C500`), so the instrument resolves through the existing
  `parse_option_symbol`.
- `Price ($)` is **empty**, not `"0"`.
- `Quantity` is **signed** (`-1`, `-4`, `+2`): negative for a short position, positive for
  a long one.
- `Run Date` is the broker's booking date, which trails the true expiry — a Friday expiry
  books the following Monday.
- There are **zero** `ASSIGNED` or `EXERCISED` rows across all three accounts and five
  years. (`EARLY` in the data is `EARLY DIST NO EXCEPT`, an IRA distribution, not an early
  assignment.)

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| E1 | Scope is **expiry only**. Assignment and exercise are out. | No assignment exists in five years of real data, and modelling one from documentation rather than from a row actually received is how the fixture got its money columns wrong the first time. |
| E2 | Assignment and exercise must **block the commit**, loudly. | E1 is only safe if the unhandled case refuses instead of passing. See §5. |
| E3 | An expiry is a **closing fill at price zero**. | Economically identical to closing the position at zero, and grouping, P&L and positions already handle closes correctly. A distinct event type would teach every consumer of fills a new class for arithmetic that is already right. |
| E4 | The fill is dated the **option's expiry date**, not `Run Date`. | See §4. |
| E5 | `side` is derived from the **sign of `Quantity`**. | The row describes the position being removed, not a trade direction; there is no verb to read a side from. |
| E6 | The zero price is a **constant the branch supplies**, never a parsed value. | See §4. |
| E7 | An expiry with no opener in the ledger is **recorded as a gap, not defended against**. | 0 of 27 in real data, and it self-heals once all of an account's files are imported. See §7. |

---

## 3. Recognition

One new `Outcome` member and one rule:

```python
class Outcome(enum.Enum):
    FILL = "fill"
    CASH = "cash"
    INTERNAL = "internal"
    EXPIRY = "expiry"        # new
    UNSUPPORTED = "unsupported"  # new, see §5
```

```python
Rule("expired_option", "EXPIRED", Outcome.EXPIRY),
```

The verb stays **data**, matching this file's stated preference that rule membership be
reviewable at a glance rather than buried in logic. `test_every_rule_is_reachable`
continues to guarantee no rule is shadowed by an earlier one.

**Position in `RULES` is unconstrained.** The table is ordered most-specific-verb-first
because several rules share a leading verb (`REINVESTMENT` appears twice). No existing verb
is a prefix of `EXPIRED` and `EXPIRED` is a prefix of none, so the new rows cannot shadow or
be shadowed. Place them where they read best; the reachability test is the backstop, not the
ordering convention.

Rejected alternatives:

- **Extra flags on `Rule`** (`side_from_sign`, `price_override`) reusing `Outcome.FILL`.
  This turns the zero-price carve-out into a data flag that any future rule could set,
  widening the blast radius of the guard's exception — the opposite of what the guard's
  own docstring argues for, to save one enum member.
- **Handling `EXPIRED` beside `YOU BOUGHT`/`YOU SOLD`.** The verb stops being data and
  falls outside the reachability test.

---

## 4. The emitted fill

| field | source |
|---|---|
| instrument | `Symbol` via `parse_option_symbol` |
| `occurred_at` | the expiry date encoded in the symbol, at **midnight UTC** — the same date-only convention `Run Date` rows already use |
| `side` | `BUY` when `Quantity < 0`, `SELL` when `Quantity > 0` |
| `quantity` | `abs(Quantity)` |
| `price` | `Decimal(0)`, supplied by the branch |
| `fee` | `Decimal(0)` |
| `funding_source` | `"external"` (the default) |
| dedupe | the existing shared `content_hash` path, **including the occurrence counter** |

**The date comes from the symbol, not from the `AS OF` prose.** For an option, expiry sits
*inside* `instrument_natural_key`, so the symbol's date is the same value that mints the
instrument and cannot disagree with it. Parsing the prose would introduce a second source
that could. This matters for reconciliation: a statement dated the day after a Friday
expiry shows no such position, so dating the close to the following Monday's `Run Date`
would leave a phantom open across the statement date and produce false drift in exactly
the window `reconcile` is meant to check.

**The zero-price guard is not called on this branch, and the reason is precise.**
`zero_price_warning` exists because *downstream of `_decimal`, a missing column and a
genuine zero are indistinguishable* — its docstring says so, and the defect that motivated
the whole importer effort was a real export whose money columns parsed to `Decimal("0")`
with no warning. On this branch `Price ($)` is **never read**. The zero is a constant the
code supplies, so the ambiguity the guard protects against cannot arise. Skipping it is
not an exception to the guard's logic; it is outside its premise. That reasoning belongs
in the code as a comment, because the next reader will otherwise see a fill-shaped row
priced at zero and assume it is the very bug the guard was written for.

**The occurrence counter is load-bearing.** Two lots of the *same* size expiring on the
same day would otherwise produce an identical `content_hash`, and the second would be
silently deduped away — losing a lot. The real data contains a same-day pair (4 and 5
contracts) that differ in quantity and so are already distinct, which is precisely why
this would not be caught by testing against that data alone.

---

## 5. Failure policy

| Condition | Outcome |
|---|---|
| `Quantity` is zero, absent or unparseable | Unmapped, with a warning. Direction and size are both unknowable. |
| `Symbol` absent, or does not parse as an option | Unmapped, with a warning. The instrument is unidentifiable. |
| `ASSIGNED` / `EXERCISED` | **Blocks the commit**, naming the verb. |
| Any other unrecognised verb | Unchanged from today. |

**Why assignment gets an explicit blocking rule rather than being left unmapped.** A
realistic `ASSIGNED`/`EXERCISED` row already blocks today: it carries a nonzero
`Quantity`, and an unmapped row blocks whenever it carries a nonzero `Quantity` *or*
`Amount`. `Outcome.UNSUPPORTED` earns its place anyway — it names the verb in the refusal
instead of a generic "unhandled action", and it blocks unconditionally, independent of
what the row's money columns happen to hold, rather than depending on `Quantity` staying
nonzero. That is defence in depth and a better error message, not the only thing standing
between an assignment and a silent drop.

---

## 6. Testing

- **Side follows the quantity sign** — a short call closes with a `BUY`, a long put with a
  `SELL`. Flipping the derivation must turn a test red.
- **The date is the expiry, not `Run Date`** — the two differ by three days in the real
  data, so substituting `Run Date` must turn a test red.
- **Price is zero *and* no zero-price warning fired.** Both halves matter: the second is
  what pins the carve-out rather than the value.
- **Two same-day lots of different sizes both survive** the dedupe, pinning the occurrence
  counter.
- **A zero quantity and an unparseable symbol warn** rather than guess.
- **`ASSIGNED` blocks the commit**, and the message names the verb.
- **End to end:** an open short call closed by its expiry yields realised P&L equal to the
  premium received, and leaves no open position.

Every new test is gated against a mutant.

---

## 7. Known gaps this spec creates

1. **An expiry whose opening fill is absent from the ledger** makes the grouper treat the
   closing fill as an *opening* one, creating a phantom position at zero cost basis.
   Deliberately not defended against: 0 of 27 expiries in five years across three accounts
   are orphaned, and `regroup_account` recomputes trades from every fill, so importing an
   account's files out of order resolves itself once they are all in. It is permanent only
   if one year of an account is imported and the earlier ones never are.
2. **Corporate actions remain unhandled.** The two long-term accounts contain `MERGER`,
   `REVERSE SPLIT`, `NAME CHANGED`, `DISTRIBUTION`, `TRANSFER OF ASSETS ACAT`, `IN LIEU
   OF` and a `BUY CANCEL OPENING TRANSACTION`. `ledger/corporate.py` already models several
   of these but is not wired to the importer. Every one of these carries a nonzero
   `Quantity` or `Amount` in the real exports (`MERGER` qty 41, `NAME CHANGED` qty -20,
   `REVERSE SPLIT` qty 306, `IN LIEU OF` amount 0.03), so every one blocks the commit
   today — these two accounts cannot be imported until corporate actions are modelled.
   That is a real gap, but it is a refusal, not a corruption: unlike the pre-fix expiry
   hazard, none of these rows can pass silently while changing share counts. It still
   needs its own spec, to turn a blocked import into a correct one.
3. **Backdated `as of` correction rows** (`REINVESTMENT as of …`, `FEE CHARGED as of …`)
   appear in one account and are not modelled. Their effect on dating is unexamined.

---

## 8. Hygiene

The test fixture must use **fabricated** option symbols and underlyings, not the real ones
in `imports/`. The repository is public. Per the standing rule, before committing anything
derived from a real export, diff its numeric tokens against the export and expect only
calendar and structural fragments to collide — the deny-list guards identifiers, not
values, and a fixture with fabricated tickers but faithfully copied amounts has passed the
scan clean before.
