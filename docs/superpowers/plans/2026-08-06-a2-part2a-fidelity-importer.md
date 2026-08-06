# A-2 Part 2a: Fidelity Importer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a real multi-account Fidelity export import end to end — routed to the right accounts, retirement plan cleanly skipped, zero unmapped money rows, and no silent value loss.

**Architecture:** Six tasks. Task 1 carries `funding_source` through the canonical types so later tasks have somewhere to put it. Task 2 replaces exact-match action mapping with a declarative rule table keyed on action *and* symbol — the only shape that can express the reinvestment rule. Tasks 3–4 add sweep classification and account routing. Task 5 makes silent loss impossible. Task 6 lets preview report duplicates. The Coinbase Advanced Trade API source and the residual A-1 gaps are part 2b.

**Tech Stack:** Python 3.10+, asyncpg, PostgreSQL 15+, pytest, `uv`.

## Global Constraints

- **The pure layer takes no I/O, no clock, no network.** `tests/test_purity.py` enforces this. `importers/` is pure — importers never touch the database.
- **Import is three-phase: parse → preview → commit.** Preview must never open a database connection except through the explicit opt-in probe added in Task 6.
- **`CanonicalCash.amount` is ALWAYS positive.** Direction lives in `kind` alone, via `OUTFLOW_KINDS` in `importers/base.py`. A negative amount is a bug, never an outflow.
- **Dedupe is `(account_id, venue_fill_id)` where the venue supplies an id, `content_hash` otherwise.** Fidelity supplies none, so Fidelity is content-hash only.
- **Database tests are opt-in** and require `TEST_PG_DSN`. The `requires_db` marker **SKIPS rather than fails** without it — a run reporting "passed" while showing skips has verified nothing. Load it with `set -a && . ./.env && set +a && uv run pytest`.
- **Every schema change is written twice** — `db/schema.sql` and a numbered migration — and `tests/db/test_schema_equivalence.py` enforces it. Never weaken that test to make a change pass.
- **`tests/fixtures/schema_baseline_a1.sql` is frozen.** Never edit or regenerate it.
- **This repository is PUBLIC.** No real account numbers, balances, tickers, holdings or hostnames in any file. Fixtures are synthetic. Findings from real exports are recorded as shapes, never specimens.
- **Gate every new test against a mutant before accepting it.** A test that passes with its bug reintroduced is worse than no test.

---

## Task 1: Carry `funding_source` through the canonical types

`fill.funding_source` exists in the database (part 1, migration 001) but nothing produces it. `CanonicalFill` has no such field, so an importer cannot express "these shares were bought with a dividend rather than with my own money." That distinction is what lets `contributed_capital` exclude reinvestment while `cost_basis` stays tax-correct.

**Files:**
- Modify: `importers/base.py` (`CanonicalFill`)
- Modify: `db/importing.py` (the fill insert)
- Test: `tests/test_importer_base.py`, `tests/db/test_importing.py`

**Interfaces:**
- Produces: `CanonicalFill.funding_source: str = "external"` — values `"external"` or `"reinvestment"`, matching the `fill_funding_source_chk` CHECK constraint.

- [ ] **Step 1: Write the failing test**

Add to `tests/db/test_importing.py`:

```python
@requires_db
async def test_funding_source_round_trips_through_commit(conn):
    """A reinvestment-funded fill must persist as such. Without this the column
    exists but nothing can ever set it, and contributed_capital cannot be
    distinguished from cost basis."""
    account_id = await create_account(
        conn, name="t", venue="coinbase", account_type="cash"
    )
    batch = ImportBatch(
        fills=(
            _fill(symbol="AAA", funding_source="reinvestment"),
            _fill(symbol="BBB"),  # defaults to external
        )
    )
    await commit_batch(conn, account_id, batch, venue="coinbase")

    rows = await conn.fetch(
        """SELECT i.symbol, f.funding_source
             FROM fill f JOIN instrument i ON i.id = f.instrument_id
            WHERE f.account_id = $1 ORDER BY i.symbol""",
        account_id,
    )
    assert [(r["symbol"], r["funding_source"]) for r in rows] == [
        ("AAA", "reinvestment"),
        ("BBB", "external"),
    ]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_importing.py::test_funding_source_round_trips_through_commit -v`

Expected: FAIL — `CanonicalFill` has no `funding_source` argument.

- [ ] **Step 3: Add the field and persist it**

In `importers/base.py`, add to `CanonicalFill` after `external_ref`:

```python
    # 'external' = the user's own capital. 'reinvestment' = bought with a
    # distribution the position itself produced. Both carry real cost basis;
    # the distinction exists so contributed_capital can exclude reinvestment
    # while cost_basis stays tax-correct. Constrained by fill_funding_source_chk.
    funding_source: str = "external"
```

In `db/importing.py`, add `funding_source` to the fill INSERT's column list and its matching parameter, taking the value from the canonical fill.

- [ ] **Step 4: Run it to verify it passes**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_importing.py -v`

Expected: PASS, existing tests unaffected.

- [ ] **Step 5: Confirm the CHECK constraint still bites**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_schema.py -v`

Expected: PASS. The existing `test_unknown_funding_source_is_rejected` proves an invalid value is refused at the database, so a typo in an importer surfaces as an error rather than as silent data.

- [ ] **Step 6: Commit**

```bash
git add importers/base.py db/importing.py tests/db/test_importing.py
git commit -m "feat(importers): carry funding_source on CanonicalFill

The column existed but nothing could set it. Reinvestment-funded fills carry
real cost basis; the tag is what lets contributed_capital exclude them while
cost_basis stays tax-correct."
```

---

## Task 2: The declarative action rule table

`_CASH_ACTIONS` is a dict of four exact prefixes. Real Fidelity action text is compound — action, then security name, then ticker, then settlement type, concatenated into one field. Worse, the reinvestment decision **cannot be made from the action alone**: `REINVESTMENT` becomes cash when the symbol is a money-market sweep and a fill when it is a real security. Any action→kind table is therefore structurally insufficient, not merely inelegant.

**Files:**
- Modify: `importers/fidelity.py`
- Test: `tests/test_fidelity.py`

**Interfaces:**
- Produces: `importers.fidelity.Outcome` (enum: `FILL`, `CASH`, `INTERNAL`, `UNMAPPED`), `importers.fidelity.Rule` (frozen dataclass), `importers.fidelity.RULES` (ordered tuple), and `classify(action: str, symbol: str) -> Rule | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fidelity.py`:

```python
def test_reinvestment_of_a_real_security_is_a_fill():
    """A DRIP purchase is a genuine acquisition with real basis, tagged so that
    contributed_capital can exclude it."""
    rule = classify("REINVESTMENT ACME CORP (AAA) (CASH)", "AAA")
    assert rule is not None
    assert rule.outcome is Outcome.FILL
    assert rule.funding_source == "reinvestment"
    assert rule.side is Side.BUY


def test_reinvestment_of_a_sweep_fund_is_internal_not_cash():
    """The sweep IS cash under A2-9, so the dividend leg already recorded this
    money. Recording the reinvestment leg too would count it twice."""
    rule = classify("REINVESTMENT MONEY MARKET (SPAXX) (CASH)", "SPAXX")
    assert rule is not None
    assert rule.outcome is Outcome.INTERNAL


def test_the_same_action_verb_resolves_differently_by_symbol():
    """The whole reason the table is keyed on action AND symbol. An action-only
    table cannot express this, so this test fails against any such design."""
    security = classify("REINVESTMENT ACME CORP (AAA) (CASH)", "AAA")
    sweep = classify("REINVESTMENT MONEY MARKET (SPAXX) (CASH)", "SPAXX")
    assert security.outcome is not sweep.outcome


def test_return_of_capital_is_not_aliased_to_dividend():
    """A return of capital reduces basis rather than being income. Recording it
    as a dividend overstates income and leaves basis high."""
    rule = classify("RETURN OF CAPITAL ACME PFD (AAA) (CASH)", "AAA")
    assert rule.outcome is Outcome.CASH
    assert rule.cash_kind == "return_of_capital"


def test_foreign_tax_paid_is_an_outflow():
    rule = classify("FOREIGN TAX PAID ACME ADR (AAA) (CASH)", "AAA")
    assert rule.outcome is Outcome.CASH
    assert rule.cash_kind == "tax"
    assert "tax" in OUTFLOW_KINDS


def test_an_unrecognised_action_classifies_as_none():
    assert classify("SOME BRAND NEW ACTION NOBODY MAPPED", "AAA") is None


def test_every_rule_is_reachable():
    """A rule shadowed by an earlier one is dead code that looks like coverage.
    Each rule must be the FIRST match for at least one sample, or the table has
    an ordering bug."""
    matched = {classify(action, symbol).name
               for action, symbol in RULE_COVERAGE_SAMPLES}
    assert matched == {r.name for r in RULES}
```

Define `RULE_COVERAGE_SAMPLES` in the test module as an explicit list of `(action, symbol)` pairs, one per rule, using synthetic tickers only.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_fidelity.py -k "reinvestment or resolves_differently or return_of_capital or foreign_tax or unrecognised or reachable" -v`

Expected: FAIL — `classify`, `Outcome`, `Rule` and `RULES` do not exist.

- [ ] **Step 3: Implement the rule table**

**First introduce the sweep predicate this table depends on.** `classify` resolves
`REINVESTMENT` differently for a sweep fund than for a real security, so it cannot be
written without `is_sweep`. Add the minimal version here; Task 3 adds the staleness guard
and the tests that pin the design choice:

```python
# Membership is DATA, not logic, so it can be reviewed at a glance. Identified
# explicitly rather than by `price == 1.00`: a real security can trade at
# exactly a dollar, and that heuristic would silently convert a genuine
# position into cash.
#
# Use the venue's PUBLISHED sweep vehicles -- the full documented list, not
# only the ones this user happens to hold. A sweep ticker is product
# infrastructure attached to essentially every account at the venue, so the
# complete list discloses nothing about anyone's holdings, and completeness is
# what keeps a sweep from being misclassified as a position.
SWEEP_SYMBOLS: frozenset[str] = frozenset({
    "SPAXX", "FDRXX", "FZFXX", "SPRXX", "FDLXX", "QPIHQ",
})


def is_sweep(symbol: str | None) -> bool:
    return (symbol or "").strip().upper() in SWEEP_SYMBOLS
```

Then, in `importers/fidelity.py`:

```python
class Outcome(enum.Enum):
    FILL = "fill"
    CASH = "cash"
    # Recognised and deliberately produces nothing. Exists to prevent
    # double-counting: a sweep dividend appears as BOTH a dividend row and a
    # reinvestment of that dividend back into the sweep. Since the sweep IS
    # cash (A2-9), those are two legs of one event; recording both counts the
    # money twice. The dividend leg records, this leg does not.
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    verb: str                       # matched against the action's leading text
    outcome: Outcome
    cash_kind: str | None = None
    side: Side | None = None
    funding_source: str = "external"
    sweep_only: bool | None = None  # None = symbol irrelevant to this rule


RULES: tuple[Rule, ...] = (
    # Ordered: more specific verbs first. `test_every_rule_is_reachable`
    # fails if any rule is shadowed by an earlier one.
    Rule("reinvest_sweep", "REINVESTMENT", Outcome.INTERNAL, sweep_only=True),
    Rule("reinvest_security", "REINVESTMENT", Outcome.FILL,
         side=Side.BUY, funding_source="reinvestment", sweep_only=False),
    Rule("exchange_sweep", "EXCHANGED TO", Outcome.INTERNAL, sweep_only=True),
    Rule("dividend_received", "DIVIDEND RECEIVED", Outcome.CASH, cash_kind="dividend"),
    Rule("dividends", "DIVIDENDS", Outcome.CASH, cash_kind="dividend"),
    Rule("interest", "INTEREST EARNED", Outcome.CASH, cash_kind="interest"),
    Rule("return_of_capital", "RETURN OF CAPITAL", Outcome.CASH,
         cash_kind="return_of_capital"),
    Rule("foreign_tax", "FOREIGN TAX PAID", Outcome.CASH, cash_kind="tax"),
    Rule("fee_charged", "FEE CHARGED", Outcome.CASH, cash_kind="fee"),
    Rule("recordkeeping_fee", "RECORDKEEPING FEE", Outcome.CASH, cash_kind="fee"),
    Rule("revenue_credit", "REVENUE CREDIT", Outcome.CASH, cash_kind="rebate"),
    Rule("eft_in", "ELECTRONIC FUNDS TRANSFER RECEIVED", Outcome.CASH,
         cash_kind="deposit"),
    Rule("eft_out", "ELECTRONIC FUNDS TRANSFER PAID", Outcome.CASH,
         cash_kind="withdrawal"),
    Rule("cash_contribution", "CASH CONTRIBUTION", Outcome.CASH, cash_kind="deposit"),
    Rule("employer_contribution", "CO CONTR", Outcome.CASH, cash_kind="deposit"),
    Rule("participant_contribution", "PARTIC CONTR", Outcome.CASH, cash_kind="deposit"),
    Rule("contributions", "CONTRIBUTIONS", Outcome.CASH, cash_kind="deposit"),
)


def classify(action: str, symbol: str) -> Rule | None:
    """First match wins. Keyed on action AND symbol, because the reinvestment
    rule resolves differently for a sweep fund than for a real security and
    cannot be expressed by the action alone."""
    a = (action or "").strip().upper()
    for rule in RULES:
        if not a.startswith(rule.verb):
            continue
        if rule.sweep_only is not None and rule.sweep_only != is_sweep(symbol):
            continue
        return rule
    return None
```

`YOU BOUGHT` / `YOU SOLD` keep their existing dedicated branch — direction comes from the action and the sign is corroboration, which the rule table does not model. Wire `classify()` into `parse()` in place of the `_CASH_ACTIONS` lookup, and pass `rule.funding_source` through to the `CanonicalFill`.

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run pytest tests/test_fidelity.py -v`

Expected: all pass, including the pre-existing tests.

- [ ] **Step 5: Mutant-gate the reachability test**

> **Correction, made during execution.** This step originally prescribed reordering
> `reinvest_security` before `reinvest_sweep`. That mutant does **not** work: those two
> rules carry mutually exclusive `sweep_only` values (`True` / `False`), so `classify`
> discriminates on the symbol regardless of their order and swapping them is a no-op.
> An implementer who accepted the passing result as proof would have shipped an
> unverified reachability test. The correct mutant must create a *genuine* shadow.

Use a mutant that actually shadows. Either:

- widen `reinvest_security`'s `sweep_only` from `False` to `None` (making it
  symbol-agnostic) and place it before `reinvest_sweep`, which then becomes unreachable;
  or
- add a rule whose `verb` is a strict prefix of a later rule's verb — e.g. a rule matching
  `"FEE"` placed before `fee_charged` — so the later rule can never be the first match.

Run `test_every_rule_is_reachable`. It MUST FAIL. Restore, and confirm it passes. Record
the verbatim failure.

**Try both shapes if time allows.** Prefix-shadowing between two cash rules is the form
most likely to occur when someone adds a rule later, and it is worth knowing whether the
test catches it. If it does not, say so in the report rather than treating the gate as
passed — a reachability test that cannot detect a shadowed rule is worthless.

- [ ] **Step 6: Commit**

```bash
git add importers/fidelity.py tests/test_fidelity.py
git commit -m "feat(fidelity): declarative rule table keyed on action and symbol

Real action text is compound -- action, security name, ticker and settlement
type in one field -- so exact matching cannot work. More importantly the
reinvestment rule resolves differently for a sweep fund than for a real
security, so an action-only table is structurally insufficient.

INTERNAL exists to prevent double-counting: a sweep dividend appears as both a
dividend row and a reinvestment of it, and the sweep IS cash."
```

---

## Task 3: Sweep staleness guard, and tests pinning the design

Task 2 introduced `SWEEP_SYMBOLS` and `is_sweep` because `classify` could not be written without them. This task adds the guard that makes the set's decay visible, and the tests that pin *why* the set is explicit rather than inferred — so a later reader does not "simplify" it into a price check.

**Files:**
- Modify: `importers/fidelity.py`
- Test: `tests/test_fidelity.py`

**Interfaces:**
- Consumes: `SWEEP_SYMBOLS`, `is_sweep` (from Task 2)
- Produces: no new public names — a warning emitted from `parse()`.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_sweep_symbol_is_recognised():
    assert is_sweep("SPAXX") is True
    assert is_sweep("spaxx") is True   # case-insensitive


def test_a_real_security_is_not_a_sweep():
    assert is_sweep("AAA") is False
    assert is_sweep("") is False
    assert is_sweep(None) is False


def test_price_is_not_used_to_infer_sweepness():
    """A real security can trade at exactly 1.00. Inferring from price would
    silently convert a genuine position into cash -- which is why the set is
    explicit. This test pins the DESIGN, not just the behaviour."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,YOU BOUGHT,AAA,PENNY CO,100,1.00,0.00,0.00,-100.00\n"
    result = FidelityImporter().parse(row)
    assert len(result.fills) == 1
    assert result.fills[0].instrument.symbol == "AAA"


def test_a_sweep_symbol_priced_far_from_par_warns():
    """Sweep funds hold a 1.00 NAV by construction. A deviation means either the
    set has acquired a non-sweep symbol or a sweep has broken the buck -- both
    need a human, and neither should pass unremarked."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,REINVESTMENT MM (SPAXX) (CASH),SPAXX,MM,10,1.40,0.00,0.00,-14.00\n"
    result = FidelityImporter().parse(row)
    assert any("sweep" in w.lower() and "SPAXX" in w for w in result.warnings)


def test_an_unlisted_symbol_reinvesting_at_par_warns_the_set_may_be_stale():
    """The direction that actually costs money. An unlisted sweep is treated as a
    real security, so its reinvestment becomes a fill that spends the dividend --
    net cash nets to zero and a phantom position appears, silently. The warning is
    the only thing that surfaces a missing ticker."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,REINVESTMENT MM (NEWSW) (CASH),NEWSW,MM,10,1.00,0.00,0.00,-10.00\n"
    result = FidelityImporter().parse(row)
    assert any("NEWSW" in w for w in result.warnings)


def test_a_real_security_at_a_dollar_is_still_imported_as_a_security():
    """The warning must not become classification. A genuine security trading at
    a dollar stays a security -- a spurious warning is cheap, silently converting
    a position into cash is not."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,YOU BOUGHT,AAA,PENNY CO,100,1.00,0.00,0.00,-100.00\n"
    result = FidelityImporter().parse(row)
    assert len(result.fills) == 1
    assert result.fills[0].instrument.symbol == "AAA"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_fidelity.py -k "sweep or par" -v`

Expected: the `is_sweep` recognition tests PASS already (Task 2 introduced it) — that is correct and expected, not evidence of anything. The staleness-warning test MUST FAIL: no such warning is emitted yet.

- [ ] **Step 3: Add the staleness guard — BOTH directions**

> **Correction, made during execution.** This step originally guarded only one
> direction: a symbol *in* the set pricing away from par. Review showed that is the
> harmless direction. The decay that actually costs money is the opposite — a genuine
> sweep ticker **missing from** the set. `sweep_only=False` means "not one of these
> six," not "is a real security," so an unlisted sweep is classified as a security and
> its reinvestment leg becomes a fill that spends the dividend. Net cash nets to zero,
> a phantom position appears in the trade log, and nothing warns. Guard both.

```python
# Sweep funds hold a $1.00 NAV by construction.
_SWEEP_PAR = Decimal("1.00")
_SWEEP_PAR_TOLERANCE = Decimal("0.01")
```

In `parse()`, emit a warning in **each** of these cases. Never suppress the row — warn and continue.

1. **A listed sweep priced away from par.** Symbol `is_sweep`, price finite, deviates from `_SWEEP_PAR` by more than `_SWEEP_PAR_TOLERANCE`. Means either the set has acquired a non-sweep symbol, or a genuine sweep broke the buck.
2. **An unlisted symbol that looks like a sweep.** Symbol is NOT in `SWEEP_SYMBOLS`, the row is a `REINVESTMENT`, and its price is within tolerance of `$1.00`. Means the set is probably missing a ticker. Name the symbol explicitly so it can be added.

**Why case 2 is a heuristic here but must not be one in `is_sweep`.** Using `price == 1.00` to *classify* would silently convert a real $1.00 security into cash — which is exactly why `SWEEP_SYMBOLS` is explicit. Using the same signal to *warn* is safe: the worst outcome is a spurious warning about a genuine penny security, which a human dismisses in seconds. Classification must be conservative; detection of your own blind spot should not be.

**Implementation note:** the real sweep tickers are a maintenance surface. Keep them in `SWEEP_SYMBOLS` alone, with the comment pointing at this guard, and do not scatter them elsewhere.

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run pytest tests/test_fidelity.py -v`

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add importers/fidelity.py tests/test_fidelity.py
git commit -m "feat(fidelity): classify sweep funds by explicit symbol set

A sweep balance is idle cash, not a position. Identified by an explicit set
rather than price == 1.00, because a real security can trade at exactly a
dollar and the heuristic would silently convert it to cash. Paired with a
staleness guard so the set falling out of date is visible."
```

---

## Task 4: Account routing, and the ignored-account escape hatch

One export spans several accounts. The importer currently puts every row into the single `--account` given and only warns, which merges separate accounts into one ledger — the exact thing D5 exists to prevent.

**Files:**
- Modify: `importers/fidelity.py` (populate `external_ref` from the account-number column)
- Modify: `db/importing.py` (routing), `cli.py` (preview reporting, commit refusal)
- Test: `tests/test_fidelity.py`, `tests/db/test_importing.py`, `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `CanonicalFill.external_ref`, `CanonicalCash.external_ref`, `account.external_ref`, `account.ignore_on_import`
- Produces: `db.importing.route_batch(conn, venue, batch) -> RoutingPlan` with fields `by_account: dict[UUID, ImportBatch]`, `unknown_refs: tuple[str, ...]`, `ignored_refs: tuple[str, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
def test_external_ref_is_the_account_number_not_the_nickname():
    """Real exports carry BOTH an account nickname and an account number. The
    number is the identifier; the nickname is not stable and is not unique."""
    result = FidelityImporter().parse(MULTI_ACCOUNT_FIXTURE)
    assert {f.external_ref for f in result.fills} == {"A0000001", "A0000002"}


@requires_db
async def test_routing_splits_a_batch_by_account(conn):
    a1 = await create_account(conn, name="one", venue="fidelity",
                              account_type="cash", external_ref="A0000001")
    a2 = await create_account(conn, name="two", venue="fidelity",
                              account_type="cash", external_ref="A0000002")
    plan = await route_batch(conn, "fidelity", _batch_spanning("A0000001", "A0000002"))
    assert set(plan.by_account) == {a1, a2}
    assert plan.unknown_refs == ()


@requires_db
async def test_an_unknown_account_ref_is_reported_not_merged(conn):
    await create_account(conn, name="one", venue="fidelity",
                         account_type="cash", external_ref="A0000001")
    plan = await route_batch(conn, "fidelity", _batch_spanning("A0000001", "A0000009"))
    assert plan.unknown_refs == ("A0000009",)


@requires_db
async def test_an_ignored_account_routes_successfully_and_is_skipped(conn):
    await create_account(conn, name="plan", venue="fidelity", account_type="cash",
                         external_ref="A0000003", ignore_on_import=True)
    plan = await route_batch(conn, "fidelity", _batch_spanning("A0000003"))
    assert plan.ignored_refs == ("A0000003",)
    assert plan.by_account == {}
    assert plan.unknown_refs == ()   # ignored is NOT unknown


@requires_db
async def test_a_null_external_ref_account_is_never_a_wildcard(conn):
    """UNIQUE (venue, external_ref) does not constrain NULLs, so several accounts
    may have none. Treating NULL as a match would make the first such account a
    silent catch-all for every unroutable row."""
    await create_account(conn, name="no-ref", venue="fidelity", account_type="cash")
    plan = await route_batch(conn, "fidelity", _batch_spanning("A0000009"))
    assert plan.by_account == {}
    assert plan.unknown_refs == ("A0000009",)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_importing.py -k routing -v`

Expected: FAIL — `route_batch` does not exist.

- [ ] **Step 3: Implement routing**

In `importers/fidelity.py`, read `external_ref` from the account-number column rather than the nickname column, falling back to `None` when absent.

In `db/importing.py`, add `route_batch`, which looks up `account.external_ref` within the venue, partitions the batch's fills and cash by resolved account id, collects refs with no matching account into `unknown_refs`, and collects refs whose account has `ignore_on_import` into `ignored_refs` without adding them to `by_account`. A row whose `external_ref` is `None`, and any account whose `external_ref` is `NULL`, must never match.

- [ ] **Step 4: Wire the CLI**

In `cmd_import`, preview prints every account found with its state (mapped / ignored / unknown) and row count — **including accounts whose rows are entirely unmapped**, which today's warning cannot see. `--commit` returns non-zero and writes nothing if `unknown_refs` is non-empty. Ignored accounts are reported as skipped, distinctly from failed.

Add a CLI test asserting `--commit` refuses and inserts nothing when an unknown ref is present, and one asserting an ignored account reports as skipped while its siblings import.

- [ ] **Step 5: Run the tests**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/ tests/test_fidelity.py -v`

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add importers/fidelity.py db/importing.py cli.py tests/
git commit -m "feat(import): route rows by account, refuse on unknown

One export spans several accounts; putting every row into a single --account
merges them into one ledger, which D5 exists to prevent. Routing matches the
account NUMBER against account.external_ref -- previously external_ref got the
nickname, which is neither stable nor unique.

--commit refuses entirely if any row routes to an unknown account. Accounts
marked ignore_on_import route successfully and skip, reported as skipped rather
than failed, so a deliberately-excluded account cannot make every import fail.
A NULL external_ref is unroutable, never a wildcard."
```

---

## Task 5: Make silent loss impossible

Two guards. An unmatched row **carrying money** blocks the commit; an unmatched row without financial content warns. And a fill-shaped row resolving to a zero price is reported in **both** importers — the defect that started this whole effort.

**Files:**
- Modify: `importers/base.py` (shared guard), `importers/fidelity.py`, `importers/coinbase.py`
- Test: `tests/test_importer_base.py`, `tests/test_fidelity.py`, `tests/test_coinbase.py`

**Interfaces:**
- Produces: `ImportBatch.blocking: tuple[str, ...]` — reasons the batch must not commit.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_unmapped_row_carrying_money_blocks_the_commit():
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,MYSTERIOUS NEW ACTION,AAA,DESC,,,,,123.45\n"
    result = FidelityImporter().parse(row)
    assert result.blocking, "a money-carrying unmapped row must block"
    assert any("MYSTERIOUS" in b for b in result.blocking)


def test_an_unmapped_row_with_no_financial_content_only_warns():
    """The trailing disclaimer block is permanently unmapped by design. If it
    blocked, no real export could ever be committed."""
    result = FidelityImporter().parse(FIXTURE + "This report is informational only.\n")
    assert result.blocking == ()
    assert result.unmapped_rows


def test_a_fill_shaped_row_with_a_zero_price_is_reported():
    """Downstream of _decimal, a missing column and a genuine zero are
    indistinguishable. The check must live where they still differ."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,YOU BOUGHT,AAA,DESC,10,0.00,0.00,0.00,0.00\n"
    result = FidelityImporter().parse(row)
    assert any("zero price" in w.lower() for w in result.warnings)


def test_the_zero_price_guard_covers_coinbase_too():
    """Same defect class, same guard. Coinbase was never audited for it."""
    result = CoinbaseImporter().parse(_coinbase_row_with_zero_price())
    assert any("zero price" in w.lower() for w in result.warnings)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_fidelity.py tests/test_coinbase.py -k "blocks or financial_content or zero_price" -v`

Expected: FAIL — `ImportBatch` has no `blocking` field and no zero-price warning is emitted.

- [ ] **Step 3: Implement**

Add `blocking: tuple[str, ...] = ()` to `ImportBatch`. In `importers/base.py`, add a shared helper both importers call when building a fill:

```python
def zero_price_warning(line_no: int, symbol: str, quantity: Decimal,
                       price: Decimal) -> str | None:
    """A fill-shaped row (real quantity) priced at zero is almost always a
    parse failure, not a free trade. Downstream of _decimal a missing column
    and a genuine zero are identical, so the distinction must be drawn here."""
    if quantity != 0 and price == 0:
        return f"line {line_no}: {symbol} has quantity {quantity} at zero price"
    return None
```

In each importer's parse loop, append that warning when it fires. In Fidelity's parse, when `classify()` returns `None` for a row whose date parsed and which carries a non-zero quantity or amount, append to `blocking` as well as `warnings`.

- [ ] **Step 4: Run them to verify they pass**

Run: `uv run pytest tests/ -k "blocks or financial_content or zero_price" -v`

Expected: PASS.

- [ ] **Step 5: Make `--commit` honour `blocking`**

In `cmd_import`, refuse to commit and return non-zero when `batch.blocking` is non-empty, printing each reason. Add a CLI test asserting nothing is inserted in that case.

- [ ] **Step 6: Full suite and commit**

Run: `set -a && . ./.env && set +a && uv run pytest -q`

```bash
git add importers/ cli.py tests/
git commit -m "feat(import): block on unmapped money rows, warn on zero prices

The action vocabulary is open-ended, so unknown actions are guaranteed.
Blocking everything is unworkable -- the trailing disclaimer is permanently
unmapped by design -- and blocking nothing is exactly how the silent-zero
defect looked like success. So: a row with a valid date and non-zero quantity
or amount that no rule matches refuses the commit; a row with no financial
content warns.

The zero-price guard covers both importers. Coinbase shares the defect class
and had never been audited for it."
```

---

## Task 6: Preview reports duplicates

Spec §7 requires preview to show what is already present. Preview deliberately never opens a connection, so it structurally cannot — gap #7. The fix is an explicit, opt-in read-only probe, not a change to preview's default.

**Files:**
- Modify: `db/importing.py`, `cli.py`
- Test: `tests/db/test_importing.py`, `tests/db/test_cli.py`

**Interfaces:**
- Produces: `db.importing.probe_duplicates(conn, account_id, batch) -> DuplicateReport` with `fill_dupes: int`, `cash_dupes: int`. Read-only: executes only SELECTs.

- [ ] **Step 1: Write the failing test**

```python
@requires_db
async def test_probe_reports_duplicates_without_writing(conn):
    account_id = await create_account(conn, name="t", venue="fidelity",
                                      account_type="cash")
    batch = _batch_of_two_fills()
    await commit_batch(conn, account_id, batch, venue="fidelity")

    before = await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1",
                                 account_id)
    report = await probe_duplicates(conn, account_id, batch)
    after = await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1",
                                account_id)

    assert report.fill_dupes == 2      # both already present
    assert before == after == 2        # the probe wrote nothing
```

- [ ] **Step 2: Run it to verify it fails**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_importing.py -k probe -v`

Expected: FAIL — `probe_duplicates` does not exist.

- [ ] **Step 3: Implement**

Add `probe_duplicates` to `db/importing.py`, computing each fill's `content_hash` (or using `venue_fill_id` where present) and counting existing matches with a single SELECT per table. It must issue no INSERT, UPDATE or DELETE.

- [ ] **Step 4: Wire it as opt-in**

Add `--check-duplicates` to the `import` command. Without it, preview keeps its current contract and opens no connection. With it, preview opens a read-only connection and reports the counts. Document in the flag's help text that plain preview deliberately touches no database.

- [ ] **Step 5: Run the tests**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/ -v`

Expected: PASS, including the existing test that plain preview opens no connection.

- [ ] **Step 6: Commit**

```bash
git add db/importing.py cli.py tests/
git commit -m "feat(import): optional read-only duplicate probe in preview

Spec section 7 requires preview to report duplicates; preview never opens a
connection by design, so it structurally could not. Adds an explicit opt-in
probe rather than changing preview's default -- the no-connection guarantee is
load-bearing and stays intact unless asked otherwise."
```

---

## Self-review

**Spec coverage.** Against `2026-08-05-a2-ledger-completion-design.md` §3, §7 and §8, for the Fidelity half:

| Spec item | Task |
|---|---|
| DRIP `funding_source` (A2-8) | 1 |
| Declarative rule table keyed on action + symbol (A2-13) | 2 |
| `INTERNAL` outcome, no double-counting (A2-11) | 2 |
| `return_of_capital` not aliased (A2-14) | 2 |
| Sweep by explicit symbol set + staleness guard (A2-9, A2-10) | 3 |
| Account routing, refuse on unknown (A2-5) | 4 |
| `ignore_on_import` escape hatch (A2-6) | 4 |
| `external_ref` is the account number (R3) | 4 |
| Multi-account warning sees fully-unmapped accounts (R2) | 4 |
| Unknown-money-row blocking (A2-12) | 5 |
| Shared zero-price guard, both importers | 5 |
| Preview duplicate reporting (gap #7) | 6 |

Deferred to **part 2b**: the Coinbase Advanced Trade API source and its cut-over (A2-16), the `positions` CLI command (gap #12), and the residual A-1 gaps — `upsert_instrument` repaint, self-referential corporate-action validation, the `content_hash` side-escaping test, the spinoff-child dedupe test, and §9's property test.

**Placeholder scan.** No TBDs. `SWEEP_SYMBOLS` carries the venue's published sweep vehicles rather than a placeholder. A pre-flight scan caught the original approach: the first cut of the repository deny-list denied sweep tickers alongside genuine holdings, which would have made the pre-commit hook block the importer's own source file. The deny-list now distinguishes instruments the user *chose to invest in* (denied) from instruments the venue uses as *cash infrastructure* (allowed, and nameable in tracked code).

**Type consistency.** `funding_source` is named identically in `CanonicalFill` (Task 1), the rule table (Task 2), and the existing `fill_funding_source_chk` constraint. `Outcome`/`Rule`/`RULES`/`classify` are defined in Task 2 and used in Tasks 3 and 5.

A first draft of this plan defined `is_sweep` in Task 3 while calling it from `classify` in Task 2 — a dependency inversion that would have blocked Task 2 outright. Corrected: Task 2 introduces `SWEEP_SYMBOLS` and `is_sweep` because it cannot be written without them, and Task 3 adds the staleness guard and the tests that pin the design rationale. Task 3's recognition tests therefore pass on arrival, which is expected and is called out in its Step 2 so nobody reads it as a green light they did not earn.

**One thing the implementer must not miss.** Task 2's `INTERNAL` outcome is the subtlest idea in this plan. It is not "ignore this row" — it is "this row is the offsetting leg of an event already recorded, and recording it again would double-count the money." Deleting it, or collapsing it into the unmapped path, silently doubles every sweep dividend.
