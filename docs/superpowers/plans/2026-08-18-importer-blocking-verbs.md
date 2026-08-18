# Unblocking the Importer's Four Non-Corporate Verbs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recognise the four non-corporate Fidelity verbs that still refuse the real history imports, so four of the five blocked files import cleanly and the fifth blocks on `TRANSFER OF ASSETS ACAT` alone.

**Architecture:** Three new rules in the existing `RULES` table cover the two retirement cash verbs and the plain `DISTRIBUTION`; the distribution reuses the existing corporate-action proposal path with a `split` kind whose ratio is completed from the ledger. A new pre-pass over the parsed rows nets broker amendment clusters (original → cancel → correction) down to a single fill dated to its as-of date, which also fixes a duplicate-fill defect that exists today.

**Tech Stack:** Python 3.10, `Decimal` arithmetic, asyncpg + Postgres, pytest with `pytest-asyncio`, `uv` for dependency management.

**Spec:** [`docs/superpowers/specs/2026-08-18-importer-blocking-verbs-design.md`](../specs/2026-08-18-importer-blocking-verbs-design.md)

## Global Constraints

- **Decimal, never float.** Every quantity, price, fee and amount is a `Decimal`. `InvalidOperation` is not a subclass of `ValueError` — catch it by name. Guard non-finite values with `is_finite()`; `Decimal("NaN")` and `Decimal("Infinity")` construct without raising.
- **DB tests skip silently without `TEST_PG_DSN`.** Every DB test run is `set -a && . ./.env && set +a && uv run pytest <file>`. Read the summary line and confirm it says neither "skipped" nor a stale count. A green run without the DSN silently skips roughly sixty tests.
- **Name the test FILE in every selector.** Never a bare `-k` substring — it silently selects zero tests and reports success.
- **Run tests in the FOREGROUND.** Call the command directly and let it block; a 600000 ms tool timeout covers it. Piped output buffers, so an empty output file mid-run is expected and is not evidence of a hang. DB test files take 3-4 minutes each.
- **Mutation gate on every new test.** Apply the named mutation, confirm the test fails (CAUGHT), revert it, confirm green. Report SURVIVED honestly — never quietly re-run until it passes. Revert every mutation before committing; verify with `git status` and `git diff`.
- **Fabricated symbols only.** `ZXCO`, `ZXQ`, `ZXRT`, `ZXDS` and the option symbol this plan names. Never a real ticker. Hygiene is **values-based, not identifier-based**: the deny-list and pre-commit hook guard identifiers, not values, and real quantities, dates and option symbols have reached drafts three times. Before any commit touching docs or fixtures, cross-check every numeric token and symbol against `imports/`.
- **This repository is public.** No real account number, symbol, quantity, price or balance in any tracked file, commit message, or PR body.
- **Never `git commit --amend`** and never rewrite history. Add a new commit.
- The importer's pure layer (`importers/`) never opens a database connection. Ledger reads belong in `cli.py`.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `importers/fidelity.py` | Rule table, row classification, amendment pre-pass, corporate-action row collection | 1, 2, 4 |
| `importers/base.py` | `CorporateActionProposal` — gains `subject_symbol` | 2 |
| `cli.py` | Split-ratio completion from the ledger, proposal rendering, netting summary | 3, 4 |
| `tests/fixtures/fidelity/real_shape_history.csv` | Shape fixture for the History dialect — gains four verb rows | 1, 2 |
| `tests/fixtures/fidelity/amendment_cluster.csv` | New: a self-contained three-row amendment story | 4 |
| `tests/test_fidelity_history.py` | Pure-layer tests for the History dialect | 1, 2, 4 |
| `tests/db/test_cli.py` | Ledger-backed CLI tests | 3, 4 |
| `docs/known-gaps.md` | Gap register | 5 |

---

## Task 1: Retirement cash verbs

**Files:**
- Modify: `importers/fidelity.py` — the `RULES` tuple
- Modify: `tests/fixtures/fidelity/real_shape_history.csv`
- Test: `tests/test_fidelity_history.py`

**Interfaces:**
- Consumes: the existing `Rule` dataclass and `Outcome.CASH`.
- Produces: nothing later tasks depend on. This task is deliberately first because it is the smallest complete change and proves the fixture-plus-rule loop works.

- [ ] **Step 1: Append two fixture rows**

Append these two lines to `tests/fixtures/fidelity/real_shape_history.csv`. The header is already:
`Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date`

```csv
03/04/2026,ROLLOVER CASH CHECK RECEIVED IRA DIR ROLOVR (Cash),,,Cash,,0,,,,1500,4210.55,
03/05/2026,EARLY DIST NO EXCEPT VS ZX99-999999-9 CASH (Cash),,,Cash,,0,,,,-500,3710.55,
```

Both quantities are zero and both amounts are non-zero, which is the shape that makes an unmapped row block. The account reference in the `EARLY DIST` text is fabricated — confirm no substring of it appears in `imports/`.

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_fidelity_history.py`:

```python
def test_a_rollover_cash_check_is_a_deposit():
    """A retirement rollover is cash arriving from outside the ledger. It
    carries a zero quantity and a real amount, which is exactly the shape
    that blocks an import while unmapped -- three such rows across the real
    exports are why two accounts could not be imported."""
    batch = FidelityImporter().parse(_fixture_text())
    deposits = [c for c in batch.cash if c.kind == "deposit" and c.amount == Decimal("1500")]
    assert len(deposits) == 1
    assert deposits[0].occurred_at.date() == date(2026, 3, 4)
    assert not [m for _, m in batch.blocking if "ROLLOVER" in m]


def test_an_early_distribution_is_a_withdrawal():
    """Money leaving a retirement account. Recorded as a positive amount
    under an outflow kind -- see importers.base.OUTFLOW_KINDS, which is why
    the export's own negative sign must NOT leak through."""
    batch = FidelityImporter().parse(_fixture_text())
    withdrawals = [
        c for c in batch.cash if c.kind == "withdrawal" and c.amount == Decimal("500")
    ]
    assert len(withdrawals) == 1
    assert withdrawals[0].amount > 0, "OUTFLOW_KINDS amounts are always positive"
    assert not [m for _, m in batch.blocking if "EARLY DIST" in m]
```

If `_fixture_text()` does not already exist in that file, use whatever helper the file already uses to load `real_shape_history.csv`; do not introduce a second loader.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fidelity_history.py -v`
Expected: both FAIL — the rows are unmapped, so no `deposit`/`withdrawal` movement exists and `batch.blocking` carries an `unhandled action` entry for each.

- [ ] **Step 4: Add the two rules**

In `importers/fidelity.py`, insert into `RULES` immediately after the `Rule("contributions", ...)` line, keeping the existing retirement-contribution cluster together:

```python
    # Retirement cash flows. D1: these map to the GENERIC kinds, not to
    # retirement-specific ones -- cash_movement.kind is a CHECK constraint
    # with no retirement value in it, and the four contribution rules above
    # already collapse the same way. A later tax-reporting feature wanting
    # the distinction back recovers it from the note.
    #
    # The verb is "ROLLOVER CASH CHECK", not the shorter "ROLLOVER": both
    # observed variants (one carries a trailing MOBILE DEPOSIT) share that
    # prefix, and the narrower one does not speculate about ROLLOVER verbs
    # the exports have never shown.
    Rule("rollover_deposit", "ROLLOVER CASH CHECK", Outcome.CASH, cash_kind="deposit"),
    Rule("early_distribution", "EARLY DIST", Outcome.CASH, cash_kind="withdrawal"),
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fidelity_history.py -v`
Expected: PASS.

- [ ] **Step 6: Run the sibling pure-layer suites**

Run: `uv run pytest tests/test_fidelity.py tests/test_fidelity_real_shape.py tests/test_fidelity_history.py -v`
Expected: PASS. Two rows were added to a shared fixture, so any test asserting a **total** row, fill, cash, warning or unmapped count over `real_shape_history.csv` will now be off by two. That is expected. Update each such assertion deliberately and name every one you changed in your report — do not adjust a count you have not read the surrounding test for.

- [ ] **Step 7: Mutation gate**

Apply each mutation, run `uv run pytest tests/test_fidelity_history.py`, record CAUGHT or SURVIVED, then revert:

1. Change `cash_kind="deposit"` to `cash_kind="withdrawal"` on `rollover_deposit`.
2. Change the `early_distribution` verb from `"EARLY DIST"` to `"EARLY DISTRIBUTION"` (which no real row starts with).

Both must be CAUGHT. Verify `git diff` is clean of mutations before the commit.

- [ ] **Step 8: Commit**

```bash
git add importers/fidelity.py tests/fixtures/fidelity/real_shape_history.csv tests/test_fidelity_history.py
git commit -m "feat(importers): recognise retirement rollover and early-distribution cash"
```

---

## Task 2: Recognise a plain DISTRIBUTION as a share distribution

**Files:**
- Modify: `importers/fidelity.py` — `RULES`, `_KIND_BY_RULE_NAME`, `_EXPECTED_LEG_COUNT`, the `Outcome.CORPORATE_ACTION` branch in `parse()`
- Modify: `importers/base.py` — `CorporateActionProposal`
- Modify: `tests/fixtures/fidelity/real_shape_history.csv`
- Test: `tests/test_fidelity_history.py`

**Interfaces:**
- Consumes: `Outcome.CORPORATE_ACTION`, `_CorporateActionRow`, `_group_corporate_actions`.
- Produces, for Task 3:
  - `CorporateActionProposal.subject_symbol: str | None` — the ticker the distribution was received on, taken from the row's `Symbol` column. `None` for every other kind.
  - a proposal with `kind == "split"`, `ratio is None`, and `quantities == (received,)` where `received` is the row's positive quantity.

- [ ] **Step 1: Append the fixture row**

Append to `tests/fixtures/fidelity/real_shape_history.csv`:

```csv
03/06/2026,DISTRIBUTION ZXDS HOLDINGS SPON ADS EA... (ZXDS) (Cash),ZXDS,ZXDS HOLDINGS SPON ADS EACH REP 1 ORD SHS,Cash,,40,,,,168,3878.55,
```

Quantity positive, amount positive, symbol present — the real shape. `ZXDS` is fabricated; confirm it does not appear in `imports/`.

- [ ] **Step 2: Write the failing tests**

```python
def test_a_plain_distribution_is_proposed_as_a_split():
    """A DISTRIBUTION with no SPINOFF marker delivers SHARES, not money --
    the export's Amount column on this row is the market value of the shares
    received, verified against the real exports by cash-balance continuity.
    So it belongs to the split family, and the ratio is NOT derivable from
    the row: the row states what was received, never what it was received
    on. cli completes that from the ledger (Task 3)."""
    batch = FidelityImporter().parse(_fixture_text())
    splits = [p for p in batch.corporate_actions if p.kind == "split"]
    assert len(splits) == 1
    p = splits[0]
    assert p.ex_date == date(2026, 3, 6)
    assert p.quantities == (Decimal("40"),)
    assert p.ratio is None, "not derivable from the row alone"
    assert p.subject_symbol == "ZXDS"
    assert not [m for _, m in batch.blocking if "DISTRIBUTION" in m]


def test_a_spinoff_is_still_a_spinoff_not_a_share_distribution():
    """Ordering guard. classify() is startswith + first-match-wins and
    "DISTRIBUTION" is a proper prefix of "DISTRIBUTION SPINOFF", so a
    share_distribution rule placed BEFORE spinoff_distribution silently
    reclassifies every spinoff in every export. This is unlike the existing
    corporate-action block, whose comment records that its position in RULES
    is not load-bearing -- that comment does not cover this rule."""
    batch = FidelityImporter().parse(_fixture_text())
    kinds = [p.kind for p in batch.corporate_actions]
    assert "spinoff" in kinds
    assert kinds.count("split") == 1


def test_a_distribution_with_no_quantity_still_blocks():
    """D5. A DISTRIBUTION carrying zero quantity has never been observed in
    the real exports, and proposing a split derived from no shares would be
    a guess dressed as a derivation. Two observed rows is thin evidence to
    generalise a verb from; this guard is what keeps the generalisation
    honest."""
    text = _fixture_text() + (
        "\n03/07/2026,DISTRIBUTION ZXDS HOLDINGS SPON ADS EA... (ZXDS) (Cash),"
        "ZXDS,ZXDS HOLDINGS SPON ADS EACH REP 1 ORD SHS,Cash,,0,,,,25,3903.55,\n"
    )
    batch = FidelityImporter().parse(text)
    assert [p for p in batch.corporate_actions if p.kind == "split"].__len__() == 1
    assert any("03/07/2026" in m or "DISTRIBUTION" in m for _, m in batch.blocking)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fidelity_history.py -v`
Expected: the three new tests FAIL — `CorporateActionProposal` has no `subject_symbol`, and the row is unmapped.

- [ ] **Step 4: Add the proposal field**

In `importers/base.py`, add to `CorporateActionProposal`, beside `parent_symbol`:

```python
    # The ticker a share distribution was received ON -- the row's own Symbol
    # column. Distinct from parent_symbol, which a SPINOFF row states about a
    # DIFFERENT instrument: here the subject and the instrument receiving
    # shares are the same one, which is why the split ratio reads that
    # instrument's own holding rather than another's. None for every other
    # kind, and for any row whose Symbol column is empty.
    subject_symbol: str | None = None
```

- [ ] **Step 5: Add the rule, the kind mapping and the leg count**

In `importers/fidelity.py`:

```python
    # ORDERING IS LOAD-BEARING HERE, unlike the corporate-action block above:
    # classify() is startswith + first-match-wins and "DISTRIBUTION" is a
    # proper prefix of "DISTRIBUTION SPINOFF". This rule MUST stay after
    # spinoff_distribution -- placed before it, every spinoff in every export
    # silently reclassifies as a share distribution.
    Rule("share_distribution", "DISTRIBUTION", Outcome.CORPORATE_ACTION),
```

Place it immediately after `Rule("cash_in_lieu", "IN LIEU OF", Outcome.CORPORATE_ACTION)`, which is after `spinoff_distribution`.

Then extend the two dicts:

```python
_KIND_BY_RULE_NAME: dict[str, str] = {
    "reverse_split": "reverse_split",
    "name_change": "name_change",
    "merger": "merger",
    "spinoff_distribution": "spinoff",
    "share_distribution": "split",
}
```

```python
_EXPECTED_LEG_COUNT: dict[str, int] = {
    "reverse_split": 2,
    "name_change": 2,
    "merger": 3,
    "spinoff": 1,
    # One row: the shares received. There is no second leg -- the holding it
    # was received on is in the ledger, not in the file.
    "split": 1,
}
```

- [ ] **Step 6: Add the shape guard and carry the symbol**

In the `Outcome.CORPORATE_ACTION` branch of `parse()`, after the existing quantity parse and finiteness checks and before the row is appended for grouping, add:

```python
                # D5: only a positive quantity makes a plain DISTRIBUTION a
                # SHARE distribution. A zero-quantity one has never been
                # observed; treat it as unmapped so it blocks and is looked
                # at, rather than proposing a split derived from no shares.
                if rule.name == "share_distribution" and quantity <= 0:
                    reject(
                        row,
                        raw_row,
                        account,
                        line_no,
                        f"line {line_no}: DISTRIBUTION with no positive quantity "
                        f"({quantity}) -- not a share distribution; see gap for D5",
                    )
                    continue
```

Set `subject_symbol` on the built `_CorporateActionRow` (add the field to that dataclass, defaulting to `None`) from the row's `symbol` when `rule.name == "share_distribution"`, and carry it through `_group_corporate_actions` onto the proposal the same way `parent_symbol` already is.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fidelity_history.py tests/test_fidelity.py tests/test_fidelity_real_shape.py tests/test_importer_base.py -v`
Expected: PASS. As in Task 1, fixture-total assertions shift by one row — update deliberately and list them in your report.

- [ ] **Step 8: Mutation gate**

1. Move `Rule("share_distribution", ...)` to immediately **before** `spinoff_distribution`. Expect `test_a_spinoff_is_still_a_spinoff_not_a_share_distribution` to fail, and note whether `test_every_rule_is_reachable` also fails — report both results, because whether the existing reachability test covers this pair is a fact worth recording, not assuming.
2. Change the D5 guard from `quantity <= 0` to `quantity < 0`.
3. Change `_EXPECTED_LEG_COUNT["split"]` from `1` to `2`.

All three must be CAUGHT. Revert each; confirm with `git diff`.

- [ ] **Step 9: Commit**

```bash
git add importers/fidelity.py importers/base.py tests/fixtures/fidelity/real_shape_history.csv tests/test_fidelity_history.py
git commit -m "feat(importers): recognise a plain DISTRIBUTION as a share distribution"
```

---

## Task 3: Complete the split ratio from the ledger

**Files:**
- Modify: `cli.py` — new `_complete_split_ratio`, wired into the same loop that calls `_complete_spinoff_ratio`; `_render_corporate_add_command`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `CorporateActionProposal.subject_symbol` and `.quantities` from Task 2.
- Produces: `async def _complete_split_ratio(conn, account_id: UUID | None, p: CorporateActionProposal) -> tuple[tuple[Decimal, Decimal] | None, str]` — the same `(ratio, note)` contract `_complete_spinoff_ratio` already returns.

**Ruling carried into this task:** for a share distribution the rendered command prints the **known ticker** in `--symbol`, not the `<SYMBOL>` placeholder. `_render_corporate_add_command`'s docstring currently says `--symbol` is always a placeholder; that reasoning is about reorganisation rows, which carry only CUSIPs. A share distribution row states its ticker outright, and printing `<SYMBOL>` beside a ratio derived from that very ticker's holding would be incoherent. Update the docstring to say so. Cost if wrong: one rendered line differs from the other kinds, and reverting it is a one-line change.

- [ ] **Step 1: Write the failing tests**

Add to `tests/db/test_cli.py`, following the fixtures and `_FakePool` pattern the file already uses:

```python
async def test_the_split_ratio_is_completed_from_the_holding(
    conn, monkeypatch, capsys, tmp_path
):
    """(held + received) : held, reduced. The row states only what was
    received; what it was received ON is in the ledger. Holding 60 and
    receiving 40 is 100:60 -> 5:3."""
    acc = await create_account(conn, name="Dist", venue="fidelity", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXDS", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(), account_id=acc, instrument_id=inst,
                executed_at=datetime(2026, 1, 5, tzinfo=UTC), side=Side.BUY,
                quantity=Decimal("60"), price=Decimal("4"), fee=Decimal("0"),
                fee_currency="USD", source=FillSource.MANUAL,
                venue_fill_id="dist-open", is_estimated=False,
            ),
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "ratio: 5:3" in out
    assert "--type split" in out
    assert "--symbol ZXDS" in out
    assert "<FILL IN>" not in out


async def test_the_split_ratio_is_left_blank_when_the_holding_is_absent(
    conn, monkeypatch, capsys, tmp_path
):
    """The year-file carrying the purchase has not been imported. Report and
    stop -- never substitute another instrument, and never guess a ratio."""
    acc = await create_account(conn, name="Empty", venue="fidelity", account_type="cash")

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "--ratio <FILL IN>" in out
    assert "holds no LONG position" in out


async def test_a_short_holding_does_not_qualify_for_a_split_ratio(
    conn, monkeypatch, capsys, tmp_path
):
    """Shares are distributed on shares you are LONG. HAVING SUM(...) > 0,
    not <> 0 -- a net-short holding would otherwise produce a nonsensical
    negative ratio that only cmd_corporate_add's positivity check would
    catch, far downstream of the mistake."""
    acc = await create_account(conn, name="Short", venue="fidelity", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXDS", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(), account_id=acc, instrument_id=inst,
                executed_at=datetime(2026, 1, 5, tzinfo=UTC), side=Side.SELL,
                quantity=Decimal("60"), price=Decimal("4"), fee=Decimal("0"),
                fee_currency="USD", source=FillSource.MANUAL,
                venue_fill_id="dist-short", is_estimated=False,
            ),
        ],
    )

    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    args = argparse.Namespace(
        venue="fidelity",
        file=_write_history_csv(tmp_path, _SHARE_DISTRIBUTION_ROW_FOR_ZXDS),
        account=str(acc),
        commit=True,
    )
    assert await cli.cmd_import(args) == 0

    out = capsys.readouterr().out
    assert "ratio: UNAVAILABLE" in out
    assert "holds no LONG position" in out
```

Define `_SHARE_DISTRIBUTION_ROW_FOR_ZXDS` beside the file's existing row templates as the single CSV line from Task 2's fixture, with an ex-date of `03/06/2026`.

Note on assertions: do **not** write `assert "-60" not in out` or any substring scan over the whole captured stdout. The account UUID is interpolated into these messages, and a `uuid4` containing that substring fails the test spuriously — that pattern is issue #18. Assert on the rendered field.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`
Expected: the three new tests FAIL. Confirm the summary line reports no skips.

- [ ] **Step 3: Implement the completion**

In `cli.py`, beside `_complete_spinoff_ratio`:

```python
async def _complete_split_ratio(
    conn, account_id: UUID | None, p: CorporateActionProposal
) -> tuple[tuple[Decimal, Decimal] | None, str]:
    """A share distribution's ratio is (held + received) : held, and only the
    RECEIVED half is in the file. Spec D2/§5.

    Unlike a spinoff, the subject and the instrument receiving shares are the
    SAME one -- the row's own Symbol column names it -- so this reads that
    instrument's holding rather than another's, and never falls back to
    identifying a holding by elimination.

    The `> 0` long-position rule is deliberate and shared with the spinoff
    completion: shares are distributed on shares you are LONG, so a net-short
    or flat holding does not qualify. `<> 0` would let a short produce a
    negative ratio that only cmd_corporate_add's positivity check would
    catch, far downstream of where the mistake happened.

    Returns (ratio, note) on the same contract as _complete_spinoff_ratio:
    ratio is None when it could not be completed, and note always explains.
    """
```

**First, extract the holding query.** `_complete_spinoff_ratio` already contains
the exact query this needs, with the `HAVING SUM(...) > 0` rule spec §5 requires.
Do not write a second copy — a duplicated logic block is a review defect on its own.
Lift it into a module-level helper and have both completions call it:

```python
async def _long_holdings_as_of(
    conn, account_id: UUID, cutoff: datetime, symbol: str | None
) -> list:
    """Instruments the account is NET LONG as of `cutoff`, optionally
    restricted to one symbol. `> 0`, never `<> 0`: both corporate-action
    completions ask "what shares was this received on?", and a net-short or
    flat holding is not an answer to that question.

    Extracted from _complete_spinoff_ratio so the split completion shares one
    query rather than a second copy that could drift from it.
    """
```

Move the existing SQL body verbatim, parameterising the symbol filter so
`symbol=None` reproduces the elimination path's behaviour exactly, and change
`_complete_spinoff_ratio` to call it. Its tests must still pass unchanged —
if any needs editing, that is a signal the extraction changed behaviour, and
you should say so rather than adjust the test.

**Then the body of `_complete_split_ratio`**, guards in the same order as its
sibling:

```python
    if conn is None:
        return None, (
            "not completed: preview opens no database connection -- rerun "
            "with --commit to complete it from the ledger"
        )
    if account_id is None:
        return None, "not completed: no --account given, so the holding is unknown"
    if not p.quantities or p.subject_symbol is None:
        return None, "not completed: the row states no symbol or no quantity received"

    received = p.quantities[0]
    cutoff = datetime.combine(p.ex_date, time.min, tzinfo=UTC)
    rows = await _long_holdings_as_of(conn, account_id, cutoff, p.subject_symbol)
    if not rows:
        return None, (
            f"not completed: account {account_id} holds no LONG position in "
            f"{p.subject_symbol} as of {p.ex_date.isoformat()} -- import the file "
            "containing that purchase and re-run, rather than deriving a ratio "
            "from a holding that is not there"
        )

    held = rows[0]["quantity"]
    return _reduce_ratio(held + received, held), (
        f"derived from the ledger: {p.subject_symbol} holding at the ex-date "
        "plus the shares this row delivered"
    )
```

Import `_reduce_ratio` from `importers.fidelity` rather than reimplementing
reduction — it already handles `Decimal` GCD. Match the row-field access
(`rows[0]["quantity"]`) to whatever column name the extracted query actually
selects; read it rather than assuming this one.

Wire it into the existing loop that calls `_complete_spinoff_ratio`, dispatching on `p.kind == "split"`.

- [ ] **Step 4: Render the known symbol**

In `_render_corporate_add_command`, replace the unconditional `"--symbol <SYMBOL>"` with the proposal's `subject_symbol` when it is present, and update the docstring paragraph that currently states `--symbol` is always a literal placeholder to record the exception and why.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`
Expected: PASS, no skips.

- [ ] **Step 6: Run the sibling DB suites**

`_render_corporate_add_command` and the completion loop are shared machinery, so run the files that exercise them:

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py tests/test_cli.py tests/test_corporate.py -v`
Expected: PASS, no skips.

- [ ] **Step 7: Mutation gate**

1. Invert the ratio to `_reduce_ratio(held, held + received)`.
2. Change `> 0` to `<> 0` in the holding query.
3. Return `(None, ...)` unconditionally from `_complete_split_ratio`.

All three CAUGHT. Revert; confirm with `git diff`.

- [ ] **Step 8: Commit**

```bash
git add cli.py tests/db/test_cli.py
git commit -m "feat(cli): complete a share distribution's ratio from the ledger holding"
```

---

## Task 4: Net broker amendment clusters

**Files:**
- Modify: `importers/fidelity.py` — new `_amendment_plan()` and its wiring into `parse()`
- Create: `tests/fixtures/fidelity/amendment_cluster.csv`
- Modify: `cli.py` — report netting in the import summary
- Test: `tests/test_fidelity_history.py`, `tests/db/test_cli.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-3.
- Produces: `ImportBatch.warnings` entries describing each netting, and the fill-count behaviour Task 5's acceptance run depends on.

**Context the brief cannot give you.** `parse()` currently iterates `csv.DictReader` directly, and the dedicated `YOU BOUGHT`/`YOU SOLD` branch runs **before** `classify()`. The correction row is a `YOU BOUGHT`, so it reaches that branch and produces a fill today — it is not gated by the blocking policy at all. That means the netting decision must be made **before** the row loop, over all rows at once. Materialise `list(reader)` first and compute the plan from it, then loop over the materialised list. The files are a few thousand rows; materialising them is not a memory concern.

- [ ] **Step 1: Create the fixture**

Create `tests/fixtures/fidelity/amendment_cluster.csv` with the standard History header and exactly four rows — the three-row cluster plus the real closing sell that falls between them, which is what makes the ordering hazard reproducible:

```csv
Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date
01/21/2026,YOU BOUGHT OPENING TRANSACTION CORR DESCRIPTION CORRECTED CONFIRM as of 2026-01-02 CALL (ZXCO) ZXCO HOLDINGS JAN 10 26 $300 (100 SHS) (Cash),-ZXCO260110C300,CALL (ZXCO) ZXCO HOLDINGS JAN 10 26 $300 (100 SHS),Cash,2.5,1,0.65,0.03,,-250.68,1377.53,01/05/2026
01/21/2026,BUY CANCEL OPENING TRANSACTION CXL DESCRIPTION CANCELLED TRADE as of 2026-01-02 CALL (ZXCO) ZXCO HOLDINGS JAN 10 26 $300 (100 SHS) (Cash),-ZXCO260110C300,CALL (ZXCO) ZXCO HOLDINGS JAN 10 26 $300 (100 SHS),Cash,2.5,-1,0.65,-0.12,,250.77,1628.21,01/05/2026
01/06/2026,YOU SOLD CLOSING TRANSACTION CALL (ZXCO) ZXCO HOLDINGS JAN 10 26 $300 (100 SHS) (Cash),-ZXCO260110C300,CALL (ZXCO) ZXCO HOLDINGS JAN 10 26 $300 (100 SHS),Cash,4.75,-1,0.65,0.02,,474.33,1377.44,01/07/2026
01/02/2026,YOU BOUGHT OPENING TRANSACTION CALL (ZXCO) ZXCO HOLDINGS JAN 10 26 $300 (100 SHS) (Cash),-ZXCO260110C300,CALL (ZXCO) ZXCO HOLDINGS JAN 10 26 $300 (100 SHS),Cash,2.5,1,0.65,0.12,,-250.77,903.11,01/05/2026
```

Every symbol, price and figure here is fabricated. Confirm none appears in `imports/`.

- [ ] **Step 2: Write the failing tests**

```python
def test_an_amendment_cluster_nets_to_one_fill():
    """Original -> cancel -> correction is ONE buy, at the corrected fee, on
    the as-of date. Asserting the FILL COUNT is the point: before this
    existed the importer emitted a third fill for this contract, because
    classify() returns None for the CORR row but the dedicated YOU BOUGHT
    branch matched it anyway. A test that only asserted the import stops
    refusing would pass while the duplicate persisted."""
    batch = FidelityImporter().parse(_read_fixture("amendment_cluster.csv"))
    buys = [f for f in batch.fills if f.side is Side.BUY]
    sells = [f for f in batch.fills if f.side is Side.SELL]
    assert len(buys) == 1, "the cancelled original and its cancel both vanish"
    assert len(sells) == 1
    assert buys[0].executed_at.date() == date(2026, 1, 2), "dated to the as-of, not the run date"
    assert buys[0].fee == Decimal("0.68"), "0.65 commission + the CORRECTED 0.03"
    assert not batch.blocking


def test_a_netting_is_reported():
    """A netting that happens silently is indistinguishable from rows being
    dropped."""
    batch = FidelityImporter().parse(_read_fixture("amendment_cluster.csv"))
    assert any("netted" in w.lower() for w in batch.warnings)


def test_a_cancel_with_no_matching_original_is_not_netted():
    """D4: degrade to blocking, never to guessing. The matcher is fitted to a
    single real cluster, so refusing to act is the failure mode it is allowed
    to have."""
    lines = _read_fixture("amendment_cluster.csv").splitlines()
    header, rows = lines[0], lines[1:]
    # drop the original (last row) -- the cancel now matches nothing
    text = "\n".join([header] + rows[:-1]) + "\n"
    batch = FidelityImporter().parse(text)
    assert any("CANCEL" in m for _, m in batch.blocking)


def test_an_ambiguous_match_is_not_netted():
    """Two identical originals mean the cancel cannot say which it reverses.
    Ambiguous is treated as no match."""
    lines = _read_fixture("amendment_cluster.csv").splitlines()
    text = "\n".join(lines + [lines[-1]]) + "\n"   # duplicate the original
    batch = FidelityImporter().parse(text)
    assert any("CANCEL" in m for _, m in batch.blocking)
```

If `_read_fixture` does not exist in the file, add it as a one-line helper next to the existing fixture loader rather than duplicating path logic.

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fidelity_history.py -v`
Expected: FAIL. Note in your report how many fills `test_an_amendment_cluster_nets_to_one_fill` actually sees before the fix — it should be 2 buys, which is the defect this task closes.

- [ ] **Step 4: Implement the plan pass**

In `importers/fidelity.py`, above `FidelityImporter`:

```python
# All 45 `as of` tokens across the real exports are ISO YYYY-MM-DD; none is
# the MM/DD/YYYY the Run Date column uses. Strict on purpose -- an unparsable
# as-of date means the row is not netted and keeps blocking (D4), which is
# safer than guessing a format the venue has never emitted.
_AS_OF_RE = re.compile(r"\bAS OF\s+(\d{4}-\d{2}-\d{2})\b")

# Anchored on the two-word phrases, NOT on bare `CXL`/`CORR` tokens. Action
# text carries the security name with its ticker in parentheses, so a bare
# three- or four-letter token can collide with a real ticker and net an
# ordinary trade out of existence. The phrases cannot.
_CANCEL_PHRASE = "CANCELLED TRADE"
_CORRECTION_PHRASE = "CORRECTED CONFIRM"


@dataclass(frozen=True, slots=True)
class _AmendmentPlan:
    """Which rows the amendment pass removes, and how it re-dates the ones it
    keeps. Keyed by the row's line number so the main loop can consult it
    without re-deriving anything."""
    suppressed: frozenset[int]
    redated: dict[int, datetime]
    notes: tuple[str, ...]


def _amendment_plan(rows: list[tuple[int, dict[str, str]]]) -> _AmendmentPlan:
    """Net original -> cancel -> correction clusters down to the correction,
    dated to its as-of date (spec D3).

    A complete chain is: an original whose (symbol, date, |quantity|, price)
    matches a cancel's as-of tuple; and a correction sharing that cancel's
    (symbol, as-of date). Every match must be UNIQUE -- an ambiguous one is
    treated as no match at all.

    Anything incomplete or ambiguous is left entirely alone, so its rows
    reach the ordinary paths and, being unmapped and money-carrying, block
    (D4). This matcher is fitted to a single real cluster; refusing to act is
    the failure mode it is allowed to have.
    """
```

Body:

```python
    cancels: dict[tuple, list[int]] = {}
    corrections: dict[tuple, list[int]] = {}
    originals: dict[tuple, list[int]] = {}
    meta: dict[int, tuple] = {}

    for line_no, row in rows:
        action = (row.get("action") or "").strip().upper()
        symbol = (row.get("symbol") or "").strip()
        try:
            qty = _decimal(row.get("quantity"))
            price = _decimal(row.get("price"))
        except InvalidOperation:
            continue                      # not nettable; the ordinary paths handle it
        if not (qty.is_finite() and price.is_finite()):
            continue

        as_of = _AS_OF_RE.search(action)
        if as_of is None:
            # A candidate ORIGINAL is dated by its own Run Date, and carries
            # no as-of marker at all -- that is what distinguishes it from
            # the two amendment legs.
            try:
                when = datetime.strptime(
                    (row.get("run date") or "").strip(), "%m/%d/%Y"
                ).replace(tzinfo=UTC)
            except ValueError:
                continue
            if action.startswith("YOU BOUGHT") or action.startswith("YOU SOLD"):
                key = (symbol, when.date(), abs(qty), price)
                originals.setdefault(key, []).append(line_no)
                meta[line_no] = key
            continue

        as_of_date = date.fromisoformat(as_of.group(1))
        key = (symbol, as_of_date, abs(qty), price)
        meta[line_no] = key
        if _CANCEL_PHRASE in action:
            cancels.setdefault(key, []).append(line_no)
        elif _CORRECTION_PHRASE in action:
            # The correction's own quantity and price are the CORRECTED ones,
            # so it is matched on (symbol, as-of) only -- keying it on the
            # full tuple would fail exactly when the correction changed one
            # of those values, which is the case corrections exist for.
            corrections.setdefault((symbol, as_of_date), []).append(line_no)

    suppressed: set[int] = set()
    redated: dict[int, datetime] = {}
    notes: list[str] = []

    for key, cancel_lines in cancels.items():
        symbol, as_of_date, _qty, _price = key
        original_lines = originals.get(key, [])
        correction_lines = corrections.get((symbol, as_of_date), [])
        # Every leg must be UNIQUE. An ambiguous match is treated as no match
        # at all (D4) -- the rows fall through and block.
        if len(cancel_lines) != 1 or len(original_lines) != 1 or len(correction_lines) != 1:
            continue
        cancel_line, original_line = cancel_lines[0], original_lines[0]
        correction_line = correction_lines[0]
        suppressed.update({cancel_line, original_line})
        redated[correction_line] = datetime.combine(as_of_date, time.min, tzinfo=UTC)
        notes.append(
            f"netted an amendment cluster on {symbol}: lines {original_line} "
            f"(original) and {cancel_line} (cancel) suppressed; line "
            f"{correction_line} (correction) dated to {as_of_date.isoformat()}"
        )

    return _AmendmentPlan(frozenset(suppressed), redated, tuple(notes))
```

Note the asymmetry, and keep it: the **cancel** is matched to its original on
the full `(symbol, date, |quantity|, price)` tuple because it reverses that row
exactly, while the **correction** is matched on `(symbol, as-of date)` alone
because its quantity and price are the corrected ones. Keying the correction on
the full tuple would break precisely when a correction changed a value, which
is what corrections are for.

This body uses `time` and `date` from `datetime` and `InvalidOperation` from
`decimal`; check the module's existing imports and extend them rather than
adding a second import line.

Wire it in: materialise `rows = list(enumerate(reader, start=preamble_offset + 2))`, compute `plan = _amendment_plan(rows)`, extend `warnings` with `plan.notes`, and in the loop `continue` on `line_no in plan.suppressed` and use `plan.redated.get(line_no, when)` as the fill's timestamp.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fidelity_history.py tests/test_fidelity.py tests/test_fidelity_real_shape.py -v`
Expected: PASS.

- [ ] **Step 6: Report the netting in the import summary**

In `cli.py`'s import output, print the netting notes so a human sees which rows were suppressed and which date the survivor took. Reuse whatever channel warnings already print through rather than adding a new section — §6 requires that it is said, not that it gets its own heading.

Add one DB test in `tests/db/test_cli.py` asserting the netting note reaches stdout on a `--commit` import of the fixture, and that the account ends with exactly one open position in the contract rather than two.

- [ ] **Step 7: Run the DB suites**

The netting changes which fills reach `regroup_account`, so run its neighbours:

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py tests/db/test_positions.py tests/db/test_trades.py -v`
Expected: PASS, no skips. Each file takes 3-4 minutes; run in the foreground.

- [ ] **Step 8: Mutation gate**

1. Use the correction row's `Run Date` instead of its as-of date.
2. Suppress only the cancel, leaving the original in place.
3. Relax the uniqueness requirement so the first candidate wins on an ambiguous match.
4. Match on a bare `"CORR"` substring instead of `_CORRECTION_PHRASE`.

All four CAUGHT. Mutation 4 needs a test that a row whose action text merely contains those letters is untouched — add it if none of your tests fail under that mutation, and say so in your report rather than declaring it CAUGHT on the strength of an unrelated failure.

- [ ] **Step 9: Commit**

```bash
git add importers/fidelity.py cli.py tests/fixtures/fidelity/amendment_cluster.csv tests/test_fidelity_history.py tests/db/test_cli.py
git commit -m "feat(importers): net broker amendment clusters to a single dated fill"
```

---

## Task 5: Gaps, docs, and the real-data acceptance run

**Files:**
- Modify: `docs/known-gaps.md`
- Modify: `README.md` if and only if it makes a claim this branch falsifies
- Create: the branch report (no tracked path — report it in your task report)

**Interfaces:**
- Consumes: everything Tasks 1-4 produced.
- Produces: the acceptance evidence the branch is judged on.

- [ ] **Step 1: Run the acceptance check against the real exports**

This is the criterion that decides the branch. It is not a unit test and does not live in the suite — `imports/` is gitignored.

```bash
uv run python -c "
import glob
from importers.fidelity import FidelityImporter
imp = FidelityImporter()
for path in sorted(glob.glob('imports/*.csv')):
    b = imp.parse(open(path, encoding='utf-8-sig').read())
    print(len(b.blocking), path.split('/')[-1])
"
```

Expected: every file reports `0` except the single 2024 file that carries the ACAT rows, which reports blockers naming `TRANSFER OF ASSETS ACAT` **only**. (The filename embeds a real account number, so it is not written here — the command prints it.) If any other verb still blocks, that is a finding — report it; do not adjust the expectation to match the output.

Record the per-file counts in your task report. **Do not paste real symbols, quantities or amounts into any tracked file or commit message** — counts and verb names only.

- [ ] **Step 2: Verify the gap numbering before writing**

```bash
grep -oE '^\| [0-9]+ \|' docs/known-gaps.md | tr -d '| ' | sort -n | tail -3
```

The highest existing gap is expected to be **47**, making this branch's gaps 48-51. Verify rather than trust — the register has been renumbered twice. Use whatever the command actually prints.

- [ ] **Step 3: Add the four gaps**

Add a new section, `## Found while unblocking the importer's non-corporate verbs (2026-08-18)`, with one table row per gap from spec §9: the amendment matcher fitted to one cluster; a cash-only `DISTRIBUTION` blocking; the split reading being a choice rather than a derivation; and netting making the ledger disagree with the broker's row count. Each row states why it matters and cites the symbol (function or constant name), **not** a line range — a range citation cannot be found by searching for its endpoint, and 40 of 112 citations were stale on one prior branch.

Also update gap #31: it is no longer "corporate actions remain unhandled". Narrow it to the `TRANSFER OF ASSETS ACAT` remainder that branch B will close, and fix its stale citation — it cites `importers/fidelity.py:298-321` for `reject()`, which now lives near line 746. Verify the new location yourself rather than copying that number.

- [ ] **Step 4: Hygiene sweep before committing docs**

Derive the search terms from the real data rather than writing them down —
a hardcoded list of real tickers plants in a tracked file exactly the strings
it is meant to hunt:

```bash
uv run python - <<'EOF'
import csv, glob, pathlib, re, subprocess
symbols = set()
for path in glob.glob("imports/*.csv"):
    lines = pathlib.Path(path).read_text(encoding="utf-8-sig").splitlines()
    h = next((i for i, l in enumerate(lines) if l.startswith("Run Date")), None)
    if h is None:
        continue
    for row in csv.DictReader(lines[h:]):
        sym = (row.get("Symbol") or "").strip().lstrip("-")
        if len(sym) >= 3 and sym.isalnum():
            symbols.add(sym)
    symbols.update(re.findall(r"\b\d{9}\b|\bX\d{8}\b", pathlib.Path(path).name))
hits = []
for term in sorted(symbols):
    r = subprocess.run(["git", "grep", "-lnw", term], capture_output=True, text=True)
    if r.stdout.strip():
        hits.append((term, r.stdout.strip().splitlines()))
print("LEAK:", hits) if hits else print("clean")
EOF
```

Expected: `clean`. `git grep` searches tracked files only, which is the right
scope — `imports/` is gitignored and must not be flagged against itself.

Then cross-check every numeric token you added to docs or fixtures against
`imports/`: the deny-list and the pre-commit hook guard identifiers, not
values, and real quantities and dates have reached drafts three times.

- [ ] **Step 5: Full suite**

Run: `set -a && . ./.env && set +a && uv run pytest -v`
Expected: PASS with no skips. Read the summary line and quote it in your report. The suite takes about ten minutes; run it in the foreground.

- [ ] **Step 6: Commit**

```bash
git add docs/known-gaps.md README.md
git commit -m "docs: record the four gaps this branch's own decisions create"
```
