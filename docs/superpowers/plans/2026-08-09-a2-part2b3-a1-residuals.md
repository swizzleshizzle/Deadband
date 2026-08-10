# A-2 part 2b-3: the A-1 residual gaps — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five A-1 residual gaps carried since A-1 (#1, #3, #4, #6, #8), and correct three more gap-list entries that are already closed but still listed as open.

**Architecture:** No new modules. Four of the six tasks are a test or a single validation line; one is a three-word SQL change with a real money consequence; one is a Hypothesis property test. Each task is independent — they can be reviewed and rejected separately.

**Tech Stack:** Python 3.11+, `uv`, asyncpg, pytest, hypothesis (already a dev dependency).

---

## Scope corrections found while planning — READ FIRST

**Three more gap-list entries are stale.** Verified against the code on 2026-08-09:

| Gap | Claim | Reality |
|---|---|---|
| #2 | "No `CHECK (contract_multiplier > 0)` or `CHECK (mark.price >= 0)`" | Both exist: `instrument_multiplier_chk` (`db/schema.sql:53`) and `mark_price_chk` (`db/schema.sql:192`). |
| #5 | "`fill.updated_at` is never written (no triggers)" | The trigger exists: `set_updated_at()` (`db/schema.sql:224`) and `fill_set_updated_at` (`db/schema.sql:231-234`). The *separate*, still-open note elsewhere in the file is that the trigger has no **behavioural** test — a different and narrower claim. |
| #7 | "Preview cannot report duplicates" | `--check-duplicates` shipped in PR #3 (`cli.py:192`). |

This is the second consecutive plan to find stale entries (2b-2 found #10 and #11 the same way). Task 6 strikes these three. **Do not re-implement any of them** — if you find yourself adding a CHECK constraint or a trigger, stop and re-read this section.

**One gap's text is imprecise, and the imprecision would waste your time.** Gap #1 says a wrong `contract_multiplier`, **`strike` or `expiry`** is never corrected. For options, `strike` and `expiry` are *inside* `instrument_natural_key` (`ledger/types.py:135-143`), so a different value yields a different key, a different row, and no conflict to repaint — they cannot drift by construction. For non-options they are NULL. The fields that genuinely can go stale are the ones **outside** the key: `contract_multiplier`, `root`, `chain`, `contract_address`. Only the first of those can cost money.

**Deliberately NOT in this plan:** gap #9 (the `MarkSource` protocol) and gap #13 (`reconcile`). #9 was not in the residual list this plan was scoped from, and with `db/marks.py` now existing, whether a one-implementation Protocol earns its keep in a repo with no type checker is a live question, not a foregone one. #13 needs a design decision before any code. Both stay open and are named in Task 6.

---

## Global Constraints

- **Purity.** `ledger/` and `importers/` are pure: no I/O, no clock, no randomness. `tests/test_purity.py` enforces it, including the first-party `db` and `venues` packages.
- **`Decimal`, never `float`.** Pin precision with `localcontext()` around any division in a pure function; `ledger/pnl.py` uses `ctx.prec = 50`.
- **The test database is SHARED and PERSISTENT.** `instrument` rows are global and outlive their account by design. Never assert on an unqualified `SELECT count(*)`; scope every assertion to rows the test created, and probe only through the transaction-rolled-back `conn` fixture, never a bare connection.
- **Tests must be able to fail.** This project has repeatedly shipped assertions whose slack exceeded the defect they watched for. For each assertion ask what mutation turns it red.
- **Every new test is gated against a mutant before acceptance.** Report each CAUGHT or SURVIVED honestly; a survivor is information, not something to hide.
- **Run the full suite yourself** with `set -a && . ./.env && set +a && uv run pytest`, and confirm the summary says neither "skipped" nor a stale count. It takes ~330s. Never run a mutation harness while it is running — that rewrites tracked source underneath it and voids the result.
- **Scope test selectors carefully.** A `-k` filter that silently under-selects is indistinguishable from a passing run; this has bitten this project twice. Prefer naming test files over `-k` substrings.

---

## File Structure

| File | Responsibility |
|---|---|
| `ledger/corporate.py` | modify: reject a self-referential corporate action |
| `db/instruments.py` | modify: repaint the non-key fields on conflict |
| `tests/test_corporate.py` | add: the spinoff **child** dedupe-key test |
| `tests/test_importer_base.py` | add: the `content_hash` **side**-escaping test |
| `tests/test_grouping_properties.py` | add: spec §9's sum-of-per-trade-P&L property |
| `tests/db/test_instruments.py` | add: the repaint tests |
| `docs/known-gaps.md` | modify: strike #2/#5/#7, close #1/#3/#4/#6/#8 |

---

## Task 1: Reject a self-referential corporate action (gap #4)

**Files:**
- Modify: `ledger/corporate.py` — `CorporateAction.__post_init__`
- Test: `tests/test_corporate.py`

**Interfaces:**
- Consumes: `CorporateAction` (existing; `instrument_id: UUID`, `resulting_instrument_id: UUID | None`)
- Produces: nothing new — a validation line

A merger, spinoff or symbol change whose `resulting_instrument_id` equals its own `instrument_id` is nonsense: the action produces the thing it consumes. `adjust_fills` terminates safely on one today, which is why this has stayed open — but it terminates by coincidence of the current implementation, not by design, and a self-referential spinoff would allocate basis from an instrument to itself.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_corporate_action_may_not_produce_its_own_instrument():
    """resulting == instrument is nonsense: the action produces the thing it
    consumes. It terminates safely today, but by coincidence of adjust_fills'
    current shape rather than by design -- a self-referential spinoff would
    allocate basis from an instrument to itself."""
    for action_type, extra in (
        (ActionType.MERGER, {}),
        (ActionType.SPINOFF, {"basis_allocation": Decimal("0.2")}),
        (ActionType.SYMBOL_CHANGE, {}),
    ):
        with pytest.raises(ValueError, match="itself|self"):
            CorporateAction(
                instrument_id=OLD,
                action_type=action_type,
                ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
                ratio_numerator=Decimal("1"),
                ratio_denominator=Decimal("1"),
                resulting_instrument_id=OLD,
                **extra,
            )


def test_a_corporate_action_producing_a_different_instrument_is_accepted():
    """The negative control: the guard must not reject legitimate actions."""
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("1"),
        resulting_instrument_id=NEW,
    )
    assert action.resulting_instrument_id == NEW
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_corporate.py -k self_referential_or_own -v`
(or name the two tests explicitly — do not rely on a `-k` substring you have not verified matches.)
Expected: FAIL — no `ValueError` raised.

- [ ] **Step 3: Implement**

In `CorporateAction.__post_init__`, immediately after the existing "requires resulting_instrument_id" check:

```python
        if self.resulting_instrument_id == self.instrument_id:
            raise ValueError(
                f"{self.action_type} cannot produce itself "
                "(resulting_instrument_id == instrument_id)"
            )
```

Placed after the None check so a missing id still reports the more specific error. Note this fires for **any** action type that carries a resulting id, not only the three that require one — a self-referential id is meaningless on any of them.

- [ ] **Step 4: Run and confirm pass**

Run: `uv run pytest tests/test_corporate.py -v`
Expected: all pass, including the pre-existing corporate-action tests.

- [ ] **Step 5: Mutation gate**

Change `==` to `is` → still passes (UUIDs compare by value; `is` would be a latent bug). **Report this one honestly** — if it survives, that means the test does not distinguish identity from equality, which is worth knowing. Then change the condition to `if False:` → the first test must FAIL.

- [ ] **Step 6: Commit**

```bash
git add ledger/corporate.py tests/test_corporate.py
git commit -m "fix(ledger): reject a corporate action that produces its own instrument"
```

---

## Task 2: Test the spinoff CHILD's dedupe-key clearing (gap #3)

**Files:**
- Test: `tests/test_corporate.py`

`adjust_fills` already clears `venue_fill_id` and `content_hash` on synthesised fills — the code is there. What is missing is a test for the **child** leg. `test_spinoff_parent_clears_dedupe_keys` (`tests/test_corporate.py:641`) covers the parent only.

This must be gated before `adjust_fills` output is ever persisted: a child fill carrying its parent's `venue_fill_id` would violate `fill_venue_id_uniq` on insert, and one carrying the parent's `content_hash` would dedupe against the parent and vanish silently — the worse of the two outcomes, because it looks like successful deduplication.

- [ ] **Step 1: Write the failing test**

```python
def test_spinoff_child_clears_dedupe_keys():
    """The twin of test_spinoff_parent_clears_dedupe_keys, and the more
    dangerous half. A child carrying the parent's venue_fill_id violates
    fill_venue_id_uniq on insert -- loud. A child carrying the parent's
    content_hash dedupes AGAINST the parent and vanishes, reported as a
    successful skip -- silent, and the position simply never appears."""
    before = fill("10", "100", 1, venue_fill_id="V-123", content_hash="deadbeef")
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0.20"),
    )
    child = [f for f in adjust_fills([before], [action]) if f.instrument_id == NEW][0]
    assert child.venue_fill_id is None
    assert child.content_hash is None
    # And it is a real fill, not an empty shell -- otherwise the assertions
    # above would pass on something that carries no position either.
    assert child.quantity > 0
```

- [ ] **Step 2: Run and watch it fail**

It will **pass immediately** — the production code already clears these. That is expected and is not a reason to skip the step: run it, see green, then prove the test is worth having via the mutation in Step 4. A test added to cover existing behaviour is only worth its line count if it can fail.

Run: `uv run pytest tests/test_corporate.py::test_spinoff_child_clears_dedupe_keys -v`

- [ ] **Step 3: Mutation gate — this IS the deliverable**

In `ledger/corporate.py`'s spinoff branch, change the child's `venue_fill_id=None` to `venue_fill_id=f.venue_fill_id`, run the test, confirm **red**. Restore. Repeat for `content_hash`. If either mutation leaves the test green, the test is not reaching the child — say so rather than adjusting until it looks right.

- [ ] **Step 4: Commit**

```bash
git add tests/test_corporate.py
git commit -m "test(ledger): gate the spinoff child's dedupe-key clearing"
```

---

## Task 3: Isolate `content_hash`'s `side` escaping (gap #6)

**Files:**
- Test: `tests/test_importer_base.py`

`content_hash` joins six fields with `|` and escapes `%` then `|` in `symbol` and `side` (`importers/base.py`). Existing tests cover escaping in **symbol** (`test_hash_escapes_delimiter_in_symbol`, `test_hash_escapes_percent_in_symbol`) but nothing covers **side**.

The gap note records that this is non-exploitable in the current six-field layout — but *the proof depends on field order*. `side` is followed by `quantity`, `price` and `occurrence`, all numeric, so a `|` injected into `side` cannot currently collide with an adjacent field's content. Add a field after `side` whose values are free text and the hole reopens, silently, with no test to notice.

- [ ] **Step 1: Write the test**

```python
def test_hash_escapes_delimiter_in_side():
    """The twin of test_hash_escapes_delimiter_in_symbol. `side` is escaped
    in the implementation, and the gap note records the escaping as currently
    non-exploitable -- but that proof rests on `side` being followed only by
    numeric fields. Adding a free-text field after it would reopen the hole
    with nothing failing. This test makes the escaping itself load-bearing,
    independent of the field order that currently protects it."""
    common = dict(
        account_id=ACCOUNT,
        executed_at=datetime(2026, 6, 1, tzinfo=UTC),
        symbol="ZXCO",
        quantity=Decimal("1"),
        price=Decimal("1"),
    )
    # Two DIFFERENT sides that would collide if `|` were not escaped.
    a = content_hash(side="buy|x", **common)
    b = content_hash(side="buy", **common)
    assert a != b
```

Read `importers/base.py`'s `content_hash` signature before writing this and match its actual parameter names and order — do not assume they are as written above.

- [ ] **Step 2: Run it**

It will pass — the escaping exists. Same reasoning as Task 2: the mutation is the deliverable.

- [ ] **Step 3: Mutation gate**

In `importers/base.py`, change the `side` component from `_escape(side.lower())` to `side.lower()`, run the test, confirm **red**. Restore. If it stays green, the two inputs are not colliding the way the test assumes — find a pair that does, or report that you could not.

- [ ] **Step 4: Commit**

```bash
git add tests/test_importer_base.py
git commit -m "test(import): isolate content_hash's side escaping from field order"
```

---

## Task 4: Repaint the instrument's non-key fields (gap #1)

**Files:**
- Modify: `db/instruments.py` — `upsert_instrument`
- Test: `tests/db/test_instruments.py`

**This is the only task in the plan that can change a money figure.** Read the whole task before starting.

`upsert_instrument` does `ON CONFLICT (natural_key) DO UPDATE SET symbol = EXCLUDED.symbol`. Everything else is frozen at whatever the first insert wrote.

**Which fields can actually drift.** For options, `underlying`, `expiry`, `strike`, `option_right`, `quote_currency` and `asset_class` are all *inside* `instrument_natural_key`, so a different value produces a different key and a different row — there is no conflict to repaint and they cannot go stale. The fields outside the key are `symbol` (already repainted), `root`, `chain`, `contract_address`, and **`contract_multiplier`**. Only the last can cost money: a wrong multiplier is a silent, permanent 100× error on every option P&L for that instrument.

**The consequence you must not paper over.** Repainting `contract_multiplier` *restates history*: every existing fill on that instrument is suddenly valued differently, with no record that the multiplier changed. That is still better than the status quo (permanently wrong, uncorrectable), but it is a real effect and Task 6 records it as a new gap. Do not add a migration, an audit column, or a warning — that is a larger design question. Repaint, test, and record.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_stale_contract_multiplier_is_corrected_on_reimport(conn):
    """The money case. A wrong multiplier stored on first insert is otherwise
    permanent, and silently scales every option P&L on that instrument."""
    wrong = option_instrument(contract_multiplier=Decimal("1"))
    iid = await upsert_instrument(conn, wrong)
    right = option_instrument(contract_multiplier=Decimal("100"))
    assert await upsert_instrument(conn, right) == iid, "must be the same row"
    row = await conn.fetchrow("SELECT contract_multiplier FROM instrument WHERE id = $1", iid)
    assert row["contract_multiplier"] == Decimal("100")


async def test_repainting_does_not_mint_a_second_row(conn):
    """Scoped to this instrument's own natural_key -- the instrument table is
    global and shared, so an unqualified count would be meaningless here."""
    inst = option_instrument(contract_multiplier=Decimal("1"))
    key = instrument_natural_key(inst)
    await upsert_instrument(conn, inst)
    await upsert_instrument(conn, option_instrument(contract_multiplier=Decimal("100")))
    n = await conn.fetchval("SELECT count(*) FROM instrument WHERE natural_key = $1", key)
    assert n == 1


async def test_key_fields_cannot_drift_because_they_make_a_different_row(conn):
    """Not a repaint case at all, and worth pinning so nobody tries to 'fix'
    it: a different strike is a different natural key, hence a different
    instrument -- not a stale field on the same one."""
    a = await upsert_instrument(conn, option_instrument(strike=Decimal("100")))
    b = await upsert_instrument(conn, option_instrument(strike=Decimal("110")))
    assert a != b
```

Write an `option_instrument(**overrides)` helper in the test file that builds a valid `Instrument` with `AssetClass.OPTION` and all key fields populated, so each test varies exactly one thing. Read `ledger/types.py`'s `Instrument` for the required fields.

- [ ] **Step 2: Run and watch the first one fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_instruments.py -v`
Expected: `test_a_stale_contract_multiplier_is_corrected_on_reimport` FAILS (multiplier stays 1). **If you see "skipped", `TEST_PG_DSN` is unset and you are testing nothing** — fix that before continuing.

- [ ] **Step 3: Implement**

```sql
        ON CONFLICT (natural_key) DO UPDATE SET
            symbol              = EXCLUDED.symbol,
            contract_multiplier = EXCLUDED.contract_multiplier,
            root                = EXCLUDED.root,
            chain               = EXCLUDED.chain,
            contract_address    = EXCLUDED.contract_address
```

Comment it in the module, covering: which fields are in the natural key and therefore cannot drift; that `contract_multiplier` is the one whose staleness costs money; and that repainting it restates the P&L of every existing fill on that instrument, which is deliberate and recorded as a gap.

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_instruments.py tests/db/test_importing.py -v`
`test_importing.py` is included because it exercises `upsert_instrument` through the import path — confirm nothing there regressed.

- [ ] **Step 5: Mutation gate**

- Revert the `contract_multiplier` line → the first test must FAIL.
- Change `ON CONFLICT ... DO UPDATE` to `DO NOTHING` → the first test must FAIL (and check what `RETURNING id` yields under `DO NOTHING`: it returns **no row**, so `fetchval` gives `None` — if that surfaces as a confusing failure rather than a clean one, say so in your report).

- [ ] **Step 6: Commit**

```bash
git add db/instruments.py tests/db/test_instruments.py
git commit -m "fix(db): repaint an instrument's non-key fields on upsert"
```

---

## Task 5: Spec §9's sum-of-per-trade-P&L property (gap #8)

**Files:**
- Test: `tests/test_grouping_properties.py`

**Interfaces:**
- Consumes: `ledger.grouping.group_fills(fills: list[Fill]) -> list[TradeGroup]`, `ledger.pnl`'s per-trade P&L (`TradePnL.realized_pnl`, net of fees; `gross_realized_pnl`, before fees)

Spec §9: *the sum of per-trade realized P&L equals the total computed from fills.* This is the only property tying grouping to valuation. Every existing property test checks conservation *within* one trade; none checks that the grouper's partition of fills into trades conserves **value** across the whole set. An allocation that conserves quantity but misattributes value between two trades passes everything today.

Read `tests/test_grouping_properties.py` first and reuse its existing Hypothesis strategies for building fills — do not write a third fill-generator.

- [ ] **Step 1: Write the failing property test**

```python
@given(fills=fills_strategy())
def test_sum_of_per_trade_realized_pnl_equals_the_total_from_fills(fills):
    """Spec §9. The only property tying GROUPING to VALUATION: every other
    property in this file checks conservation within a single trade, so an
    allocation that conserves quantity while misattributing value between two
    trades is invisible to all of them.

    Compared gross, not net: fee allocation across trades is its own
    convention (see the fee-allocation restatement in docs/known-gaps.md) and
    folding it in here would make a failure ambiguous between two causes."""
    groups = group_fills(fills)
    per_trade = sum((pnl_for(g).gross_realized_pnl for g in groups), Decimal(0))
    total = gross_realized_from_fills(fills)
    assert per_trade == total
```

You will need `gross_realized_from_fills` — an independent computation of the same quantity that does **not** go through the grouper, or the property is circular and cannot fail. Derive it directly from the fills: for each `(account, instrument)`, walk the fills in time order maintaining an average cost, and accumulate `(exit_price − avg_cost) × closed_quantity` (sign-flipped for shorts). Put it in the test file, not in `ledger/` — it exists to disagree with the production path, so it must not share code with it.

**If you cannot write an independent computation you are confident in, stop and tell me.** A property test that computes the expected value using the code under test is worse than no test: it is green by construction and reads as coverage.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_grouping_properties.py -v`
It may pass immediately (the property may hold). That is fine — Step 3 is what establishes its worth.

- [ ] **Step 3: Mutation gate — the deliverable**

In `ledger/grouping.py`, perturb the allocation so it still conserves **quantity** but shifts **value** between two trades — for example, allocate a closing fill against the wrong open lot when two are open on the same instrument. The property must go **red** while the existing quantity-conservation properties stay green. That contrast is the whole point: if both go red, your mutation was too coarse and proves nothing about this property specifically.

Report the exact mutation you used. If you cannot find one that separates the two, say so — that would mean this property is not independent of the existing ones, which is itself a finding worth having.

- [ ] **Step 4: Commit**

```bash
git add tests/test_grouping_properties.py
git commit -m "test(ledger): spec §9 sum-of-per-trade P&L property"
```

---

## Task 6: Correct the gap list

**Files:**
- Modify: `docs/known-gaps.md`

- [ ] **Step 1: Strike gaps #2, #5 and #7 as already closed**, with the evidence: `instrument_multiplier_chk` at `db/schema.sql:53` and `mark_price_chk` at `:192`; `set_updated_at()` at `:224` and `fill_set_updated_at` at `:231-234`; `--check-duplicates` at `cli.py:192`, shipped in PR #3.

  For #5, be precise rather than just striking it: the trigger exists, but the *separate* note elsewhere in this file — that the trigger has no **behavioural** test — is still true and still open. Do not let striking one collapse the other.

- [ ] **Step 2: Note the pattern, once, and without ceremony.** This is the second consecutive plan to find stale entries (2b-2 found #10 and #11). Five closed items have now been carried as open across three sessions. Add a line near the top of the section saying that entries are struck with evidence when closed, and that anyone closing a gap should strike it in the same commit — the cost is a re-investigation per stale entry, paid by whoever plans next.

- [ ] **Step 3: Close #1, #3, #4, #6 and #8** with what shipped. For #1, correct the text's imprecision as well: `strike` and `expiry` are inside the natural key and cannot drift; the field that could actually go wrong was `contract_multiplier`.

- [ ] **Step 4: Add the new gap Task 4 creates.** Repainting `contract_multiplier` **restates the P&L of every existing fill on that instrument**, with no record that it changed. Better than permanently wrong, but real: an audit trail or a warning on change is a larger design question, deliberately not taken here.

- [ ] **Step 5: Record what remains open** — #9 (`MarkSource` protocol; whether a one-implementation Protocol earns its keep in a repo with no type checker is a live question, not a foregone one) and #13 (`reconcile`; needs the `account_snapshot` write path *and* a design decision about what to do with an unvaluable position).

- [ ] **Step 6: Commit**

```bash
git add docs/known-gaps.md
git commit -m "docs: close A-1 residuals, strike three stale entries, record the repaint's cost"
```

---

## Self-Review

**Spec coverage.** Gap #1 → Task 4. #3 → Task 2. #4 → Task 1. #6 → Task 3. #8 → Task 5. Stale #2/#5/#7 → Task 6. Spec §9's property → Task 5. **Deliberately uncovered:** #9 and #13, with reasons, recorded in Task 6.

**Placeholders.** None: every code step carries real code, every test step real tests. Task 5's `gross_realized_from_fills` is specified as an algorithm rather than transcribed code — deliberately, because transcribing it from the production path is exactly the circularity that would make the property vacuous, and the step says so.

**Type consistency.** `CorporateAction`'s fields in Task 1 match `ledger/corporate.py:40-49`. `upsert_instrument(conn, instrument) -> UUID` in Task 4 matches `db/instruments.py:14`. `group_fills(fills) -> list[TradeGroup]` in Task 5 matches `ledger/grouping.py:41`.

**Known soft spot.** Tasks 2, 3 and 5 add tests for behaviour that already works, so each will go green on first run. Their entire value is in the mutation gate, and a tired implementer could skip it and truthfully report "tests pass". Each task names the mutation as the deliverable for exactly that reason — hold them to it in review.
