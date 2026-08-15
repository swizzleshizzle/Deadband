# Option Expiry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recognise `EXPIRED` rows in Fidelity activity exports and emit a closing fill at price zero, so an option that expires worthless actually closes its position and realises its P&L.

**Architecture:** Two new `Outcome` members on the existing rule table. `EXPIRY` routes to a dedicated `build_expiry_fill` that supplies price zero itself and never touches the zero-price guard; `UNSUPPORTED` refuses the commit for `ASSIGNED`/`EXERCISED`. No schema change, no new module — `importers/fidelity.py` only, plus tests and docs.

**Tech Stack:** Python 3.11+, `uv`, pytest.

**Spec:** `docs/superpowers/specs/2026-08-14-option-expiry-design.md`. Read §4 (the emitted fill) and §5 (failure policy) before starting.

## Global Constraints

- **Purity.** `importers/` and `ledger/` import no I/O, no clock, no randomness, and not the first-party `db`/`venues` packages. `tests/test_purity.py` enforces it.
- **`Decimal`, never `float`.**
- **The clock lives in `cli.py`.** `importers/` never calls `datetime.now()`. Every date comes from the row or the parsed symbol.
- **Refusals write nothing.** A rejected row produces a warning and an unmapped entry; a blocking one additionally refuses the commit.
- **Rule membership is DATA.** New verbs belong in `RULES`, not in branching logic. `test_every_rule_is_reachable` fails if a rule is shadowed.
- **Tests must be able to fail.** For each assertion ask what mutation turns it red.
- **Every new test is gated against a mutant.** Report each CAUGHT or SURVIVED honestly.
- **Do not run the full suite** — it takes ~6.5 minutes and the controller runs it. Use targeted selectors, and **name the test FILE, never a `-k` substring** (a silently under-selecting `-k` looks identical to a passing run; it has bitten this project twice).
- **This repo is PUBLIC.** Fixtures use fabricated symbols. Never copy a real symbol, account number or amount from `imports/`.

---

## File Structure

| File | Responsibility |
|---|---|
| `importers/fidelity.py` | modify: `Outcome.EXPIRY`, `Outcome.UNSUPPORTED`, two/three `RULES` rows, `build_expiry_fill`, dispatch branches |
| `tests/test_fidelity.py` | unit behaviour: side from sign, date from symbol, zero price without a warning, refusals, blocking |
| `tests/fixtures/fidelity/real_shape_activity.csv` | modify: add an open-then-expire option pair |
| `tests/test_fidelity_real_shape.py` | end-to-end: the premium becomes realised P&L and no position remains |
| `docs/known-gaps.md`, `README.md` | modify: record the gaps §7 names, document the behaviour |

---

## Task 1: The `EXPIRY` outcome and the closing fill

**Files:**
- Modify: `importers/fidelity.py`
- Test: `tests/test_fidelity.py`

**Interfaces:**
- Consumes: `parse_option_symbol(symbol) -> Instrument | None` (existing), `reject(row, raw_row, account, line_no, message)` (existing, defined inside `parse`), `_decimal(raw) -> Decimal` (existing).
- Produces: `Outcome.EXPIRY`; `Rule("expired_option", "EXPIRED", Outcome.EXPIRY)`; `build_expiry_fill(row, raw_row, line_no, symbol, account) -> None` defined inside `parse` alongside `build_fill`.

**Read first:** `importers/fidelity.py`'s `build_fill` (it is the sibling this deliberately does *not* reuse) and `importers/base.py`'s `zero_price_warning` docstring (it is the reason why).

- [ ] **Step 1: Write the failing tests**

Read `tests/test_fidelity.py` first and reuse whatever helper it already has for building a CSV and calling the importer. If it has none, use this shape; the header must match a real per-account export, which has **no** `Account`/`Account Number` columns and **does** have `Cash Balance ($)`:

```python
_HDR = (
    "Run Date,Action,Symbol,Description,Type,Price ($),Quantity,"
    "Commission ($),Fees ($),Accrued Interest ($),Amount ($),"
    "Cash Balance ($),Settlement Date"
)


def _csv(*rows: str) -> str:
    return "\n".join(("Brokerage", "", _HDR, *rows))


def test_expired_short_call_closes_with_a_buy_at_zero():
    """The row describes the POSITION being removed, not a trade direction.
    A negative quantity is a short, and a short is closed by buying it back.
    Reading the sign as a side would open a second short instead of closing
    the first, and the phantom would never go away."""
    batch = FidelityImporter().parse(
        _csv("11/24/2026,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,Cash,,-1,,,,0.00,,")
    )
    (fill,) = batch.fills
    assert fill.side is Side.BUY
    assert fill.quantity == Decimal(1)
    assert fill.price == Decimal(0)


def test_expired_long_put_closes_with_a_sell_at_zero():
    batch = FidelityImporter().parse(
        _csv("11/24/2026,EXPIRED PUT (ZXCO) ZXCO CORP,-ZXCO261121P10,,Cash,,2,,,,0.00,,")
    )
    (fill,) = batch.fills
    assert fill.side is Side.SELL
    assert fill.quantity == Decimal(2)


def test_expiry_is_dated_from_the_symbol_not_the_run_date():
    """Fidelity books a Friday expiry on the following Monday. A statement
    dated in between shows no such position, so dating the close to Run Date
    would leave a phantom open across the statement date and produce false
    drift in exactly the window reconcile exists to check."""
    batch = FidelityImporter().parse(
        _csv("11/24/2026,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,Cash,,-1,,,,0.00,,")
    )
    (fill,) = batch.fills
    assert fill.executed_at == datetime(2026, 11, 21, tzinfo=UTC)


def test_expiry_does_not_trip_the_zero_price_guard():
    """The guard exists because downstream of _decimal a missing column and a
    genuine zero are indistinguishable. This path never reads `price` at all,
    so the ambiguity cannot arise. Asserting the absence of the warning is
    what pins the carve-out -- asserting price == 0 alone would still pass if
    the guard fired."""
    batch = FidelityImporter().parse(
        _csv("11/24/2026,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,Cash,,-1,,,,0.00,,")
    )
    assert not any("zero price" in w.lower() for w in batch.warnings)


def test_two_same_size_lots_expiring_the_same_day_both_survive():
    """Identical rows hash identically without the occurrence counter, and the
    second would be silently deduped away -- losing a lot. The real export's
    same-day pair differs in quantity, so testing against that data alone
    would never catch this."""
    row = "11/24/2026,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,Cash,,-3,,,,0.00,,"
    batch = FidelityImporter().parse(_csv(row, row))
    assert len(batch.fills) == 2


def test_expiry_with_an_unparsable_symbol_is_refused_not_guessed():
    batch = FidelityImporter().parse(
        _csv("11/24/2026,EXPIRED SOMETHING ODD,NOTANOPTION,,Cash,,-1,,,,0.00,,")
    )
    assert batch.fills == ()
    assert any("option symbol" in w for w in batch.warnings)


def test_expiry_with_zero_quantity_is_refused_not_guessed():
    """Neither direction nor size is knowable, and guessing either is how you
    get a plausible wrong number."""
    batch = FidelityImporter().parse(
        _csv("11/24/2026,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,Cash,,0,,,,0.00,,")
    )
    assert batch.fills == ()
    assert any("quantity" in w for w in batch.warnings)
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_fidelity.py -v`
Expected: FAIL — the `EXPIRED` rows are unmapped, so `batch.fills` is empty and the side/date assertions never run.

- [ ] **Step 3: Implement**

Add the outcome member, with the comment explaining why it is not `FILL`:

```python
class Outcome(enum.Enum):
    FILL = "fill"
    CASH = "cash"
    INTERNAL = "internal"
    # An option leaving the book because it expired worthless. Produces a
    # CLOSING fill at price zero. Distinct from FILL because the zero is a
    # constant this code supplies rather than a value parsed from the row,
    # so zero_price_warning must not run on it -- see build_expiry_fill.
    EXPIRY = "expiry"
```

Add the rule. Position is unconstrained (no existing verb is a prefix of `EXPIRED` and vice versa); place it after the reinvest rules so the fill-producing rules read together:

```python
    Rule("expired_option", "EXPIRED", Outcome.EXPIRY),
```

Add `build_expiry_fill` immediately after `build_fill` inside `parse`:

```python
        def build_expiry_fill(
            row: dict[str, str],
            raw_row: dict[str, str],
            line_no: int,
            symbol: str,
            account: str | None,
        ) -> None:
            """An option that expired worthless: close the position at zero.

            Deliberately NOT routed through build_fill. build_fill reads
            `price` from the row and runs zero_price_warning on the result;
            this path never reads `price` at all. Giving build_fill a price
            override would make the guard bypassable from any future call
            site, which is the opposite of what importers.base's
            zero_price_warning docstring asks for. The near-duplication of
            the quantity checks below is the price of keeping the guard
            unreachable from here, and is deliberate.
            """
            instrument = parse_option_symbol(symbol)
            if instrument is None:
                reject(
                    row,
                    raw_row,
                    account,
                    line_no,
                    f"line {line_no}: expiry with no parsable option symbol "
                    f"({symbol!r}), skipped",
                )
                return

            try:
                raw_qty = _decimal(row.get("quantity"))
            except InvalidOperation as exc:
                reject(row, raw_row, account, line_no, f"line {line_no}: bad number ({exc})")
                return

            # NaN == 0 is False, so the finiteness test must be part of the
            # same guard rather than a later one.
            if not raw_qty.is_finite() or raw_qty == 0:
                reject(
                    row,
                    raw_row,
                    account,
                    line_no,
                    f"line {line_no}: expiry with zero or non-finite quantity, skipped",
                )
                return

            # The row describes the POSITION being removed, not a trade
            # direction -- there is no verb here to read a side from. A short
            # (negative) position is closed by buying it back, a long one by
            # selling it.
            side = Side.BUY if raw_qty < 0 else Side.SELL

            # The option's own expiry, NOT `Run Date`. Fidelity books a Friday
            # expiry on the following Monday; a statement dated in between
            # shows no such position, so Run Date would leave a phantom open
            # across the statement date and produce false drift. `expiry` sits
            # inside instrument_natural_key, so this is the same value that
            # mints the instrument and cannot disagree with it. Midnight UTC
            # matches the date-only convention the Run Date branch uses.
            when = datetime(
                instrument.expiry.year,
                instrument.expiry.month,
                instrument.expiry.day,
                tzinfo=UTC,
            )

            fills.append(
                CanonicalFill(
                    instrument=instrument,
                    executed_at=when,
                    side=side,
                    quantity=abs(raw_qty),
                    price=Decimal(0),
                    fee=Decimal(0),
                    fee_currency="USD",
                    external_ref=account,
                    funding_source="external",
                )
            )
```

Add the dispatch branch. It must go **before** the trailing `# rule.outcome is Outcome.CASH` fallthrough:

```python
            if rule.outcome is Outcome.EXPIRY:
                build_expiry_fill(row, raw_row, line_no, symbol, account)
                continue
```

- [ ] **Step 4: Run and confirm pass**

Run: `uv run pytest tests/test_fidelity.py tests/test_purity.py -v`
Expected: all pass, including the pre-existing tests.

- [ ] **Step 5: Mutation gate**

- Invert the side derivation (`Side.SELL if raw_qty < 0 else Side.BUY`) → the short-call and long-put tests must FAIL.
- Use `when` from `Run Date` instead of the symbol → `test_expiry_is_dated_from_the_symbol_not_the_run_date` must FAIL.
- Call `zero_price_warning` on this path → `test_expiry_does_not_trip_the_zero_price_guard` must FAIL.
- Drop the `raw_qty == 0` guard → `test_expiry_with_zero_quantity_is_refused_not_guessed` must FAIL.
- Return early instead of rejecting on an unparsable symbol (no warning appended) → `test_expiry_with_an_unparsable_symbol_is_refused_not_guessed` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add importers/fidelity.py tests/test_fidelity.py
git commit -m "feat(importers): close an expired option at zero"
```

---

## Task 2: `UNSUPPORTED` — refuse assignment and exercise loudly

**Files:**
- Modify: `importers/fidelity.py`
- Test: `tests/test_fidelity.py`

**Interfaces:**
- Consumes: `Outcome` (extended in Task 1), the `blocking` list inside `parse`.
- Produces: `Outcome.UNSUPPORTED`; `Rule("assigned_option", "ASSIGNED", Outcome.UNSUPPORTED)`; `Rule("exercised_option", "EXERCISED", Outcome.UNSUPPORTED)`.

**Why this exists.** Scope is deliberately expiry-only. Today an unmapped row blocks the commit *only* when it carries a non-zero quantity or amount (`reject` → `_carries_money`). The option leg of an assignment may well be `0.00`, exactly like an expiry — which would drop it silently and reproduce the hole Task 1 closes. `UNSUPPORTED` means *recognised, and deliberately refuses*.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_assigned_option_blocks_the_commit_even_with_no_money_on_the_row():
    """Scope is expiry-only, which is only safe if the unhandled case refuses
    rather than passes. An assignment's option leg can carry Amount 0.00, so
    the ordinary carries-money blocking rule would let it through silently --
    the exact shape of the bug this branch exists to fix."""
    batch = FidelityImporter().parse(
        _csv("11/24/2026,ASSIGNED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,Cash,,-1,,,,0.00,,")
    )
    assert batch.fills == ()
    assert batch.blocking != ()
    assert any("ASSIGNED" in message for _ref, message in batch.blocking)


def test_an_exercised_option_blocks_the_commit():
    batch = FidelityImporter().parse(
        _csv("11/24/2026,EXERCISED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,Cash,,-1,,,,0.00,,")
    )
    assert batch.fills == ()
    assert any("EXERCISED" in message for _ref, message in batch.blocking)


def test_an_expiry_does_not_block():
    """The counterpart assertion: the two outcomes must not be conflated."""
    batch = FidelityImporter().parse(
        _csv("11/24/2026,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,Cash,,-1,,,,0.00,,")
    )
    assert batch.blocking == ()
    assert len(batch.fills) == 1


def test_every_outcome_member_has_a_dispatch_branch():
    """The dispatch used to end in a bare `# rule.outcome is Outcome.CASH`
    fallthrough, so a new Outcome with no branch would be silently treated as
    a cash movement. This pins that it cannot happen again."""
    for rule in RULES:
        assert rule.outcome in {
            Outcome.FILL,
            Outcome.CASH,
            Outcome.INTERNAL,
            Outcome.EXPIRY,
            Outcome.UNSUPPORTED,
        }
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_fidelity.py -v`
Expected: FAIL — `Outcome.UNSUPPORTED` does not exist, and `ASSIGNED`/`EXERCISED` currently fall through as unmapped rows whose `Amount` of `0.00` means they do not block.

- [ ] **Step 3: Implement**

```python
    # Recognised and deliberately REFUSED. Scope is expiry-only by decision
    # E1 of the spec; this is what makes that a choice rather than a bet. An
    # unmapped row blocks only when it carries money, and an assignment's
    # option leg can carry Amount 0.00 -- so without this it would drop
    # silently, exactly like an expiry did before Task 1.
    UNSUPPORTED = "unsupported"
```

```python
    Rule("assigned_option", "ASSIGNED", Outcome.UNSUPPORTED),
    Rule("exercised_option", "EXERCISED", Outcome.UNSUPPORTED),
```

The dispatch branch appends to `blocking` **unconditionally** — not via `reject`, whose blocking is conditional on `_carries_money`:

```python
            if rule.outcome is Outcome.UNSUPPORTED:
                message = (
                    f"line {line_no}: {action.split()[0]} is recognised but not "
                    "supported; import refuses rather than guessing at the "
                    "resulting stock leg"
                )
                warnings.append(message)
                unmapped.append(str(raw_row))
                blocking.append((account, message))
                continue
```

Then make the CASH fallthrough explicit so a future outcome cannot land in it silently:

```python
            if rule.outcome is not Outcome.CASH:
                raise AssertionError(f"unhandled rule outcome {rule.outcome!r}")
```

placed immediately before the existing `# rule.outcome is Outcome.CASH` block, replacing that bare comment.

- [ ] **Step 4: Run and confirm pass**

Run: `uv run pytest tests/test_fidelity.py tests/test_purity.py -v`

- [ ] **Step 5: Mutation gate**

- Route `UNSUPPORTED` through `reject` instead of appending to `blocking` directly → `test_an_assigned_option_blocks_the_commit_even_with_no_money_on_the_row` must FAIL (the row's `Amount` is `0.00`, so `_carries_money` is False).
- Map `ASSIGNED` to `Outcome.EXPIRY` → both the assignment test and `test_an_expiry_does_not_block` must still distinguish them; report what actually reddens.
- Delete the `raise AssertionError` fallthrough guard and add a throwaway `Outcome` member with no branch → confirm it would be treated as CASH. Revert the throwaway member.

- [ ] **Step 6: Commit**

```bash
git add importers/fidelity.py tests/test_fidelity.py
git commit -m "feat(importers): refuse assignment and exercise rather than dropping them"
```

---

## Task 3: End-to-end — the premium becomes realised P&L

**Files:**
- Modify: `tests/fixtures/fidelity/real_shape_activity.csv`
- Test: `tests/test_fidelity_real_shape.py`

**Interfaces:**
- Consumes: everything from Tasks 1 and 2.
- Produces: no new API. This task proves the fill Task 1 emits actually closes a trade once grouped.

**Read `tests/test_fidelity_real_shape.py` first** and follow its existing structure rather than inventing a second harness.

**Hygiene — non-negotiable.** The fixture uses **fabricated** symbols. Do not copy any symbol, underlying, account number or amount from `imports/`. The deny-list guards identifiers, not values: a fixture with invented tickers but faithfully copied amounts has passed the scan clean before. Before committing, diff the fixture's numeric tokens against the real export and expect only calendar and structural fragments to collide:

Substitute the real export's filename for `<EXPORT>` when you run it — do **not** write
that filename into any tracked file. The exports in `imports/` are named after the account
they belong to, and `imports/` is gitignored and hook-blocked precisely so those names stay
out of the repository:

```bash
python3 - <<'EOF'
import re, pathlib
real = pathlib.Path('imports/<EXPORT>.csv').read_text(encoding='utf-8-sig')
fix  = pathlib.Path('tests/fixtures/fidelity/real_shape_activity.csv').read_text(encoding='utf-8-sig')
nums = lambda t: set(re.findall(r'-?\d+\.\d+|\b\d{2,}\b', t))
print(sorted(nums(real) & nums(fix)))
EOF
```

- [ ] **Step 1: Add the fixture rows**

Append an open-then-expire pair to `tests/fixtures/fidelity/real_shape_activity.csv`, matching the column count of the rows already in that file. Use a fabricated underlying (`ZXCO`) and round figures that make the arithmetic obvious:

- a `YOU SOLD OPENING TRANSACTION` of 1 contract of `-ZXCO261121C500` at `4.00`, amount `400.00`, run date `10/03/2026`
- an `EXPIRED CALL (ZXCO) …` of `-1` of `-ZXCO261121C500`, amount `0.00`, run date `11/24/2026`

- [ ] **Step 2: Write the failing test**

```python
def test_an_expired_short_call_closes_and_realises_its_premium():
    """The whole point of the feature. Before this, only the opening SELL was
    recorded: the short stayed open forever, was valued as a liability that
    did not exist, and its premium was never realised."""
    batch = FidelityImporter().parse(REAL_SHAPE_TEXT)
    opt = [f for f in batch.fills if f.instrument.symbol == "-ZXCO261121C500"]
    assert len(opt) == 2
    opening = next(f for f in opt if f.side is Side.SELL)
    closing = next(f for f in opt if f.side is Side.BUY)
    assert opening.price == Decimal("4.00")
    assert closing.price == Decimal(0)
    assert closing.executed_at == datetime(2026, 11, 21, tzinfo=UTC)
    assert closing.quantity == opening.quantity
```

Adapt `REAL_SHAPE_TEXT` to whatever the file already uses to load the fixture.

- [ ] **Step 3: Run and watch it fail**

Run: `uv run pytest tests/test_fidelity_real_shape.py -v`
Expected: FAIL before the fixture rows are added (only one fill for that symbol).

- [ ] **Step 4: Run and confirm pass**

Run: `uv run pytest tests/test_fidelity_real_shape.py tests/test_fidelity.py -v`

- [ ] **Step 5: Mutation gate**

- Remove the `EXPIRED` rule from `RULES` → this test must FAIL.
- Run the numeric-token diff above and paste its output into your report. Anything other than calendar or structural fragments is a hygiene failure, not a test failure — stop and report it.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/fidelity/real_shape_activity.csv tests/test_fidelity_real_shape.py
git commit -m "test(importers): an expired short call closes and realises its premium"
```

---

## Task 4: Record what this leaves open

**Files:**
- Modify: `docs/known-gaps.md`, `README.md`

- [ ] **Step 1: Check the current highest gap number**

`grep -oE '^\| [0-9]+ \|' docs/known-gaps.md | tail -3`. **Check, do not assume** — this file has had two renumbering incidents, and a previous session hit a conflict where two branches each added the same number. Number forward from whatever it actually ends at.

- [ ] **Step 2: Record three gaps**, matching the existing rows' format and level of reasoning:

1. **An expiry whose opening fill is absent** makes the grouper treat the closing fill as an *opening* one, creating a phantom position at zero cost basis. Deliberately not defended against: 0 of 27 expiries across three accounts and five years are orphaned, and `regroup_account` recomputes trades from every fill, so importing an account's files out of order resolves itself once they are all in. Permanent only if one year of an account is imported and the earlier ones never are.
2. **Corporate actions remain unhandled.** The two long-term accounts contain `MERGER`, `REVERSE SPLIT`, `NAME CHANGED`, `DISTRIBUTION`, `TRANSFER OF ASSETS ACAT`, `IN LIEU OF` and a `BUY CANCEL OPENING TRANSACTION`. `ledger/corporate.py` already models several but is not wired to the importer. **The asymmetry is the point:** those carrying a non-zero `Amount` block the commit, but `MERGER` and `NAME CHANGED` carry `0.00` and pass silently while changing share counts — the same silent-drop shape this plan fixes for expiry.
3. **Backdated `as of` correction rows** (`REINVESTMENT as of …`, `FEE CHARGED as of …`) appear in one account and are not modelled; their effect on dating is unexamined.

- [ ] **Step 3: README** — document that an expired option closes at zero on its expiry date, and that assignment and exercise deliberately refuse the import rather than being guessed at. Match the structure of the existing importer documentation.

- [ ] **Step 4: Verify and commit.** Open every file you cite and check the line numbers resolve — this branch's predecessor had citations rot three times. Stage, run `.githooks/pre-commit` without bypassing, and confirm `git diff --cached --stat` shows only documentation.

```bash
git add docs/known-gaps.md README.md
git commit -m "docs: record what option-expiry handling leaves open"
```

---

## Self-Review

**Spec coverage.** E1 (expiry-only scope) → Tasks 1 and 2. E2 (assignment blocks) → Task 2. E3 (closing fill at zero) → Task 1. E4 (dated from the symbol) → Task 1, Step 1's third test. E5 (side from the quantity sign) → Task 1, first two tests. E6 (zero is a supplied constant) → Task 1's `build_expiry_fill` and the guard test. §3's recognition via the rule table → Task 1, Step 3. §5's failure policy, all four rows → Task 1 (quantity, symbol) and Task 2 (assignment, exercise). §6's testing list, all seven items → Tasks 1 and 3. §7's three gaps → Task 4. §8's hygiene → Task 3, Step 1 and Step 5.

**Placeholders.** None. Every code step carries the code; every test step carries the test.

**Type consistency.** `build_expiry_fill(row, raw_row, line_no, symbol, account)` takes no `when` (it derives one) and no `side` (it derives one), which is what distinguishes it from `build_fill(row, raw_row, line_no, symbol, when, account, side, funding_source)`. `Outcome.EXPIRY` and `Outcome.UNSUPPORTED` are introduced in Tasks 1 and 2 respectively and referenced consistently thereafter. `parse_option_symbol` returns `Instrument | None` and its `.expiry` is a `date`, which is why Task 1 builds a `datetime` from its three components rather than calling `.replace(tzinfo=...)` on it.

**Known soft spot.** Task 2's third mutation asks the implementer to add and then revert a throwaway `Outcome` member. That is the only mutation in this plan that edits an enum rather than a statement, and it is the easiest to leave behind by accident. The implementer should confirm `git diff` is clean before committing.
