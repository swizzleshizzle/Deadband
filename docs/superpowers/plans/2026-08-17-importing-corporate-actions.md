# Importing Corporate Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recognise corporate-action rows in a Fidelity history export so they stop refusing the import, and propose ready-to-run `corporate add` commands for them.

**Architecture:** Four layers, each independently shippable. **Recognition** adds an `Outcome` that produces nothing and does not block — that is the missing half of gap #33. **Grouping** collects the recognised rows into logical actions using the venue's own `#REOR` reference. **Derivation** infers each action's ratio from the paired quantities and keeps the evidence. **Proposal** renders a `corporate add` command per action from `cli.py`, which has the connection needed to complete a spinoff's ratio. Nothing in this plan writes a corporate action.

**Tech Stack:** Python 3.11+, `uv`, asyncpg, pytest.

**Spec:** `docs/superpowers/specs/2026-08-17-importing-corporate-actions-design.md`. Read §1 (what was verified against the real exports, including the two-dialect finding), §5 (grouping) and §6 (derivation) before starting.

## Global Constraints

- **Purity.** `ledger/` and `importers/` import no I/O, no clock, no randomness, and not the first-party `db`/`venues` packages. `tests/test_purity.py` enforces it. The proposal is **data**; `cli.py` renders it. **This plan changes nothing in `ledger/`.**
- **`Decimal`, never `float`.**
- **The clock lives in `cli.py`.** `importers/` and `db/` never call `datetime.now()`.
- **Nothing in this plan stores a corporate action.** Every path ends in a proposal or a report. If you find yourself calling `add_action`, stop and report.
- **Refusals write nothing and exit non-zero (exit 2).**
- **No schema change.** CUSIP resolution is advisory (spec D7); no CUSIP column is added.
- **The test database is SHARED and PERSISTENT**, and `instrument` rows are global. Never assert on an unqualified `SELECT count(*)`; scope assertions to rows the test created, and probe only through the transaction-rolled-back `conn` fixture.
- **DB tests skip silently without `TEST_PG_DSN`.** Always `set -a && . ./.env && set +a && uv run pytest <file>`, and read the summary line to confirm it says neither "skipped" nor a stale count.
- **Run tests in the FOREGROUND**, with a generous timeout (600000 ms). **Never background or monitor a test run** — three implementers on this project have stalled indefinitely doing exactly that, each despite an explicit instruction not to. See `/root/.claude/CLAUDE.md`.
- **Do not run the full suite** (~10 minutes; the controller runs it). **Name the test FILE in selectors, never a `-k` substring.**
- **Every new test is gated against a mutant.** Report each CAUGHT or SURVIVED honestly.
- **This repo is PUBLIC and `imports/` holds real exports.** Every fixture value — symbol, CUSIP, quantity, date, reorganisation reference, account number — must be **fabricated**. The established convention is `ZXCO` / "ZEPHYR EXPLORATION CO". The deny-list guards identifiers, not values; cross-check numeric tokens against `imports/` before committing, and note that short decimals collide constantly, so check whether a match is a standalone field value before concluding anything.

---

## File Structure

| File | Responsibility |
|---|---|
| `tests/fixtures/fidelity/real_shape_history.csv` | **new** — the first History-dialect fixture, fabricated, carrying all four action types plus cash-in-lieu |
| `importers/fidelity.py` | modify: `Outcome.CORPORATE_ACTION`, five `Rule`s, row collection, grouping, derivation |
| `importers/base.py` | modify: the proposal dataclass and two `ImportBatch` fields |
| `cli.py` | modify: `cmd_import` renders proposals and completes the spinoff ratio; `--account` help text corrected |
| `tests/test_fidelity_history.py` | **new** — the dialect and recognition |
| `tests/test_fidelity.py` | modify: rule reachability |
| `tests/db/test_cli.py` | modify: the proposal surface |
| `docs/known-gaps.md`, `README.md` | modify |

---

## Task 1: The History dialect, and recognition

**Files:**
- Create: `tests/fixtures/fidelity/real_shape_history.csv`, `tests/test_fidelity_history.py`
- Modify: `importers/fidelity.py`
- Test: `tests/test_fidelity_history.py`, `tests/test_fidelity.py`

**Interfaces:**
- Consumes: `Outcome`, `Rule`, `RULES`, `classify` (existing).
- Produces: `Outcome.CORPORATE_ACTION`; five `Rule` entries named `reverse_split`, `name_change`, `merger`, `spinoff_distribution`, `cash_in_lieu`.

**Read first:** `importers/fidelity.py`'s `Outcome` enum — especially the `INTERNAL` member's comment and the `investment_gain_loss` rule's comment. That rule is the precedent for this whole task: a money-carrying row that no rule matched, which blocked the commit, and which a real export "could not be imported at all until this rule existed."

**Why this is not `INTERNAL`.** `INTERNAL` means recognised and deliberately produces nothing. These rows produce nothing *and* have a follow-up action a later task will emit. They need their own outcome so grouping can find them.

**Why this is not `UNSUPPORTED`.** That means recognised and deliberately refused, and it blocks unconditionally. The whole point here is that the import proceeds.

**No fixture covers the History dialect** — both existing ones are Activity dialect, with `Account`/`Account Number` columns. That is why nobody noticed history exports yield `external_ref=None`. The fixture you create is the first coverage of the dialect that actually holds corporate actions, and it is worth more than this task alone.

- [ ] **Step 1: Build the fixture**

`tests/fixtures/fidelity/real_shape_history.csv`, in the **History** dialect. Its header is, exactly:

```
Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date
```

Note what it does **not** have: `Account` and `Account Number`. Copy the preamble shape from `tests/fixtures/fidelity/real_shape_activity.csv` — a BOM, then a blank line, then the header on the third line — and give it a trailing legal-disclaimer block, because real exports have one and `_locate_header` plus the unmapped-row policy must cope with it.

Include, with **entirely fabricated** values:
- an ordinary BUY and an ordinary dividend, so the file proves the dialect parses at all;
- a reverse split as two rows, one negative quantity and one positive, sharing a `#REOR` reference;
- a name change as two rows, same shape;
- a merger as three rows sharing one `#REOR` reference;
- a spinoff distribution as a single positive row;
- a cash-in-lieu row.

Fabricate the CUSIPs (nine alphanumerics, e.g. `ZXC000001`), the reorganisation references, the quantities and the dates. Use `ZXCO` / `ZEPHYR EXPLORATION CO` and a second fabricated instrument for the resulting side. **Do not copy any value from `imports/`.**

- [ ] **Step 2: Write the failing tests**

`tests/test_fidelity_history.py`:

```python
import pathlib

from importers.fidelity import FidelityImporter

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "fidelity" / "real_shape_history.csv"


def _batch():
    return FidelityImporter().parse(FIXTURE.read_text())


def test_the_history_dialect_parses_at_all():
    """The multi-year history export is a different dialect from every existing
    fixture -- no Account/Account Number columns, a Cash Balance column instead.
    Nothing covered it before this file, which is why external_ref=None went
    unnoticed."""
    batch = _batch()
    assert batch.fills
    assert batch.cash


def test_history_rows_carry_no_account_ref():
    """Not a defect -- the account is in the filename, not the file. Pinned
    because `import --account` is the only way these rows route, and a future
    change that started inventing a ref would silently route them somewhere."""
    batch = _batch()
    assert {f.external_ref for f in batch.fills} == {None}
    assert batch.refs_seen == ()


def test_corporate_action_rows_do_not_block_the_import():
    """Gap #33's actual acceptance test. These rows carry a nonzero quantity and
    matched no rule, so the money-carrying-unmapped policy blocked the whole
    import -- which is why two accounts could never be imported."""
    assert _batch().blocking == ()


def test_corporate_action_rows_produce_no_fill_and_no_cash():
    """Recognised and deferred, not recognised and recorded. A corporate action
    is not a trade and not a cash movement; the follow-up is a `corporate add`
    proposal a later task emits."""
    batch = _batch()
    refs = {f.venue_fill_id for f in batch.fills if f.venue_fill_id}
    assert not any("REOR" in (r or "") for r in refs)
```

Add to `tests/test_fidelity.py`, beside the existing reachability test:

```python
def test_the_corporate_action_rules_are_all_reachable():
    """RULES is first-match-wins. A corporate-action verb shadowed by an earlier
    rule silently reverts to the blocking behaviour this task removes, and every
    test above would still pass."""
    for name in ("reverse_split", "name_change", "merger",
                 "spinoff_distribution", "cash_in_lieu"):
        rule = next(r for r in RULES if r.name == name)
        assert classify(rule.verb, "") is rule
```

- [ ] **Step 3: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/test_fidelity_history.py tests/test_fidelity.py -v`
Expected: FAIL — `blocking` is non-empty, because the corporate-action rows match no rule.

- [ ] **Step 4: Implement**

Add the outcome, with a comment explaining the two-way distinction:

```python
    # Recognised, produces nothing, and does NOT block -- the row's follow-up
    # is a `corporate add` proposal, not a fill or a cash movement.
    #
    # Distinct from INTERNAL, which also produces nothing but has no follow-up,
    # and from UNSUPPORTED, which is recognised and deliberately REFUSED. These
    # rows carry a nonzero quantity, so before this existed they hit the
    # money-carrying-unmapped policy and refused the entire import -- the same
    # shape investment_gain_loss was added for, and the reason two accounts
    # could not be imported at all.
    CORPORATE_ACTION = "corporate_action"
```

Add the five rules to `RULES`, **before** any rule whose verb could shadow them, and give each the verb prefix observed in the export. Confirm placement against `test_every_rule_is_reachable` rather than by eye.

Then, in the row loop, treat `Outcome.CORPORATE_ACTION` as producing nothing and — critically — as **not** contributing to `blocking`.

- [ ] **Step 5: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/test_fidelity_history.py tests/test_fidelity.py tests/test_fidelity_real_shape.py -v`

Run the existing real-shape test too: you changed `RULES` ordering, which every dialect shares.

- [ ] **Step 6: Mutation gate**

- Give `reverse_split` the outcome `Outcome.UNSUPPORTED` → `test_corporate_action_rows_do_not_block_the_import` must FAIL.
- Move the corporate-action rules to the **end** of `RULES`, after `expired_option` → report what reddens. If nothing does, say so: it means no existing verb shadows them and the ordering claim is unpinned by this fixture.
- Remove the `cash_in_lieu` rule entirely → `test_corporate_action_rows_do_not_block_the_import` must FAIL, since that row carries money.

- [ ] **Step 7: Commit**

```bash
git add importers/fidelity.py tests/fixtures/fidelity/real_shape_history.csv tests/test_fidelity_history.py tests/test_fidelity.py
git commit -m "feat(importers): recognise corporate-action rows so they stop refusing the import"
```

---

## Task 2: Grouping

**Files:**
- Modify: `importers/base.py`, `importers/fidelity.py`
- Test: `tests/test_fidelity_history.py`

**Interfaces:**
- Consumes: `Outcome.CORPORATE_ACTION` (Task 1).
- Produces:
  ```python
  # importers/base.py
  @dataclass(frozen=True, slots=True)
  class CorporateActionProposal:
      kind: str                       # 'reverse_split' | 'name_change' | 'merger' | 'spinoff'
      ex_date: date
      source_cusip: str | None
      resulting_cusip: str | None
      description: str                # the venue's own text, for a human to identify it
      quantities: tuple[Decimal, ...] # the evidence the ratio was derived from
      ratio: tuple[Decimal, Decimal] | None = None   # filled by Task 3; None until then
      group_ref: str | None = None    # the #REOR reference, or None when the fallback keyed it

  # ImportBatch gains:
      corporate_actions: tuple[CorporateActionProposal, ...] = ()
      cash_in_lieu: tuple[str, ...] = ()
  ```

**Read spec §5 before starting.** It says explicitly: **the `#REOR` token format is not pinned and must not be guessed.** It varies — some rows carry a letter prefix — and a plausible "last digit is the leg index" reading does **not** survive contact with all the data. Derive the format from the fixture you built and pin it with tests; fall back to `(ex-date, CUSIP pair)` when a row has no usable REOR token.

**Group shapes:** two legs for a reverse split and a name change, three for a merger, one for a spinoff. A group matching none of these is reported as unrecognised rather than coerced (spec §7).

- [ ] **Step 1: Write the failing tests**

```python
def test_each_reorganisation_becomes_one_proposal():
    """Grouping is on the venue's own #REOR reference -- Fidelity stating which
    rows are one event -- not on inference from date and CUSIP."""
    kinds = [p.kind for p in _batch().corporate_actions]
    assert sorted(kinds) == ["merger", "name_change", "reverse_split", "spinoff"]


def test_the_three_row_merger_is_one_proposal_not_three():
    """A merger arrives as three rows. Grouping on the REOR reference handles
    that without a special case; grouping on (date, cusip) would not."""
    merger = next(p for p in _batch().corporate_actions if p.kind == "merger")
    assert len(merger.quantities) == 3


def test_the_single_row_spinoff_is_one_proposal():
    """A spinoff has no negative leg -- it adds the child without removing the
    parent. Gap #33 and the previous design both call these FROM/TO pairs; that
    is true of the other three types and false of this one."""
    spinoff = next(p for p in _batch().corporate_actions if p.kind == "spinoff")
    assert len(spinoff.quantities) == 1


def test_a_group_of_an_unexpected_shape_is_reported_not_coerced():
    """Forcing an unknown shape into the nearest match is how a wrong ratio gets
    proposed with confidence."""
    batch = FidelityImporter().parse(_fixture_with_a_stray_reorganisation_leg())
    assert any("unrecognised" in w.lower() for w in batch.warnings)


def test_cash_in_lieu_is_reported_separately_from_the_proposals():
    """It moves real cash and needs gap #35's arithmetic. Listing it beside the
    proposals would imply an action the user can record."""
    batch = _batch()
    assert batch.cash_in_lieu
    assert not any(p.kind == "cash_in_lieu" for p in batch.corporate_actions)
```

`_fixture_with_a_stray_reorganisation_leg()` returns the fixture text with one extra corporate-action row appended carrying a REOR reference that pairs with nothing — build it by string manipulation in the test, not by adding a fourth fixture file.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/test_fidelity_history.py -v`
Expected: FAIL — `ImportBatch` has no attribute `corporate_actions`.

- [ ] **Step 3: Implement**

Add `CorporateActionProposal` to `importers/base.py` and the two `ImportBatch` fields, each with a comment saying what it is for — follow `refs_seen`'s and `blocking`'s existing comments, which explain *why the field exists*, not what it holds.

In `importers/fidelity.py`, collect `CORPORATE_ACTION` rows as they are classified, then group them after the row loop. Derive the REOR parse from the fixture. Where a group's shape matches no known action, emit a warning naming the group and its rows and **do not** emit a proposal.

Leave `ratio` as `None` throughout this task — Task 3 fills it.

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/test_fidelity_history.py tests/test_fidelity.py tests/test_fidelity_real_shape.py -v`

- [ ] **Step 5: Mutation gate**

- Group on `(ex_date, cusip_pair)` only, ignoring the REOR reference → `test_the_three_row_merger_is_one_proposal_not_three` must FAIL.
- Coerce an unknown group shape to the nearest known one → `test_a_group_of_an_unexpected_shape_is_reported_not_coerced` must FAIL.
- Put cash-in-lieu rows into `corporate_actions` → `test_cash_in_lieu_is_reported_separately_from_the_proposals` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add importers/base.py importers/fidelity.py tests/test_fidelity_history.py
git commit -m "feat(importers): group corporate-action rows on the venue's reorganisation reference"
```

---

## Task 3: Derivation

**Files:**
- Modify: `importers/fidelity.py`
- Test: `tests/test_fidelity_history.py`

**Interfaces:**
- Consumes: `CorporateActionProposal` (Task 2).
- Produces: no new API. `CorporateActionProposal.ratio` is populated for every kind **except** `spinoff`.

**Read spec §6.** The spinoff's ratio is child shares against the parent holding at the ex-date, and the parent holding is not in the file — only `cli.py`, which has a connection, can complete it. That is why `ratio` is `| None` rather than required, and it is Task 4's job.

**Every proposal keeps the quantities it derived from.** The ratio is an inference; the quantities are evidence. Cash-in-lieu means a reverse split's quantities need not be an exact multiple, so a derived ratio can be slightly off — and getting `--ratio` backwards is wrong by a factor of 36.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_reverse_split_ratio_is_derived_and_reduced():
    """NEW:OLD, reduced to the smallest integer pair -- the direction
    adjust_fills consumes. Inverting it turns a reverse split into a forward
    one and is wrong by the square of the ratio."""
    split = next(p for p in _batch().corporate_actions if p.kind == "reverse_split")
    assert split.ratio == (Decimal(1), Decimal(6))


def test_a_name_change_ratio_is_one_to_one():
    change = next(p for p in _batch().corporate_actions if p.kind == "name_change")
    assert change.ratio == (Decimal(1), Decimal(1))


def test_a_spinoff_carries_no_ratio_out_of_the_importer():
    """Not derivable from the file: the row carries only the child shares, and
    the ratio needs the parent holding at the ex-date. cli.py completes it."""
    spinoff = next(p for p in _batch().corporate_actions if p.kind == "spinoff")
    assert spinoff.ratio is None


def test_every_proposal_keeps_the_quantities_it_derived_from():
    """The ratio is an inference; the quantities are the evidence. A reverse
    split whose quantities do not reduce cleanly -- the cash-in-lieu case -- is
    exactly when a human needs to see both."""
    for proposal in _batch().corporate_actions:
        assert proposal.quantities


def test_a_ratio_that_does_not_reduce_cleanly_is_flagged():
    """Fractional remainders are paid out as cash in lieu, so raw quantities
    need not be an exact multiple. Silently rounding would propose a confident
    wrong ratio."""
    batch = FidelityImporter().parse(_fixture_with_a_fractional_split())
    split = next(p for p in batch.corporate_actions if p.kind == "reverse_split")
    assert split.approximate is True
```

`_fixture_with_a_fractional_split()` amends the fixture's reverse-split quantities so they do not reduce to a clean pair. `approximate: bool = False` is a new field on `CorporateActionProposal`.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/test_fidelity_history.py -v`
Expected: FAIL — `ratio` is `None` for every proposal.

- [ ] **Step 3: Implement**

Derive per spec §6's table. Reduce with `math.gcd` over integer quantities, or by normalising the `Decimal` fraction — whichever you choose, `Decimal` in and `Decimal` out, never `float`. Set `approximate=True` when the reduction is not exact.

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/test_fidelity_history.py tests/test_fidelity.py -v`

- [ ] **Step 5: Mutation gate**

- Emit the ratio as OLD:NEW → `test_a_reverse_split_ratio_is_derived_and_reduced` must FAIL. **This is the most important mutation in the plan**: the inverted ratio is the failure the spec singles out as plausible at every step.
- Skip the reduction, emitting raw quantities as the ratio → the same test must FAIL.
- Never set `approximate` → `test_a_ratio_that_does_not_reduce_cleanly_is_flagged` must FAIL.
- Derive a ratio for the spinoff from its single quantity → `test_a_spinoff_carries_no_ratio_out_of_the_importer` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add importers/fidelity.py tests/test_fidelity_history.py
git commit -m "feat(importers): derive corporate-action ratios and keep the evidence"
```

---

## Task 4: The proposal surface

**Files:**
- Modify: `cli.py`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `ImportBatch.corporate_actions`, `ImportBatch.cash_in_lieu` (Tasks 2-3).
- Produces: `cmd_import` output only. No new API, and **nothing is written**.

**Read `cmd_import` first**, especially its `pool.close()` comment — `close()` runs after the `async with pool.acquire()` block exits, never inside it, or it deadlocks.

**This task completes the spinoff's ratio** by reading the parent holding at the ex-date from the ledger. It is the one place a proposal is finished outside the pure layer.

**Also fix `--account`'s help text.** It currently says a venue carrying its own per-row account number — naming Fidelity — "routes automatically and does not need this." That is true of the Activity & Orders dialect and **false** of the History dialect, which carries no account column and is the only dialect containing corporate actions. It misleads exactly the user trying to import those files.

- [ ] **Step 1: Write the failing tests**

Use `tests/db/test_cli.py`'s established idioms: a module-level `_FakePool`, a per-test local `async def fake_create_pool(*_a, **_kw): return _FakePool(conn)` monkeypatched over `cli.create_pool`, and `capsys`.

```python
async def test_import_proposes_a_corporate_add_command(conn, monkeypatch, capsys):
    """The importer proposes and never stores: a corporate action silently
    restates history across every account holding the instrument, which is why
    `corporate add` previews by default and refuses duplicates."""
    ...
    out = capsys.readouterr().out
    assert "corporate add" in out
    assert "--ratio 1:6" in out


async def test_the_proposal_prints_the_evidence_it_derived_from(conn, monkeypatch, capsys):
    """A ratio is an inference. Printing the quantities beside it is the one
    moment a human can catch an inverted or distorted one before it is stored."""
    ...
    assert "1800" in capsys.readouterr().out


async def test_nothing_is_written_by_a_proposal(conn, monkeypatch):
    """Not even with --commit. The import commits fills and cash; corporate
    actions are proposed only."""
    ...
    assert await list_actions(conn) == []


async def test_the_spinoff_ratio_is_completed_from_the_ledger(conn, account_with_1800,
                                                              monkeypatch, capsys):
    """Not derivable from the file -- the row carries only the child shares."""
    ...


async def test_cash_in_lieu_is_reported_as_unapplied(conn, monkeypatch, capsys):
    """It moves real cash and the ledger does not reflect it. Saying so is the
    whole mitigation."""
    out = capsys.readouterr().out
    assert "not applied" in out.lower()
```

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`
Expected: FAIL — nothing renders proposals. Run the whole file; do not use an unverified `-k`.

- [ ] **Step 3: Implement**

Render a clearly separated section after the trade summary: one `corporate add` line per proposal, each preceded by its evidence and the venue's description text. List cash-in-lieu rows in their own subsection, stating they are recognised but not applied and pointing at gap #35.

Complete the spinoff ratio by reading the parent holding at the ex-date. Where it cannot be determined, print the proposal with the ratio blank and say why (spec §7).

Correct `--account`'s help text.

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`

- [ ] **Step 5: Mutation gate**

- Store the proposals instead of printing them → `test_nothing_is_written_by_a_proposal` must FAIL. This pins the plan's central constraint.
- Print the ratio without the quantities → `test_the_proposal_prints_the_evidence_it_derived_from` must FAIL.
- Fold cash-in-lieu into the proposal list → `test_cash_in_lieu_is_reported_as_unapplied` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add cli.py tests/db/test_cli.py
git commit -m "feat(cli): propose corporate actions found during an import"
```

---

## Task 5: Documentation

**Files:**
- Modify: `docs/known-gaps.md`, `README.md`

- [ ] **Step 1: Find the current highest gap number.**

`grep -oE '^\| [0-9]+ \|' docs/known-gaps.md | tail -3`. **Check, do not assume** — this file has had two renumbering incidents and one session where two branches each claimed the same number. **Prefer symbol references over line ranges** in anything you write: line citations in this file have gone stale on every branch, and forty were corrected on the last one.

- [ ] **Step 2: Close gap #33** — corporate actions can now be imported, in the sense that they are recognised, no longer refuse the import, and are proposed. Record what remains true: they are proposed, never stored.

- [ ] **Step 3: Record the gaps the spec's §10 names**, each in its own row, matching neighbouring rows' format and depth of reasoning (read several first — those rows carry real argument):

1. **Actions are proposed, never imported.** A user who ignores the proposals gets an import whose positions are wrong for the affected instruments. The mitigation is entirely that the report is loud.
2. **Cash in lieu of fractional shares is recognised but not applied.** It moves real cash; the ledger's cash and realised P&L do not reflect it. Same arithmetic as gap #35's merger cash — cross-reference rather than duplicating.
3. **CUSIPs are never stored.** Resolution is advisory and per-import, so the next import proposes the same unresolved action again.
4. **The reorganisation-reference grouping is venue-specific and format-fragile.** A change to Fidelity's format degrades grouping to the `(ex-date, CUSIP pair)` fallback, which cannot distinguish two actions on one instrument on one date.
5. **Ongoing incremental imports remain unsolved.** This covers the multi-year history exports; the 90-day Activity & Orders cap still constrains keeping the ledger current.

- [ ] **Step 4: README** — document that `import` reports corporate actions it finds and proposes `corporate add` commands, that nothing is stored automatically and why, and that a history-dialect export needs `--account` because it carries no account column.

- [ ] **Step 5: Verify and commit.** Open every file you cite and confirm it resolves. **Cross-check every numeric token and symbol in your diff against `imports/`** and report the command. Stage, run `.githooks/pre-commit` without bypassing it, and confirm `git diff --cached --stat` shows only documentation.

```bash
git add docs/known-gaps.md README.md
git commit -m "docs: corporate actions are recognised on import and proposed"
```

---

## Self-Review

**Spec coverage.** §1's two-dialect finding → Task 1's fixture and `test_history_rows_carry_no_account_ref`, and Task 4's `--account` help fix. D1 (recognition separated, and the missing half of gap #33) → Task 1. D2 (propose, never store) → Task 4, `test_nothing_is_written_by_a_proposal`. D3 (import completes) → Task 1, `test_corporate_action_rows_do_not_block_the_import`. D4 (group on REOR) → Task 2 and its first mutation. D5 (print the evidence) → Task 3's `test_every_proposal_keeps_the_quantities_it_derived_from` and Task 4's evidence test. D6 (cash-in-lieu recognised, not applied) → Tasks 1, 2 and 4. D7 (degrade, do not fail) → Task 4's blank-ratio path. §5's "do not guess the REOR format" → Task 2's Step 3. §6's derivation table → Task 3. §7's failure policy → Tasks 2 and 4. §10's gaps → Task 5.

**Placeholders.** Task 4's test bodies are elided with `...` where the setup is the file's existing `_FakePool` idiom rather than new code; every assertion is given. Tasks 1-3 carry their tests in full.

**Type consistency.** `CorporateActionProposal` is defined in Task 2 with `ratio: tuple[Decimal, Decimal] | None`, populated in Task 3 for every kind but `spinoff`, and completed in Task 4 from the ledger. `approximate: bool` is added in Task 3, where its test lives. `ImportBatch.corporate_actions` and `.cash_in_lieu` are defined in Task 2 and consumed in Task 4. `Outcome.CORPORATE_ACTION` is defined in Task 1 and consumed in Task 2.

**Known soft spot.** Task 2's REOR parse is the least specified thing in the plan, deliberately: the spec forbids guessing the format, and my own attempt to read a leg index out of it did not survive the data. The implementer derives it from the fixture it built in Task 1 — which means a fixture that encodes a *wrong* guess about the format would validate a parser that fails on real files. Task 2's implementer should re-read the real export's reference format before trusting the fixture, and say in its report what format it found.
