# Corporate Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record corporate actions and actually apply them, so a stored reverse split reaches the positions the ledger reports.

**Architecture:** `ledger/corporate.py` and the `corporate_action` table already exist and are unwired. This adds a storage layer (`db/corporate.py`), calls the existing, already-tested `adjust_fills` from `regroup_account` between the manual-holding reduction and `group_fills`, and puts a preview-by-default CLI in front. The adjustment is derived at read time and never baked into stored fills.

**Tech Stack:** Python 3.11+, `uv`, asyncpg, pytest.

**Spec:** `docs/superpowers/specs/2026-08-15-corporate-actions-design.md`. Read §4 (applying the adjustment) and §5 (the preview) before starting — they carry reasoning this plan compresses.

## Global Constraints

- **Purity.** `ledger/` and `importers/` import no I/O, no clock, no randomness, and not the first-party `db`/`venues` packages. `tests/test_purity.py` enforces it. **This plan changes nothing in `ledger/`** — `adjust_fills` is already complete and heavily tested. If you find yourself editing it, stop and report.
- **`Decimal`, never `float`.**
- **The clock lives in `cli.py`.** `db/` never calls `datetime.now()`.
- **Refusals write nothing and exit non-zero (exit 2).** Validate before opening a write transaction.
- **The adjustment is derived at read time.** Never write adjusted quantities back to the `fill` table.
- **No schema change.** `corporate_action` already has every column.
- **The test database is SHARED and PERSISTENT**, and `instrument` rows are global. Never assert on an unqualified `SELECT count(*)`; scope assertions to rows the test created, and probe only through the transaction-rolled-back `conn` fixture.
- **DB tests skip silently without `TEST_PG_DSN`.** Always `set -a && . ./.env && set +a && uv run pytest <file>`, and read the summary line to confirm it says neither "skipped" nor a stale count.
- **Do not run the full suite** (~6.5 minutes; the controller runs it). **Name the test FILE in selectors, never a `-k` substring** — a silently under-selecting `-k` looks identical to a passing run and has bitten this project twice.
- **Every new test is gated against a mutant.** Report each CAUGHT or SURVIVED honestly.
- **This repo is PUBLIC.** `imports/` holds real exports. Use fabricated symbols only (`ZXCO`). The deny-list guards identifiers, not values — real quantities and dates have reached a draft spec twice on this project. Never copy a quantity, price, date or symbol out of `imports/`.

---

## File Structure

| File | Responsibility |
|---|---|
| `db/corporate.py` | **new** — storage and preview: `add_action`, `list_actions`, `remove_action`, `find_duplicate`, `actions_for_instruments`, `preview_effect`, `EffectPreview` |
| `db/trades.py` | modify: `regroup_account` applies actions between the manual reduction and `group_fills` |
| `cli.py` | modify: `corporate add` / `corporate list` / `corporate remove` |
| `tests/db/test_corporate_actions.py` | **new** — the storage layer (distinct from `tests/test_corporate.py`, which tests the pure layer) |
| `tests/db/test_trades.py` | modify: the regroup wiring, including ordering against manual holdings |
| `tests/db/test_cli.py` | modify: the three subcommands |
| `docs/known-gaps.md`, `README.md` | modify |

---

## Task 1: Storage and preview

**Files:**
- Create: `db/corporate.py`
- Test: `tests/db/test_corporate_actions.py`

**Interfaces:**
- Consumes: `ledger.corporate.{ActionType, CorporateAction}` (existing).
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class EffectPreview:
      accounts: int
      fills_changed: int
      samples: tuple[tuple[Fill, Fill], ...]   # (before, after), at most 3

  async def add_action(conn, action: CorporateAction, note: str | None = None) -> UUID
  async def list_actions(conn, instrument_id: UUID | None = None) -> list[asyncpg.Record]
  async def remove_action(conn, action_id: UUID) -> bool
  async def find_duplicate(conn, instrument_id: UUID, ex_date: date,
                           action_type: ActionType) -> UUID | None
  async def actions_for_instruments(conn, instrument_ids: Sequence[UUID]) -> list[CorporateAction]
  async def preview_effect(conn, instrument_id: UUID, *,
                           adding: CorporateAction | None = None,
                           removing: UUID | None = None) -> EffectPreview
  ```

**Read first:** `db/marks.py` (this module follows its shape — a thin async function per operation, `Decimal` passed through untouched) and `db/cash.py` (for the fetch-map-delegate pattern `preview_effect` uses).

**`actions_for_instruments` returns the pure dataclass, not rows.** That is the whole point of the interface: `regroup_account` hands its result straight to `adjust_fills`. Converting `action_type` TEXT to `ActionType`, and the NUMERIC columns to `Decimal`, happens here and nowhere else.

**`preview_effect` is the cumulative diff** (spec §5). For each account holding fills on the instrument it fetches those fills, runs `adjust_fills` twice — once with the stored actions, once with the stored actions plus/minus the proposed change — and diffs. Computing the proposed action against *raw* fills instead would make a second identical entry look exactly like a first.

- [ ] **Step 1: Write the failing tests**

Build fixtures with `create_account`, `upsert_instrument` and `insert_fills`. **Read `tests/db/test_positions.py` first** and reuse its fixture-building pattern rather than inventing another. There is no `tests/db/conftest.py`, so define fixtures locally in the new file.

```python
def _split(instrument_id, *, num="1", den="6", ex_date=date(2026, 3, 2),
           action_type=ActionType.REVERSE_SPLIT):
    return CorporateAction(
        instrument_id=instrument_id,
        action_type=action_type,
        ex_date=ex_date,
        ratio_numerator=Decimal(num),
        ratio_denominator=Decimal(den),
    )


async def test_an_action_round_trips(conn, an_instrument):
    action_id = await add_action(conn, _split(an_instrument))
    (row,) = await list_actions(conn, an_instrument)
    assert row["id"] == action_id
    assert row["action_type"] == "reverse_split"
    assert row["ratio_numerator"] == Decimal(1)
    assert row["ratio_denominator"] == Decimal(6)


async def test_actions_for_instruments_returns_the_pure_dataclass(conn, an_instrument):
    """regroup_account hands this straight to adjust_fills, which takes
    CorporateAction -- not a Record. Returning rows would push the TEXT-to-enum
    and NUMERIC-to-Decimal conversion into the caller, where it would be done
    once per call site and eventually wrong in one of them."""
    await add_action(conn, _split(an_instrument))
    (action,) = await actions_for_instruments(conn, [an_instrument])
    assert isinstance(action, CorporateAction)
    assert action.action_type is ActionType.REVERSE_SPLIT
    assert action.ratio_denominator == Decimal(6)


async def test_actions_are_scoped_to_the_instruments_asked_for(conn, two_instruments):
    a, b = two_instruments
    await add_action(conn, _split(a))
    assert await actions_for_instruments(conn, [b]) == []


async def test_find_duplicate_matches_on_instrument_ex_date_and_type(conn, an_instrument):
    """Entering the same 1:6 reverse split twice applies it twice -- a 1:36
    restatement that looks plausible at every individual step."""
    action_id = await add_action(conn, _split(an_instrument))
    assert await find_duplicate(
        conn, an_instrument, date(2026, 3, 2), ActionType.REVERSE_SPLIT
    ) == action_id


async def test_find_duplicate_ignores_a_different_ex_date(conn, an_instrument):
    await add_action(conn, _split(an_instrument))
    assert await find_duplicate(
        conn, an_instrument, date(2026, 4, 2), ActionType.REVERSE_SPLIT
    ) is None


async def test_remove_action_deletes_and_reports(conn, an_instrument):
    action_id = await add_action(conn, _split(an_instrument))
    assert await remove_action(conn, action_id) is True
    assert await list_actions(conn, an_instrument) == []


async def test_remove_action_reports_false_for_an_unknown_id(conn):
    assert await remove_action(conn, uuid4()) is False


async def test_preview_counts_the_fills_a_new_action_would_change(conn, account_with_1800):
    """1800 shares at 0.05, reverse split 1:6 -> 300 at 0.30."""
    account_id, instrument_id = account_with_1800
    preview = await preview_effect(conn, instrument_id, adding=_split(instrument_id))
    assert preview.fills_changed == 1
    assert preview.accounts == 1
    (before, after), = preview.samples
    assert before.quantity == Decimal(1800)
    assert after.quantity == Decimal(300)


async def test_preview_is_cumulative_not_against_raw_fills(conn, account_with_1800):
    """With one 1:6 split already stored, previewing a SECOND action must show
    the incremental change from 300, not another 1800 -> 300. An isolated
    preview would render a duplicate entry indistinguishable from a first."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _split(instrument_id))
    second = _split(instrument_id, ex_date=date(2026, 4, 2))
    preview = await preview_effect(conn, instrument_id, adding=second)
    (before, after), = preview.samples
    assert before.quantity == Decimal(300)
    assert after.quantity == Decimal(50)


async def test_preview_of_a_removal_shows_the_reverse(conn, account_with_1800):
    account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _split(instrument_id))
    preview = await preview_effect(conn, instrument_id, removing=action_id)
    (before, after), = preview.samples
    assert before.quantity == Decimal(300)
    assert after.quantity == Decimal(1800)


async def test_preview_of_an_instrument_with_no_fills_is_empty(conn, an_instrument):
    """A legitimately pre-recorded future action, not an error."""
    preview = await preview_effect(conn, an_instrument, adding=_split(an_instrument))
    assert preview.fills_changed == 0
    assert preview.accounts == 0
```

Build `account_with_1800` as one account holding a single BUY fill of `1800` at `0.05` on a fabricated `ZXCO` equity, executed before the `2026-03-02` ex-date.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_corporate_actions.py -v`
Expected: FAIL — no module `db.corporate`. **If you see "skipped", `TEST_PG_DSN` is unset and you are testing nothing.**

- [ ] **Step 3: Implement**

```python
# db/corporate.py
"""Corporate action storage, and the preview of what applying one would change.

The adjustment itself lives in ledger/corporate.py and is never performed here:
this module fetches, maps to the pure dataclass, and delegates -- the same
shape db/cash.py has.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

import asyncpg

from db.fills import fetch_fills
from ledger.corporate import ActionType, CorporateAction, adjust_fills
from ledger.types import Fill


@dataclass(frozen=True, slots=True)
class EffectPreview:
    accounts: int
    fills_changed: int
    # (before, after) pairs, capped -- the preview is for a human deciding
    # whether the ratio is right, not an audit log.
    samples: tuple[tuple[Fill, Fill], ...]


_SAMPLE_CAP = 3


def _to_action(row: asyncpg.Record) -> CorporateAction:
    return CorporateAction(
        instrument_id=row["instrument_id"],
        action_type=ActionType(row["action_type"]),
        ex_date=row["ex_date"],
        ratio_numerator=row["ratio_numerator"],
        ratio_denominator=row["ratio_denominator"],
        resulting_instrument_id=row["resulting_instrument_id"],
        cash_component=row["cash_component"],
        basis_allocation=row["basis_allocation"],
    )


async def add_action(
    conn: asyncpg.Connection, action: CorporateAction, note: str | None = None
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO corporate_action
            (instrument_id, action_type, ex_date, ratio_numerator,
             ratio_denominator, resulting_instrument_id, cash_component,
             basis_allocation, note)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        RETURNING id
        """,
        action.instrument_id,
        action.action_type.value,
        action.ex_date,
        action.ratio_numerator,
        action.ratio_denominator,
        action.resulting_instrument_id,
        action.cash_component,
        action.basis_allocation,
        note,
    )


async def list_actions(
    conn: asyncpg.Connection, instrument_id: UUID | None = None
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            """
            SELECT * FROM corporate_action
             WHERE ($1::uuid IS NULL OR instrument_id = $1)
             ORDER BY ex_date, action_type
            """,
            instrument_id,
        )
    )


async def remove_action(conn: asyncpg.Connection, action_id: UUID) -> bool:
    """True if a row was deleted. False means the id was unknown -- the caller
    refuses rather than reporting a successful no-op."""
    result = await conn.execute("DELETE FROM corporate_action WHERE id = $1", action_id)
    return result != "DELETE 0"


async def find_duplicate(
    conn: asyncpg.Connection,
    instrument_id: UUID,
    ex_date: date,
    action_type: ActionType,
) -> UUID | None:
    """The id of an existing action with the same key, or None.

    There is no UNIQUE constraint on the table (adding one is a migration and
    is out of scope), so this is an application-level guard. Its absence at the
    database level is a recorded gap.
    """
    return await conn.fetchval(
        """
        SELECT id FROM corporate_action
         WHERE instrument_id = $1 AND ex_date = $2 AND action_type = $3
         LIMIT 1
        """,
        instrument_id,
        ex_date,
        action_type.value,
    )


async def actions_for_instruments(
    conn: asyncpg.Connection, instrument_ids: Sequence[UUID]
) -> list[CorporateAction]:
    if not instrument_ids:
        return []
    rows = await conn.fetch(
        """
        SELECT * FROM corporate_action
         WHERE instrument_id = ANY($1::uuid[]) OR resulting_instrument_id = ANY($1::uuid[])
         ORDER BY ex_date
        """,
        list(instrument_ids),
    )
    return [_to_action(r) for r in rows]


async def preview_effect(
    conn: asyncpg.Connection,
    instrument_id: UUID,
    *,
    adding: CorporateAction | None = None,
    removing: UUID | None = None,
) -> EffectPreview:
    """What would change if `adding` were stored, or `removing` deleted.

    CUMULATIVE, not the proposed action against raw fills. With one 1:6 split
    already stored, previewing a second identical one against raw fills would
    print the same plausible 1800 -> 300 while the stored state silently became
    1800 -> 50. See the design's section 5.
    """
    stored = await actions_for_instruments(conn, [instrument_id])
    if adding is not None:
        proposed = [*stored, adding]
    else:
        keep = {r["id"]: _to_action(r) for r in await list_actions(conn, instrument_id)}
        keep.pop(removing, None)
        proposed = list(keep.values())

    account_ids = [
        r["account_id"]
        for r in await conn.fetch(
            "SELECT DISTINCT account_id FROM fill WHERE instrument_id = $1", instrument_id
        )
    ]

    accounts = 0
    changed = 0
    samples: list[tuple[Fill, Fill]] = []
    for account_id in account_ids:
        fills = await fetch_fills(conn, account_id)
        before = {f.id: f for f in adjust_fills(fills, stored)}
        after = {f.id: f for f in adjust_fills(fills, proposed)}
        touched = 0
        for fill_id, b in before.items():
            a = after.get(fill_id)
            if a is None or a.quantity != b.quantity or a.price != b.price:
                touched += 1
                if len(samples) < _SAMPLE_CAP and a is not None:
                    samples.append((b, a))
        if touched:
            accounts += 1
            changed += touched

    return EffectPreview(accounts=accounts, fills_changed=changed, samples=tuple(samples))
```

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_corporate_actions.py -v`

- [ ] **Step 5: Mutation gate**

- Make `actions_for_instruments` return the raw rows → `test_actions_for_instruments_returns_the_pure_dataclass` must FAIL.
- Make `preview_effect` compute `after` from `adjust_fills(fills, [adding])` against raw fills → `test_preview_is_cumulative_not_against_raw_fills` must FAIL.
- Drop the `ex_date` predicate from `find_duplicate` → `test_find_duplicate_ignores_a_different_ex_date` must FAIL.
- Make `remove_action` always return `True` → `test_remove_action_reports_false_for_an_unknown_id` must FAIL.
- Drop the `instrument_id = ANY($1)` predicate from `actions_for_instruments` → `test_actions_are_scoped_to_the_instruments_asked_for` must FAIL. If deleting the predicate outright causes an asyncpg argument-count error rather than a behavioural failure, adapt it minimally (a tautology such as `$1::uuid[] IS NOT NULL`) and say exactly what you changed.

- [ ] **Step 6: Commit**

```bash
git add db/corporate.py tests/db/test_corporate_actions.py
git commit -m "feat(db): corporate action storage and cumulative preview"
```

---

## Task 2: Apply actions in `regroup_account`

**Files:**
- Modify: `db/trades.py` (`regroup_account`, between the fills loop ending at line ~79 and `seen_openings` at line ~81)
- Test: `tests/db/test_trades.py`

**Interfaces:**
- Consumes: `db.corporate.actions_for_instruments`, `ledger.corporate.adjust_fills`.
- Produces: no new API. `regroup_account` keeps its signature and return type.

**This is the correctness core of the plan.** Everything else is plumbing around it.

**Read `db/trades.py:20-81` first.** `regroup_account` resolves the account, computes `manual_held` (how much of each fill a manual trade already claims), fetches fills, reduces each by that holding, drops any reduced to zero, and hands the remainder onward.

**Insert the adjustment after that loop and before `seen_openings`.** It must precede the `if fills:` block, because `by_id`, `multipliers` and `underlyings` are all derived from `fills` — a spinoff child carries a new id and a different `instrument_id`, and those lookups have to see it.

**The order is load-bearing and the reverse is a bug.** `trade_fill` quantities were recorded in the units that existed when the manual grouping was made — pre-split units. Adjusting first would compare a fill scaled from 1800 to 300 against a manual holding of 1800, yield a negative remainder, and drop the fill entirely.

- [ ] **Step 1: Write the failing tests**

These live in a different file from Task 1's, so they need their own copies of the helper and fixture — pytest fixtures do not cross module boundaries and there is no `tests/db/conftest.py`:

```python
def _split(instrument_id, *, num="1", den="6", ex_date=date(2026, 3, 2),
           action_type=ActionType.REVERSE_SPLIT):
    return CorporateAction(
        instrument_id=instrument_id,
        action_type=action_type,
        ex_date=ex_date,
        ratio_numerator=Decimal(num),
        ratio_denominator=Decimal(den),
    )
```

`account_with_1800` is one account holding a single BUY fill of `1800` at `0.05` on a fabricated `ZXCO` equity, executed before the `2026-03-02` ex-date. Build it with `create_account`, `upsert_instrument` and `insert_fills`, following `tests/db/test_positions.py`'s existing fixture pattern.

```python
async def test_a_stored_reverse_split_reaches_the_position(conn, account_with_1800):
    """The whole point of the subsystem. Before this wiring, adjust_fills was
    never called anywhere in production code -- an action could be stored and
    would change nothing."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)
    (position,) = await open_positions(conn, account_id)
    assert position.quantity == Decimal(300)


async def test_removing_the_action_restores_the_original_quantity(conn, account_with_1800):
    """Derived at read time, so removal is a genuine undo rather than a second
    restatement."""
    account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)
    await remove_action(conn, action_id)
    await regroup_account(conn, account_id)
    (position,) = await open_positions(conn, account_id)
    assert position.quantity == Decimal(1800)


async def test_the_stored_fill_is_never_rewritten(conn, account_with_1800):
    """Fills are ground truth. The adjustment is a view over them, and writing
    it back would make the action impossible to undo and double-apply on the
    next regroup."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)
    (fill,) = await fetch_fills(conn, account_id)
    assert fill.quantity == Decimal(1800)


async def test_a_fill_partly_held_by_a_manual_trade_is_not_dropped(conn, partly_manual_account):
    """The ordering test. trade_fill quantities are in pre-split units, so
    adjusting BEFORE the manual reduction compares 300 against a holding of
    1200, yields a negative remainder, and drops the fill from the ledger
    entirely. Reversing the two steps must turn this red."""
    account_id, instrument_id = partly_manual_account
    await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)
    positions = await open_positions(conn, account_id)
    assert positions != []


async def test_an_account_with_no_actions_is_unaffected(conn, account_with_1800):
    account_id, _instrument_id = account_with_1800
    await regroup_account(conn, account_id)
    (position,) = await open_positions(conn, account_id)
    assert position.quantity == Decimal(1800)
```

Build `partly_manual_account` as one account with a single BUY fill of `1800`, of which a manual trade holds `1200` — so `600` reaches the grouper and, after a 1:6 split, `100`.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_trades.py -v`
Expected: FAIL — the position is still `1800`, because nothing applies the action.

- [ ] **Step 3: Implement**

Add the import and insert this immediately after the `fills` list is built, before `seen_openings`:

```python
    # Corporate actions are applied HERE: after the manual reduction, before
    # grouping -- and never written back to the fill table. Fills are ground
    # truth, an action is a separate fact, and the adjusted view is a
    # consequence of both. That is what makes removing an action a genuine undo
    # rather than a second restatement.
    #
    # THE ORDER IS LOAD-BEARING. trade_fill quantities were recorded in the
    # units that existed when a manual grouping was made -- pre-split units. If
    # adjustment ran first, a fill scaled from 1800 to 300 would be compared
    # against a manual holding of 1800, yield a negative remainder, and be
    # dropped entirely: the fill would vanish from the ledger rather than
    # merely being mis-sized.
    #
    # A consequence, recorded as a gap rather than solved here: fills WHOLLY
    # owned by a manual trade never reach this point (they are skipped above),
    # so manual groupings are not split-adjusted.
    if fills:
        actions = await actions_for_instruments(
            conn, list({f.instrument_id for f in fills})
        )
        if actions:
            fills = adjust_fills(fills, actions)
```

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_trades.py tests/db/test_positions.py -v`

Run `tests/db/test_positions.py` too: it is the closest consumer of `regroup_account`'s output and will catch a regression this task could plausibly cause.

- [ ] **Step 5: Mutation gate**

- Move the adjustment **before** the manual-reduction loop → `test_a_fill_partly_held_by_a_manual_trade_is_not_dropped` must FAIL.
- Delete the `adjust_fills` call → `test_a_stored_reverse_split_reaches_the_position` must FAIL.
- Write the adjusted quantity back with an `UPDATE fill SET quantity = ...` → `test_the_stored_fill_is_never_rewritten` must FAIL. Revert carefully; this mutation touches the database.

- [ ] **Step 6: Commit**

```bash
git add db/trades.py tests/db/test_trades.py
git commit -m "feat(db): apply corporate actions when regrouping"
```

---

## Task 3: The `corporate` command

**Files:**
- Modify: `cli.py`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `db.corporate.{add_action, list_actions, remove_action, find_duplicate, preview_effect}`, `db.marks.resolve_instrument_by_symbol`, `db.trades.regroup_account`.
- Produces: `cmd_corporate_add(args)`, `cmd_corporate_list(args)`, `cmd_corporate_remove(args)`.

**Read `cmd_marks_set` and `cmd_snapshot_add` first** — this follows their structure exactly: parse and validate everything, refuse before opening a connection, and follow `cmd_trades`' pool handling **including its comment** (`pool.close()` runs after the `async with pool.acquire()` block exits, never inside it, or it deadlocks).

Arguments:

- `corporate add --type {split,reverse_split,merger,spinoff,symbol_change} --symbol S --ex-date D --ratio NEW:OLD [--resulting-symbol S] [--basis-allocation F] [--note T] [--commit]`
- `corporate list [--symbol S]`
- `corporate remove ID [--commit]`

**`--ratio NEW:OLD` maps to `ratio_numerator:ratio_denominator`**, the direction `adjust_fills` consumes: a quantity is scaled by `numerator / denominator`. A 1-for-6 reverse split is `--ratio 1:6` and scales 1800 to 300. Inverting it turns a reverse split into a 6× forward split — wrong by a factor of 36, with every step still looking plausible.

**`add` and `remove` preview by default and write only with `--commit`**, mirroring `import`. On `--commit` both regroup **every account holding the instrument**, in one transaction, because positions come from materialised `trade` rows and are otherwise silently stale.

- [ ] **Step 1: Write the failing tests**

Adapt to whatever `tests/db/test_cli.py` already uses for the fake pool and argument namespaces — **read it first**; it has a `_FakePool(conn)` class and builds `argparse.Namespace` by hand. It already has `_args` (marks) and `_snapshot_args`; add a distinctly named third rather than widening either.

This is a third file again, so it needs its own copies of the helper and fixture:

```python
def _split(instrument_id, *, num="1", den="6", ex_date=date(2026, 3, 2),
           action_type=ActionType.REVERSE_SPLIT):
    return CorporateAction(
        instrument_id=instrument_id,
        action_type=action_type,
        ex_date=ex_date,
        ratio_numerator=Decimal(num),
        ratio_denominator=Decimal(den),
    )
```

`account_with_1800` is as in Task 2: one account, one BUY fill of `1800` at `0.05` on a fabricated `ZXCO` equity, executed before the `2026-03-02` ex-date. `_corporate_args(**kw)` builds an `argparse.Namespace` with the flags the subcommand registers, defaulting anything not passed to `None`/`False`.

```python
async def test_corporate_add_previews_without_writing(conn, account_with_1800, monkeypatch, capsys):
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="reverse_split", symbol="ZXCO", ex_date="2026-03-02",
        ratio="1:6", commit=False))
    assert rc == 0
    assert await list_actions(conn, instrument_id) == []
    out = capsys.readouterr().out
    assert "preview only" in out
    assert "300" in out


async def test_corporate_add_commits_and_regroups(conn, account_with_1800, monkeypatch):
    """Positions come from materialised trade rows, so an add that does not
    regroup leaves them silently stale."""
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="reverse_split", symbol="ZXCO", ex_date="2026-03-02",
        ratio="1:6", commit=True))
    assert rc == 0
    (position,) = await open_positions(conn, account_id)
    assert position.quantity == Decimal(300)


async def test_corporate_add_refuses_a_duplicate_without_writing(conn, account_with_1800, monkeypatch, capsys):
    """The same 1:6 split entered twice is a 1:36 restatement that looks
    plausible at every individual step."""
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    args = _corporate_args(type="reverse_split", symbol="ZXCO",
                           ex_date="2026-03-02", ratio="1:6", commit=True)
    assert await cli.cmd_corporate_add(args) == 0
    assert await cli.cmd_corporate_add(args) == 2
    assert len(await list_actions(conn, instrument_id)) == 1
    assert "already" in capsys.readouterr().err.lower()


async def test_corporate_add_refuses_a_spinoff_with_no_basis_allocation(conn, account_with_1800, monkeypatch):
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="spinoff", symbol="ZXCO", ex_date="2026-03-02", ratio="1:1",
        resulting_symbol="ZXCB", commit=True))
    assert rc == 2
    assert await list_actions(conn, instrument_id) == []


async def test_corporate_add_refuses_a_malformed_ratio_without_writing(conn, account_with_1800, monkeypatch):
    account_id, instrument_id = account_with_1800
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="reverse_split", symbol="ZXCO", ex_date="2026-03-02",
        ratio="one-to-six", commit=True))
    assert rc == 2
    assert await list_actions(conn, instrument_id) == []


async def test_corporate_add_refuses_an_unknown_symbol(conn, monkeypatch):
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_add(_corporate_args(
        type="reverse_split", symbol="NOSUCH", ex_date="2026-03-02",
        ratio="1:6", commit=True))
    assert rc == 2


async def test_corporate_list_shows_stored_actions(conn, account_with_1800, monkeypatch, capsys):
    account_id, instrument_id = account_with_1800
    await add_action(conn, _split(instrument_id))
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_list(_corporate_args(symbol="ZXCO"))
    assert rc == 0
    assert "reverse_split" in capsys.readouterr().out


async def test_corporate_remove_undoes_the_adjustment(conn, account_with_1800, monkeypatch):
    account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_remove(_corporate_args(id=str(action_id), commit=True))
    assert rc == 0
    (position,) = await open_positions(conn, account_id)
    assert position.quantity == Decimal(1800)


async def test_corporate_remove_refuses_an_unknown_id(conn, monkeypatch):
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_corporate_remove(_corporate_args(id=str(uuid4()), commit=True))
    assert rc == 2
```

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`
Expected: FAIL — `cli` has no attribute `cmd_corporate_add`. Run the whole file; do not use an unverified `-k`.

- [ ] **Step 3: Implement**

Register the subcommand group and write the three handlers. Parse `--ratio` with a helper that refuses anything other than two positive finite `Decimal`s separated by a colon, catching `InvalidOperation` (which is **not** a `ValueError` subclass) and rejecting non-finite values with `is_finite()` — the same pair of guards `cmd_marks_set` and `cmd_snapshot_add` already carry. Build the `CorporateAction` before opening the pool so its `__post_init__` validation (spinoff needs `basis_allocation`; merger/spinoff/symbol_change need a resulting instrument; a resulting instrument may not equal the source) refuses without a connection.

```python
    p_corp = sub.add_parser("corporate", help="corporate actions")
    corp_sub = p_corp.add_subparsers(dest="corporate_command", required=True)

    p_corp_add = corp_sub.add_parser("add", help="record a corporate action")
    p_corp_add.add_argument("--type", required=True,
                            choices=[t.value for t in ActionType])
    p_corp_add.add_argument("--symbol", required=True)
    p_corp_add.add_argument("--ex-date", required=True, help="ISO-8601 date")
    p_corp_add.add_argument("--ratio", required=True, help="NEW:OLD, e.g. 1:6")
    p_corp_add.add_argument("--resulting-symbol", default=None)
    p_corp_add.add_argument("--basis-allocation", default=None)
    p_corp_add.add_argument("--note", default=None)
    p_corp_add.add_argument("--commit", action="store_true")
    p_corp_add.set_defaults(fn=cmd_corporate_add)

    p_corp_list = corp_sub.add_parser("list", help="show stored corporate actions")
    p_corp_list.add_argument("--symbol", default=None)
    p_corp_list.set_defaults(fn=cmd_corporate_list)

    p_corp_rm = corp_sub.add_parser("remove", help="delete a corporate action")
    p_corp_rm.add_argument("id")
    p_corp_rm.add_argument("--commit", action="store_true")
    p_corp_rm.set_defaults(fn=cmd_corporate_remove)
```

On `--commit`, both `add` and `remove` must, inside one transaction: write (or delete), then regroup every account holding the instrument —

```python
            account_ids = [
                r["account_id"]
                for r in await conn.fetch(
                    "SELECT DISTINCT account_id FROM fill WHERE instrument_id = $1",
                    instrument_id,
                )
            ]
            for account_id in account_ids:
                await regroup_account(conn, account_id)
```

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`

- [ ] **Step 5: Mutation gate**

- Skip the regroup on `--commit` → `test_corporate_add_commits_and_regroups` must FAIL.
- Drop the `find_duplicate` check → `test_corporate_add_refuses_a_duplicate_without_writing` must FAIL.
- Invert the ratio (`denominator:numerator`) → `test_corporate_add_commits_and_regroups` must FAIL, since the position would become 10800 rather than 300.
- Write the action before previewing when `--commit` is absent → `test_corporate_add_previews_without_writing` must FAIL.
- Regroup only the first account holding the instrument → report what reddens. If nothing does, say so: it means no test covers the multi-account case and the "every account" claim is unpinned.

- [ ] **Step 6: Commit**

```bash
git add cli.py tests/db/test_cli.py
git commit -m "feat(cli): deadband corporate add/list/remove"
```

---

## Task 4: Record what this leaves open

**Files:**
- Modify: `docs/known-gaps.md`, `README.md`

- [ ] **Step 1: Find the current highest gap number.**

`grep -oE '^\| [0-9]+ \|' docs/known-gaps.md | tail -3`. **Check, do not assume** — this file has had two renumbering incidents and one session hit a conflict where two branches each added the same number.

- [ ] **Step 2: Record the five gaps the spec's §9 names**, each in its own row, matching neighbouring rows' format and depth of reasoning (read several first — these rows carry real argument, not one-liners):

1. **Corporate actions still cannot be imported.** The two long-term accounts remain unimportable (gap #31) until the export's `FROM`/`TO` pairs can be parsed — which needs CUSIP resolution the `instrument` table cannot express, ratio derivation from paired quantities, and a three-row merger case.
2. **Manual trades are not split-adjusted**, because fills wholly owned by one never reach the grouper. Fixing it means deciding what a permanent user grouping means across a restatement.
3. **Merger cash is not modelled.** `CorporateAction.cash_component` is stored and never read by `adjust_fills`. A merger paying cash understates cash by that amount, and the field's existence invites the assumption that it works.
4. **No audit trail on restatement.** Adding or removing an action silently changes every affected position and realised figure; `list` shows current state but nothing records when an action was added or what it changed — the same shortcoming gap #15 records for `contract_multiplier` repainting.
5. **No database-level uniqueness** on `(instrument_id, ex_date, action_type)`. The duplicate refusal is application-level only; direct SQL could still double-enter. Adding the constraint is a migration and was kept out of scope.

- [ ] **Step 3: README** — document the three subcommands with a worked example, state that `add` and `remove` preview by default and write only with `--commit`, and explain `--ratio NEW:OLD` explicitly. Match the structure of the existing command documentation.

- [ ] **Step 4: Verify and commit.** Open every file you cite and check the line numbers resolve. Stage, run `.githooks/pre-commit` without bypassing, and confirm `git diff --cached --stat` shows only documentation.

```bash
git add docs/known-gaps.md README.md
git commit -m "docs: record what corporate-action support leaves open"
```

---

## Self-Review

**Spec coverage.** C1 (derived at read time) → Task 2, `test_the_stored_fill_is_never_rewritten`. C2 (wired into `regroup_account` only) → Task 2. C3 (all five types) → Task 3's `--type` choices, drawn from `ActionType`. C4 (preview by default, list and remove) → Task 3. C5 (cumulative preview) → Task 1's `preview_effect` and its dedicated test. C6 (duplicate refusal) → Task 1's `find_duplicate`, Task 3's refusal. C7 (regroup every holding account) → Task 3. C8 (merger cash unmodelled) → Task 4, gap 3. §4's ordering → Task 2, `test_a_fill_partly_held_by_a_manual_trade_is_not_dropped`. §6's failure policy, every row → Task 3. §9's five gaps → Task 4.

**Placeholders.** None. Every code step carries its code; every test step carries its test.

**Type consistency.** `EffectPreview(accounts, fills_changed, samples)` is defined in Task 1 and rendered in Task 3. `actions_for_instruments(conn, instrument_ids) -> list[CorporateAction]` is defined in Task 1 and consumed in Task 2, where `adjust_fills` requires exactly that type. `find_duplicate(...) -> UUID | None` and `remove_action(...) -> bool` are defined in Task 1 and drive Task 3's two refusals. `--ratio NEW:OLD` maps to `ratio_numerator:ratio_denominator` consistently in Task 3 and the spec.

**Known soft spot.** Task 3 is the largest task and its `--commit` path spans three modules (`db/corporate.py`, `db/marks.py`'s resolver, `db/trades.py`'s regroup) inside one transaction. Its last mutation deliberately asks what reddens when only the first account is regrouped, because I expect the multi-account claim to be the least-covered assertion in the plan — if nothing reddens, that is a real coverage gap and should be reported rather than passed over.
