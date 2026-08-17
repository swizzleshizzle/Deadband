# Deadband — Importing corporate actions from broker exports

**Date:** 2026-08-17
**Status:** Design approved
**Depends on:** PR #14 (all five action types materialise), `importers/fidelity.py`, `ledger/corporate.py`
**Closes:** issue #12, gap #33
**Scope:** Recognising corporate-action rows in a Fidelity history export, grouping them into
logical actions, and **proposing** `corporate add` commands. Storing them automatically is
deliberately out of scope — see D2.

---

## 1. Context

Gap #33 records that corporate actions cannot be imported. The rows match no importer rule
and carry a nonzero quantity, so they land in `ImportBatch.blocking` under the
money-carrying-unmapped policy and **refuse the entire import**. Two accounts have therefore
never been importable at all.

PR #14 made all five action types storable and correct. What remains is getting them out of
the export.

### What was verified against the real exports before this design

The following was established by reading the files directly, not assumed:

| Claim | Finding |
|---|---|
| The 90-day Activity & Orders cap blocks this | **False.** The exports are per-year history files spanning several years per account. That cap constrains ongoing incremental imports, not this backfill. Issue #12 is not blocked on it. |
| Rows are identified by CUSIP, not ticker | **True**, and the CUSIP is present: a CUSIP-shaped token appears in the `Action` field of every corporate-action row, and in `Description` for nearly all. The gap is that `instrument` has nowhere to put one — there is `natural_key`, but no CUSIP column. |
| Rows arrive as `FROM`/`TO` pairs | **True for reverse splits and name changes. False for spinoff**, which is a single positive row with no negative counterpart — correct, since a spinoff adds the child without removing the parent. |
| A merger is three rows | **True.** |
| The `Symbol` column is empty on these rows | **True** for the paired rows. |

### The export comes in two dialects, and only one carries an account

Verified by running the importer against a real history export:

- **Activity & Orders** — the dialect every existing fixture uses. Carries `Account` and
  `Account Number` columns, so rows route themselves.
- **History for Account** — the dialect the multi-year exports use, and **the only one that
  contains corporate actions**. It has no account columns at all (the account is in the
  filename); it carries `Cash Balance ($)` instead.

A history export therefore parses cleanly but yields fills and cash whose `external_ref` is
`None`, and `db/importing.py` is explicit that "a row whose external_ref is None is never
routed at all". Those rows route only via `import --account <uuid>`, which already exists and
already refuses with a clear message when it is needed and absent.

Two consequences for this design:

1. **Recognition closes gap #33 only in combination with `--account`.** Removing the block is
   necessary and is the part that is missing; `--account` is the part that already works. Any
   claim that recognition alone makes the accounts importable is wrong.
2. **`--account`'s help text is wrong for this dialect.** It says a venue carrying its own
   per-row account number — naming Fidelity — "routes automatically and does not need this."
   True of Activity & Orders, false of History for Account, and misleading exactly when a user
   is trying to import the files that contain corporate actions. Fixing that sentence is in
   scope.

Two things no prior document records, both found here:

- **Cash in lieu of fractional shares.** Rows exist for the cash paid out for the fractional
  remainder a reverse split leaves. They move real money and are modelled nowhere.
- **A `#REOR <number>` reorganisation reference on every corporate-action row.** This is the
  venue's own statement of which rows constitute one event.

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Recognition is separated from derivation, and **recognition alone is the fix for gap #33**. | The accounts are unimportable because these rows *block*. A rule that recognises them without producing anything unblocks both accounts before a single ratio is derived, and is independently shippable. |
| D2 | The importer **proposes, never stores**. It emits ready-to-run `corporate add` commands. | A corporate action silently restates history across every account holding the instrument. `corporate add` previews by default and refuses duplicates precisely because of that; an importer that writes them directly bypasses the deliberation those guards exist to force. A mis-derived ratio is wrong by a factor of 36 and looks plausible at every step. |
| D3 | An import containing an unrecorded action **completes**, reporting loudly. | Refusing is today's pain in a politer form. The positions in the affected instruments are knowingly wrong until the proposals are run — a warned-about wrong number, not a silent one. |
| D4 | Group on the venue's `#REOR` reference, with `(ex-date, CUSIP pair)` as fallback. | It is Fidelity stating which rows are one event. Inferring the grouping reproduces a fact the file already carries, and handles the three-row merger without a special case. |
| D5 | Every proposal prints **the quantities it derived from**, beside the ratio it inferred. | Cash-in-lieu means the raw quantities need not be an exact multiple, so a derived ratio can be slightly off. This is the one moment a human can catch an inverted or distorted ratio before it is stored. |
| D6 | Cash-in-lieu rows are **recognised and reported, never applied**. | They need the merger-cash arithmetic gap #35 already tracks. Recognising them stops them blocking; applying them is a second subsystem. |
| D7 | Unresolvable pieces **degrade, they do not fail**. A CUSIP mapping to no known instrument still yields a proposal, with the description and CUSIP shown and the symbol left blank. | This is what makes CUSIP resolution advisory. **No schema change, and no CUSIP column, is needed for this work.** |

---

## 3. What ships

| File | Responsibility |
|---|---|
| `importers/fidelity.py` | modify: `Outcome.CORPORATE_ACTION`, five new `Rule`s, row collection, grouping and derivation |
| `importers/base.py` | modify: `ImportBatch.corporate_actions` and `ImportBatch.cash_in_lieu` |
| `cli.py` | modify: `cmd_import` renders the proposals; fills the spinoff ratio from the ledger; corrects `--account`'s help text for the History dialect |
| `docs/known-gaps.md`, `README.md` | modify |

`ledger/` is **not** touched. `importers/` remains pure — no I/O, no clock, no `db` import;
`tests/test_purity.py` enforces it, and the proposal is *data* that `cli.py` renders.

---

## 4. Recognition

A new `Outcome.CORPORATE_ACTION`: recognised, produces no fill and no cash, and **does not
block**.

It is deliberately not `Outcome.UNSUPPORTED`, which already exists for "recognised and
deliberately refused" and blocks unconditionally. These rows are recognised and *deferred*:
the import proceeds and the proposal says what to do next.

Five `Rule`s matched on leading verb — reverse split, name change, merger, spinoff
distribution, and cash-in-lieu. `RULES` is first-match-wins and `test_every_rule_is_reachable`
fails if one rule shadows another; that test is what proves all five are live, so it must be
seen to fail if a rule is ordered wrongly.

**Recognition is the missing half of gap #33**, and it is the first task and independently
shippable. At that point a history export imports — given `--account`, which already works —
with the corporate-action rows reported as recognised and unhandled. Everything after it
improves the report rather than unblocking anything.

The precedent is `investment_gain_loss`, added for exactly this shape: a money-carrying row
that no rule matched, which blocked the commit, and which a real export "could not be imported
at all until this rule existed." Its comment says `INTERNAL` was chosen over leaving it
unmapped for that reason. This design makes the same move for a different reason — those rows
produce nothing *and* have a follow-up action, which is why they get their own outcome rather
than reusing `INTERNAL`.

---

## 5. Grouping

Group rows into logical actions on the **`#REOR` reference**.

**The exact token format is not yet pinned, and must not be guessed.** It varies across rows —
some carry a letter prefix — and a plausible-looking "last digit is the leg index" reading does
**not** survive contact with all the data. The implementation must derive the format from the
real files and pin it with tests. Where a row carries no usable REOR token, fall back to
`(ex-date, CUSIP pair)`.

Group shapes to expect, from §1: two legs for a reverse split and a name change, three for a
merger, one for a spinoff. A group whose shape matches none of these is reported as
unrecognised rather than forced into the nearest match — see §7.

---

## 6. Derivation

| Type | Ratio | Source |
|---|---|---|
| Reverse split | `abs(qty_in) : abs(qty_out)`, reduced to the smallest integer pair | The paired rows |
| Merger | as above | The paired rows |
| Name change | `1:1` | Constant |
| Spinoff | child shares : parent holding at the ex-date | **The ledger** — not derivable from the file, which carries only the child shares received |

The spinoff's ratio is filled by `cli.py`, which has a connection; `importers/` emits the
proposal with that ratio absent. This is the one place the proposal is completed outside the
pure layer, and it is why the field is optional rather than required.

### 6a. The ratio is also stated in the text — parse it, and cross-check

Found after this spec was first written, by checking the real exports rather than assuming:
**the description states the ratio explicitly.** Patterns of the form `N FOR N` occur 21 times
across the exports and `N:N` 10 times, alongside `R/S` and `REV SPLIT` markers.

So there are **two independent sources** for the same number: the stated ratio in the text, and
the ratio derived from the paired quantities. Use both.

- Parse the stated ratio where present.
- Derive the ratio from the quantities as §6 describes.
- **Cross-check them.** Agreement is strong evidence the parse is right. **Disagreement is the
  signal that matters** — it means either a fractional remainder paid out as cash in lieu, or a
  misparse — and the proposal must say so rather than silently preferring one source.

This is materially stronger than either source alone, and it directly serves D5: the whole
reason proposals carry evidence is that an inverted or distorted ratio is wrong by the square
of the ratio and looks plausible at every step. Two sources that agree is the best evidence
available; two that disagree is the loudest possible warning.

Where only one source is available — a name change states no ratio, and a spinoff has neither —
use what there is and record which source the ratio came from.

**Every proposal prints the quantities it derived from** (D5). The ratio is an inference; the
quantities are evidence. A reverse split whose quantities do not reduce cleanly — the
cash-in-lieu case — is exactly when a human needs to see both.

---

## 7. Failure policy

| Condition | Outcome |
|---|---|
| Corporate-action row recognised | Import **proceeds**. Row produces no fill, no cash, no block. |
| A group whose shape matches no known action | Reported as an unrecognised corporate action, with its rows. Import still proceeds. Not forced into the nearest match. |
| CUSIP resolves to no known instrument | Proposal still emitted, with description and CUSIP shown and the symbol blank (D7). |
| Spinoff whose parent holding cannot be determined | Proposal emitted with the ratio blank and a note saying why. |
| Ratio does not reduce to a clean integer pair | Proposal emitted with both the reduced ratio and the raw quantities, flagged as approximate. |
| Cash-in-lieu row | Recognised, reported separately, never applied (D6). |

Nothing in this design writes a corporate action. Every path ends in a proposal or a report.

---

## 8. The proposal surface

`cmd_import` prints, after the trade summary, a clearly separated section: one ready-to-run
`corporate add` command per detected action, each preceded by the evidence it was derived from
and by the description text identifying the instrument.

Cash-in-lieu rows are listed in their own subsection, stating plainly that they are recognised
but not applied and pointing at gap #35, so they are not mistaken for something the user can
act on.

The section must be hard to miss: the whole justification for D3 is that a knowingly-wrong
position is acceptable *only* when the user has been told.

---

## 9. Testing

- **Recognition, before derivation exists**: a fixture containing corporate-action rows imports
  successfully and produces no blocking entries. This is gap #33's actual acceptance test.
- **`test_every_rule_is_reachable` must fail** if any of the five new rules is shadowed. Pin
  this by mutation, not by assertion that it passes.
- **Grouping** against the real REOR format: two-leg, three-leg and one-leg groups each become
  one action.
- **A group of an unexpected shape** is reported, not coerced.
- **Ratio derivation**, including a case whose quantities do not reduce cleanly, asserting both
  the reduced ratio and that the evidence is reported.
- **The spinoff proposal** carries no ratio out of `importers/`, and is completed by `cli.py`
  against a ledger holding.
- **A CUSIP resolving to nothing** still yields a proposal.
- **`importers/` stays pure** — `tests/test_purity.py` already enforces this and must keep
  passing.
- Fixtures use **fabricated** symbols, CUSIPs, quantities and reorganisation references. The
  repository is public and `imports/` holds real exports; nothing from them is reproduced in a
  tracked file.
- Every new test is gated against a mutant.

---

## 10. Known gaps this design creates

1. **Corporate actions are proposed, never imported.** A user who ignores the proposals gets an
   import whose positions are wrong for the affected instruments. D3 accepts this in exchange
   for the accounts being importable at all; the mitigation is entirely that the report is
   loud.
2. **Cash in lieu of fractional shares is recognised but not applied.** It moves real cash and
   is now visible in the report, but the ledger's cash and realised P&L do not reflect it.
   Same arithmetic as gap #35's merger cash.
3. **CUSIPs are never stored.** Resolution is advisory and per-import; the mapping a user
   supplies by filling in a symbol is not remembered, so the next import proposes the same
   unresolved action again.
4. **The REOR grouping is venue-specific and format-fragile.** It is Fidelity's reference in
   Fidelity's format. A change to that format degrades grouping to the `(ex-date, CUSIP pair)`
   fallback, which cannot distinguish two actions on one instrument on one date.
5. **Ongoing incremental imports remain unsolved.** This design covers the multi-year history
   exports. The 90-day Activity & Orders cap still constrains keeping the ledger current, which
   is its own problem.
