# `reconcile` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close known gap #13 — `deadband reconcile` compares the ledger against a stored broker-statement snapshot and reports a single trustworthy verdict, plus `deadband snapshot add` to record the statement figures.

**Architecture:** Five thin units behind one command. A new pure `ledger/cash.py` owns the cash sign convention; `ledger/reconcile.py` is extended so the pure layer owns the reliability judgement and emits a `verdict`; `db/snapshots.py` and `db/cash.py` are plumbing; `cli.py` renders. No schema change — `account_snapshot` already has the columns.

**Tech Stack:** Python 3.11+, `uv`, asyncpg, pytest, hypothesis.

**Spec:** `docs/superpowers/specs/2026-08-10-reconcile-design.md`. Read §5 (cash derivation), §6 (verdict) and §8 (failure policy) before starting — they carry the reasoning this plan compresses.

---

## Global Constraints

- **Purity.** `ledger/` and `importers/` import no I/O, no clock, no randomness, and not the first-party `db`/`venues` packages. `tests/test_purity.py` enforces it.
- **`Decimal`, never `float`.** Pin precision with `localcontext()` around division; `ledger/reconcile.py` already uses `ctx.prec = 50`.
- **The clock lives in `cli.py`.** `db/` and `ledger/` never call `datetime.now()`.
- **`verdict` is the field callers render.** `is_within_tolerance` answers only "do the numbers agree" — a component, never the answer.
- **An unmarked position is not an unvaluable one.** Unmarked = known quantity, missing price; cost basis is a defensible stale proxy and the run stays reliable. Unvaluable = unknown or meaningless *quantity*; no proxy exists and the run becomes `UNRELIABLE`. Conflating them is the single most likely way to get this wrong.
- **Refusals write nothing and exit non-zero.**
- **The test database is SHARED and PERSISTENT**, and `instrument` rows are global. Never assert on an unqualified `SELECT count(*)`; scope assertions to rows the test created, and probe only through the transaction-rolled-back `conn` fixture, never a bare connection.
- **Tests must be able to fail.** For each assertion ask what mutation turns it red.
- **Every new test is gated against a mutant.** Report each CAUGHT or SURVIVED honestly.
- **Run the full suite yourself:** `set -a && . ./.env && set +a && uv run pytest`. Confirm the summary says neither "skipped" nor a stale count (~330s). Never run a mutation harness while it is running.
- **Name test files in selectors, not `-k` substrings.** A `-k` filter that silently under-selects looks identical to a passing run; that has bitten this project twice.

---

## File Structure

| File | Responsibility |
|---|---|
| `ledger/cash.py` | **pure**: the cash sign convention. `net_cash(movements, fills) -> Decimal` |
| `ledger/reconcile.py` | modify: `ReconcileVerdict`, `UnvaluableRef`, `Drift.verdict`, `reconcile(..., unvaluable=...)` |
| `db/snapshots.py` | `add_snapshot`, `latest_snapshot` |
| `db/cash.py` | fetch movement and fill rows, delegate to `ledger/cash.py` |
| `cli.py` | modify: `snapshot add`, `reconcile` |
| `docs/known-gaps.md` | modify: close #13, record the six gaps §10 names |

---

## Task 1: The pure cash sign convention

**Files:**
- Create: `ledger/cash.py`
- Test: `tests/test_cash.py`

**Interfaces:**
- Consumes: `importers.base.OUTFLOW_KINDS` (existing: `frozenset({"withdrawal", "fee", "tax"})`)
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class CashMovementRow:
      kind: str
      amount: Decimal      # always positive; direction lives in `kind`

  @dataclass(frozen=True, slots=True)
  class CashFillRow:
      side: Side
      quantity: Decimal
      price: Decimal
      multiplier: Decimal
      fee: Decimal

  def net_cash(
      movements: Sequence[CashMovementRow], fills: Sequence[CashFillRow]
  ) -> Decimal
  ```

**Why this is pure and separate.** The sign convention is judgement, not plumbing, and `OUTFLOW_KINDS` has sat in `importers/base.py` since A-1 with a docstring anticipating exactly this consumer. A future Dashboard will want the same function.

**The formula** (spec §5):

```
cash = Σ movements(signed) + Σ sell_proceeds − Σ buy_costs

  movements(signed): −amount if kind in OUTFLOW_KINDS else +amount
  sell_proceeds:     quantity × price × multiplier − fee
  buy_costs:         quantity × price × multiplier + fee
```

**The multiplier is the thing to get right.** Two option contracts at $3.50 with a ×100 multiplier cost $700, not $7. Dropped, every option trade is wrong by a hundredfold, and the resulting equity figure looks like a plausible drift rather than a bug.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cash.py
from decimal import Decimal

import pytest

from ledger.cash import CashFillRow, CashMovementRow, net_cash
from ledger.types import Side


def mv(kind, amount):
    return CashMovementRow(kind=kind, amount=Decimal(amount))


def fl(side, qty, price, mult="1", fee="0"):
    return CashFillRow(
        side=side, quantity=Decimal(qty), price=Decimal(price),
        multiplier=Decimal(mult), fee=Decimal(fee),
    )


def test_no_rows_is_zero_cash():
    assert net_cash([], []) == Decimal(0)


@pytest.mark.parametrize(
    "kind,expected",
    [("deposit", "100"), ("dividend", "100"), ("interest", "100"), ("rebate", "100"),
     ("withdrawal", "-100"), ("fee", "-100"), ("tax", "-100")],
)
def test_each_movement_kind_carries_the_right_sign(kind, expected):
    """`amount` is always positive by convention (importers.base.OUTFLOW_KINDS);
    direction lives entirely in `kind`. A kind that subtracts when it should add
    is a 2x error in the wrong direction, not a rounding difference."""
    assert net_cash([mv(kind, "100")], []) == Decimal(expected)


def test_a_buy_spends_cash_and_a_sell_produces_it():
    assert net_cash([], [fl(Side.BUY, "10", "20")]) == Decimal("-200")
    assert net_cash([], [fl(Side.SELL, "10", "20")]) == Decimal("200")


def test_the_contract_multiplier_scales_a_fill_s_cash_effect():
    """Two option contracts at 3.50 with a x100 multiplier cost 700, not 7.
    Dropping the multiplier makes every option trade wrong by a hundredfold, and
    the resulting equity figure reads as a plausible drift rather than a bug."""
    assert net_cash([], [fl(Side.BUY, "2", "3.50", mult="100")]) == Decimal("-700")


def test_fees_reduce_proceeds_and_increase_cost():
    assert net_cash([], [fl(Side.BUY, "1", "100", fee="1.50")]) == Decimal("-101.50")
    assert net_cash([], [fl(Side.SELL, "1", "100", fee="1.50")]) == Decimal("98.50")


def test_a_drip_nets_to_its_residual_not_to_zero_and_not_to_double():
    """A dividend arrives as a CASH movement and the reinvestment spends it as a
    FILL with funding_source='reinvestment'. Both legs are recorded, so they
    cancel to the small residual that genuinely stayed in cash. Do NOT special-case
    reinvestment fills -- a special case here would double-count."""
    got = net_cash([mv("dividend", "11.30")], [fl(Side.BUY, "0.197", "57.25")])
    assert got == Decimal("11.30") - Decimal("0.197") * Decimal("57.25")
    assert got != Decimal(0)


def test_movements_and_fills_combine():
    got = net_cash(
        [mv("deposit", "1000"), mv("fee", "25")],
        [fl(Side.BUY, "10", "20"), fl(Side.SELL, "4", "30")],
    )
    assert got == Decimal("1000") - Decimal("25") - Decimal("200") + Decimal("120")
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_cash.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ledger.cash'`

- [ ] **Step 3: Implement**

```python
# ledger/cash.py
"""Cash balance from movements and fills. Pure — no I/O, no clock."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from importers.base import OUTFLOW_KINDS
from ledger.types import Side


@dataclass(frozen=True, slots=True)
class CashMovementRow:
    kind: str
    # Always positive -- direction lives in `kind`. See OUTFLOW_KINDS'
    # docstring in importers/base.py: a negative amount is a bug, not an outflow.
    amount: Decimal


@dataclass(frozen=True, slots=True)
class CashFillRow:
    side: Side
    quantity: Decimal
    price: Decimal
    multiplier: Decimal
    fee: Decimal


def net_cash(
    movements: Sequence[CashMovementRow], fills: Sequence[CashFillRow]
) -> Decimal:
    """The account's cash balance implied by everything the ledger holds.

    Cash CANNOT come from cash_movement alone: a buy spends cash as a FILL, not
    as a movement, so a balance built only from movements omits every trade.

    A DRIP needs no special case. The dividend is a movement in, the
    reinvestment is a fill out, and the two cancel to the residual that really
    stayed in cash. Adding a reinvestment special case here would double-count.

    Sweep rows are already absent: importers/fidelity.py classifies a
    sweep-fund reinvestment as INTERNAL so it is never counted twice (A2-9).
    """
    with localcontext() as ctx:
        # Same pin as ledger/pnl.py and ledger/reconcile.py.
        ctx.prec = 50
        total = Decimal(0)
        for m in movements:
            total += -m.amount if m.kind in OUTFLOW_KINDS else m.amount
        for f in fills:
            # The multiplier is load-bearing: 2 contracts at 3.50 with x100 is
            # 700, not 7.
            notional = f.quantity * f.price * f.multiplier
            total += (notional - f.fee) if f.side is Side.SELL else -(notional + f.fee)
        return total
```

- [ ] **Step 4: Run and confirm pass**

Run: `uv run pytest tests/test_cash.py tests/test_purity.py -v`
Expected: all pass, including purity.

- [ ] **Step 5: Mutation gate**

- Drop `* f.multiplier` → `test_the_contract_multiplier_scales...` must FAIL.
- Invert the `OUTFLOW_KINDS` test (`if m.kind not in OUTFLOW_KINDS`) → the parametrized sign test must FAIL.
- Swap the `Side.SELL` branch for `Side.BUY` → `test_a_buy_spends_cash_and_a_sell_produces_it` must FAIL.
- Add a reinvestment special case that skips such fills → `test_a_drip_nets_to_its_residual...` must FAIL. (There is no `funding_source` on `CashFillRow`, so this mutation means skipping *all* buys; if that makes several tests red rather than one, say so — it is a coarse mutation and proves less than the others.)

- [ ] **Step 6: Commit**

```bash
git add ledger/cash.py tests/test_cash.py
git commit -m "feat(ledger): pure cash netting over movements and fills"
```

---

## Task 2: The verdict, and unvaluable positions in the pure layer

**Files:**
- Modify: `ledger/reconcile.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: existing `Position`, `Snapshot`, `Drift`
- Produces:
  ```python
  class ReconcileVerdict(StrEnum):
      OK = "ok"
      DRIFT = "drift"
      UNRELIABLE = "unreliable"

  @dataclass(frozen=True, slots=True)
  class UnvaluableRef:
      instrument_id: UUID
      symbol: str
      reason: str

  def reconcile(
      snapshot: Snapshot,
      positions: Sequence[Position],
      marks: Mapping[UUID, Decimal],
      computed_cash: Decimal,
      unvaluable: Sequence[UnvaluableRef] = (),
      tolerance: Decimal = Decimal("0.01"),
  ) -> Drift
  ```
  `Drift` gains `verdict: ReconcileVerdict` and `unvaluable_positions: tuple[UnvaluableRef, ...]`.

`unvaluable` is keyword-safe with a default of `()`, so the ten existing `reconcile()` tests keep passing unchanged. **Do not edit them to add the parameter** — their continuing to pass untouched is evidence the extension is backward-compatible.

**`UNRELIABLE` outranks `DRIFT`** (spec §6). If an account has both an unvaluable position and a numeric disagreement, the verdict is `UNRELIABLE`: the disagreement cannot be attributed — it may be entirely the missing position, or may hide a real defect on top. Reporting `DRIFT` would imply a precision the data does not support.

- [ ] **Step 1: Write the failing tests**

```python
def unvaluable_ref(reason="open quantity unknown"):
    return UnvaluableRef(instrument_id=uuid4(), symbol="ZXCO", reason=reason)


def test_agreement_with_nothing_unvaluable_is_ok():
    d = reconcile(_snapshot(equity="1000", cash="1000"), [], {}, Decimal("1000"))
    assert d.verdict is ReconcileVerdict.OK
    assert d.is_within_tolerance is True
    assert d.unvaluable_positions == ()


def test_disagreement_with_nothing_unvaluable_is_drift():
    d = reconcile(_snapshot(equity="1000", cash="1000"), [], {}, Decimal("900"))
    assert d.verdict is ReconcileVerdict.DRIFT
    assert d.is_within_tolerance is False


def test_any_unvaluable_position_makes_the_run_unreliable():
    d = reconcile(
        _snapshot(equity="1000", cash="1000"), [], {}, Decimal("1000"),
        unvaluable=[unvaluable_ref()],
    )
    assert d.verdict is ReconcileVerdict.UNRELIABLE
    assert len(d.unvaluable_positions) == 1


def test_unreliable_outranks_drift():
    """Both conditions at once. The numeric gap cannot be attributed -- it may be
    entirely the unvalued position, or may hide a real defect on top -- so
    reporting DRIFT would imply a precision the data does not support."""
    d = reconcile(
        _snapshot(equity="1000", cash="1000"), [], {}, Decimal("900"),
        unvaluable=[unvaluable_ref()],
    )
    assert d.verdict is ReconcileVerdict.UNRELIABLE


def test_an_unreliable_run_still_reports_its_numbers():
    """The numbers are still useful for judging whether the gap explains the
    drift. Refusing to compute them would make one orphaned trade disable the
    check for the whole account."""
    d = reconcile(
        _snapshot(equity="1000", cash="1000"), [], {}, Decimal("900"),
        unvaluable=[unvaluable_ref()],
    )
    assert d.computed_cash == Decimal("900")
    assert d.equity_difference == Decimal("-100")


def test_is_within_tolerance_still_answers_only_the_numeric_question():
    """It is a COMPONENT of the verdict, never the answer. On an unreliable run
    whose numbers happen to agree it is still True -- which is exactly why a
    caller must render `verdict` and not this."""
    d = reconcile(
        _snapshot(equity="1000", cash="1000"), [], {}, Decimal("1000"),
        unvaluable=[unvaluable_ref()],
    )
    assert d.is_within_tolerance is True
    assert d.verdict is ReconcileVerdict.UNRELIABLE


def test_an_unmarked_but_valuable_position_does_not_make_the_run_unreliable():
    """The distinction the whole design turns on. Unmarked = known quantity,
    missing price; cost basis is a defensible stale proxy. Unvaluable = unknown
    quantity, for which no proxy exists."""
    pos = Position(
        instrument_id=uuid4(), quantity=Decimal("10"),
        cost_basis=Decimal("100"), multiplier=Decimal("1"),
    )
    d = reconcile(
        _snapshot(equity="2000", cash="1000"), [pos], {}, Decimal("1000")
    )
    assert d.unmarked_instruments == (pos.instrument_id,)
    assert d.verdict is not ReconcileVerdict.UNRELIABLE
```

Write `_snapshot(equity, cash)` as a small helper if `tests/test_reconcile.py` has no equivalent; **read the file first** and reuse whatever it already uses to build a `Snapshot`.

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL — `ImportError` on `ReconcileVerdict`.

- [ ] **Step 3: Implement**

Add to `ledger/reconcile.py`:

```python
class ReconcileVerdict(StrEnum):
    OK = "ok"
    DRIFT = "drift"
    UNRELIABLE = "unreliable"


@dataclass(frozen=True, slots=True)
class UnvaluableRef:
    """A position the ledger holds but cannot value. `instrument_id` may be a
    grouping key rather than a real instrument id -- db/positions.py uses a
    trade's own id when its instrument is unreachable -- so never look one up."""

    instrument_id: UUID
    symbol: str
    reason: str
```

`Drift` gains two fields:

```python
    unvaluable_positions: tuple[UnvaluableRef, ...]
    # THE field callers render. `is_within_tolerance` above answers only "do the
    # numbers agree" -- a component of this, never the answer. A caller reading
    # it alone would print a clean pass on an account with unvalued positions,
    # which is the misuse docs/known-gaps.md's gap #12 note already warns about
    # for `unvaluable_reason`. An enum cannot be half-read.
    verdict: ReconcileVerdict
```

and `reconcile()` grows `unvaluable: Sequence[UnvaluableRef] = ()` before `tolerance`, computing:

```python
        within = abs(equity_difference) <= tolerance and abs(cash_difference) <= tolerance
        if unvaluable:
            # UNRELIABLE outranks DRIFT: with something unvalued, a numeric gap
            # cannot be attributed. It may be entirely the missing position, or
            # may hide a real defect on top.
            verdict = ReconcileVerdict.UNRELIABLE
        elif within:
            verdict = ReconcileVerdict.OK
        else:
            verdict = ReconcileVerdict.DRIFT
```

- [ ] **Step 4: Run and confirm pass**

Run: `uv run pytest tests/test_reconcile.py tests/test_purity.py -v`
Expected: all pass — **including the ten pre-existing tests, unedited.**

- [ ] **Step 5: Mutation gate**

- Reorder so `within` is checked before `unvaluable` → `test_unreliable_outranks_drift` must FAIL.
- Make `verdict` ignore `unvaluable` entirely → `test_any_unvaluable_position_makes_the_run_unreliable` must FAIL.
- Make an unmarked position append to `unvaluable` → `test_an_unmarked_but_valuable_position...` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add ledger/reconcile.py tests/test_reconcile.py
git commit -m "feat(ledger): reconcile emits a verdict and carries unvaluable positions"
```

---

## Task 3: Snapshot storage

**Files:**
- Create: `db/snapshots.py`
- Test: `tests/db/test_snapshots.py`

**Interfaces:**
- Produces:
  ```python
  async def add_snapshot(conn, account_id: UUID, as_of: datetime,
                         cash_balance: Decimal, total_equity: Decimal,
                         source: str = "statement", note: str | None = None) -> None
  async def latest_snapshot(conn, account_id: UUID,
                            as_of: datetime | None = None) -> asyncpg.Record | None
  ```

No schema change. `account_snapshot` already has every column and `UNIQUE (account_id, as_of)`.

`latest_snapshot` returns the most recent row **on or before** `as_of` (or the most recent overall when `as_of` is `None`), ordered by `as_of DESC` — **not** by insertion. This is the same hazard `latest_marks` has: a correction entered today for last month's statement must not become "the latest".

Re-adding the same `as_of` **updates** it, same reasoning as `set_mark`: correcting a mistyped figure is the point, and the table has no history columns.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_snapshot_round_trips(conn, an_account):
    when = datetime(2026, 7, 31, tzinfo=UTC)
    await add_snapshot(conn, an_account, when, Decimal("2110.00"), Decimal("41203.18"))
    row = await latest_snapshot(conn, an_account)
    assert row["cash_balance"] == Decimal("2110.00")
    assert row["total_equity"] == Decimal("41203.18")
    assert row["as_of"] == when


async def test_the_latest_by_date_wins_not_the_last_written(conn, an_account):
    """Same hazard as latest_marks: a correction entered today for last month's
    statement must not become 'the latest'. Ordering is by as_of, not insertion."""
    newer = datetime(2026, 7, 31, tzinfo=UTC)
    older = datetime(2026, 6, 30, tzinfo=UTC)
    await add_snapshot(conn, an_account, newer, Decimal("10"), Decimal("100"))
    await add_snapshot(conn, an_account, older, Decimal("20"), Decimal("200"))
    assert (await latest_snapshot(conn, an_account))["total_equity"] == Decimal("100")


async def test_as_of_selects_the_most_recent_on_or_before(conn, an_account):
    await add_snapshot(conn, an_account, datetime(2026, 6, 30, tzinfo=UTC),
                       Decimal("20"), Decimal("200"))
    await add_snapshot(conn, an_account, datetime(2026, 7, 31, tzinfo=UTC),
                       Decimal("10"), Decimal("100"))
    row = await latest_snapshot(conn, an_account, datetime(2026, 7, 1, tzinfo=UTC))
    assert row["total_equity"] == Decimal("200")


async def test_rewriting_the_same_as_of_updates_rather_than_failing(conn, an_account):
    when = datetime(2026, 7, 31, tzinfo=UTC)
    await add_snapshot(conn, an_account, when, Decimal("10"), Decimal("100"))
    await add_snapshot(conn, an_account, when, Decimal("11"), Decimal("111"))
    assert (await latest_snapshot(conn, an_account))["total_equity"] == Decimal("111")


async def test_an_account_with_no_snapshot_returns_none(conn, an_account):
    """Absent must be distinguishable from a zero-equity snapshot."""
    assert await latest_snapshot(conn, an_account) is None


async def test_snapshots_are_scoped_to_their_account(conn, two_accounts):
    a, b = two_accounts
    await add_snapshot(conn, a, datetime(2026, 7, 31, tzinfo=UTC),
                       Decimal("10"), Decimal("100"))
    assert await latest_snapshot(conn, b) is None
```

Build `an_account`/`two_accounts` with `db.accounts.create_account` inside the `conn` fixture. **Read `tests/db/test_positions.py` first** and reuse its account-building helper rather than inventing another.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_snapshots.py -v`
Expected: FAIL — no module `db.snapshots`. **If you see "skipped", `TEST_PG_DSN` is unset and you are testing nothing.**

- [ ] **Step 3: Implement**

```python
# db/snapshots.py
"""Broker-statement snapshots. The figures reconcile compares against."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import asyncpg


async def add_snapshot(
    conn: asyncpg.Connection,
    account_id: UUID,
    as_of: datetime,
    cash_balance: Decimal,
    total_equity: Decimal,
    source: str = "statement",
    note: str | None = None,
) -> None:
    """Record what the broker reported. Re-adding the same `as_of` UPDATES it --
    correcting a mistyped figure is the point, and the table has no history
    columns. Same reasoning as db/marks.py's set_mark."""
    if as_of.tzinfo is None:
        raise ValueError("snapshot as_of must be timezone-aware")
    await conn.execute(
        """
        INSERT INTO account_snapshot
            (account_id, as_of, cash_balance, total_equity, source, note)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (account_id, as_of) DO UPDATE SET
            cash_balance = EXCLUDED.cash_balance,
            total_equity = EXCLUDED.total_equity,
            source       = EXCLUDED.source,
            note         = EXCLUDED.note
        """,
        account_id, as_of, cash_balance, total_equity, source, note,
    )


async def latest_snapshot(
    conn: asyncpg.Connection, account_id: UUID, as_of: datetime | None = None
) -> asyncpg.Record | None:
    """The most recent snapshot on or before `as_of`, by DATE not by insertion.

    A correction entered today for last month's statement must not become "the
    latest" -- the same ordering hazard db/marks.py's latest_marks has. Returns
    None when the account has none: absent must stay distinguishable from a
    genuine zero-equity snapshot.
    """
    return await conn.fetchrow(
        """
        SELECT * FROM account_snapshot
         WHERE account_id = $1 AND ($2::timestamptz IS NULL OR as_of <= $2)
         ORDER BY as_of DESC
         LIMIT 1
        """,
        account_id, as_of,
    )
```

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_snapshots.py -v`

- [ ] **Step 5: Mutation gate**

- `ORDER BY as_of DESC` → `ASC` → `test_the_latest_by_date_wins...` must FAIL.
- Drop the `as_of <= $2` predicate → `test_as_of_selects_the_most_recent_on_or_before` must FAIL.
- `DO UPDATE` → `DO NOTHING` → `test_rewriting_the_same_as_of_updates...` must FAIL.
- Drop the `account_id = $1` predicate → `test_snapshots_are_scoped_to_their_account` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add db/snapshots.py tests/db/test_snapshots.py
git commit -m "feat(db): account snapshot storage"
```

---

## Task 4: Reading cash from the database

**Files:**
- Create: `db/cash.py`
- Test: `tests/db/test_cash.py`

**Interfaces:**
- Consumes: `ledger.cash.{CashMovementRow, CashFillRow, net_cash}`
- Produces:
  ```python
  class MixedCurrencyError(RuntimeError): ...

  async def account_cash(conn, account_id: UUID) -> Decimal
  ```

`account_cash` fetches the account's cash movements and its fills (joined to `instrument` for `contract_multiplier`), maps them to the pure rows, and returns `net_cash(...)`.

**It refuses a mixed-currency account** (spec §7/R7). v1 does not model FX, and summing across currencies produces a confident wrong number. Raise `MixedCurrencyError` naming the currencies found. Check both `cash_movement.currency` and the instruments' `quote_currency` — an account can be single-currency in one and not the other.

- [ ] **Step 1: Write the failing tests**

```python
async def test_cash_combines_movements_and_fills(conn, funded_account):
    """A buy spends cash as a FILL, not a movement -- a balance built from
    movements alone would omit every trade."""
    assert await account_cash(conn, funded_account) == Decimal("745.00")


async def test_an_option_fill_uses_its_contract_multiplier(conn, option_account):
    """2 contracts at 3.50 with x100 costs 700, not 7. Dropping the multiplier
    makes the balance wrong by a hundredfold on every option trade."""
    assert await account_cash(conn, option_account) == Decimal("-700.00")


async def test_an_account_with_nothing_has_zero_cash(conn, an_account):
    assert await account_cash(conn, an_account) == Decimal(0)


async def test_cash_is_scoped_to_its_account(conn, two_funded_accounts):
    a, b = two_funded_accounts
    assert await account_cash(conn, a) != await account_cash(conn, b)


async def test_a_mixed_currency_account_is_refused(conn, mixed_currency_account):
    """v1 does not model FX. Summing across currencies produces a confident
    wrong number, which is the failure class this project exists to avoid."""
    with pytest.raises(MixedCurrencyError) as exc:
        await account_cash(conn, mixed_currency_account)
    assert "USD" in str(exc.value) and "EUR" in str(exc.value)
```

Build fixtures with `create_account`, `upsert_instrument` and the existing fill/cash insert helpers; **read `tests/db/test_importing.py` for how cash movements are inserted.** Pick figures that make each expected total obvious from the fixture.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cash.py -v`
Expected: FAIL — no module `db.cash`. Confirm no skips.

- [ ] **Step 3: Implement**

`account_cash` runs two queries — one over `cash_movement`, one over `fill` joined to `instrument` — collects the distinct currencies from both, raises `MixedCurrencyError` if more than one, and otherwise maps to `CashMovementRow`/`CashFillRow` and returns `net_cash(...)`. Keep every `Decimal` as asyncpg returns it; do not round.

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cash.py tests/test_cash.py -v`

- [ ] **Step 5: Mutation gate**

- Drop `contract_multiplier` from the join and pass `Decimal(1)` → the option test must FAIL.
- Drop the currency check → `test_a_mixed_currency_account_is_refused` must FAIL.
- Drop the account predicate from either query → `test_cash_is_scoped_to_its_account` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add db/cash.py tests/db/test_cash.py
git commit -m "feat(db): account cash balance, refusing mixed currency"
```

---

## Task 5: `deadband snapshot add`

**Files:**
- Modify: `cli.py`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `db.snapshots.add_snapshot`
- Produces: `cmd_snapshot_add(args)`

Arguments: `--account` (required), `--as-of` (required, ISO-8601 date or timestamp), `--equity` (required), `--cash` (required), `--note` (optional).

**The clock lives here.** A bare date like `2026-07-31` becomes midnight UTC. A naive timestamp is refused, matching `marks set`.

**Refuse a future `--as-of`** beyond the same 2-minute clock-skew tolerance `marks set` uses, and for the same reason: `latest_snapshot` treats the newest date as current, so a fat-fingered year would silently become the figure every reconciliation compares against. Reuse `marks set`'s existing tolerance constant — do not define a second one.

Follow `cmd_trades` for pool handling, **including its comment**: `pool.close()` runs after the `async with pool.acquire()` block exits, never inside it, or it deadlocks.

Validate `--equity`/`--cash` as `Decimal`, catching `InvalidOperation` (not a `ValueError` subclass) and rejecting non-finite values with `is_finite()` — the same guards `cmd_marks_set` already has. Refuse **before** opening a connection.

- [ ] **Step 1: Write the failing tests**

```python
async def test_snapshot_add_stores_the_figures(conn, an_account, monkeypatch):
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_snapshot_add(_args(
        account=str(an_account), as_of="2026-07-31",
        equity="41203.18", cash="2110.00", note=None))
    assert rc == 0
    row = await latest_snapshot(conn, an_account)
    assert row["total_equity"] == Decimal("41203.18")
    assert row["as_of"] == datetime(2026, 7, 31, tzinfo=UTC)


async def test_snapshot_add_refuses_a_future_as_of_without_writing(conn, an_account, monkeypatch, capsys):
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_snapshot_add(_args(
        account=str(an_account), as_of="2099-01-01",
        equity="1", cash="1", note=None))
    assert rc == 2
    assert await latest_snapshot(conn, an_account) is None


async def test_snapshot_add_refuses_a_non_finite_figure_without_writing(conn, an_account, monkeypatch):
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_snapshot_add(_args(
        account=str(an_account), as_of="2026-07-31",
        equity="NaN", cash="1", note=None))
    assert rc == 2
    assert await latest_snapshot(conn, an_account) is None
```

Adapt `_args` and `_fake_pool` to whatever `tests/db/test_cli.py` already uses — **read it first**; the names above are illustrative.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`
Expected: FAIL — `cli` has no attribute `cmd_snapshot_add`. Run the whole file; do not use an unverified `-k`.

- [ ] **Step 3: Implement**

Register the subcommand and write `cmd_snapshot_add`, mirroring `cmd_marks_set`'s structure: parse and validate everything first, open the pool last.

```python
    p_snapshot = sub.add_parser("snapshot", help="broker statement figures")
    snap_sub = p_snapshot.add_subparsers(dest="snapshot_command", required=True)
    p_snap_add = snap_sub.add_parser("add", help="record a statement's equity and cash")
    p_snap_add.add_argument("--account", required=True)
    p_snap_add.add_argument("--as-of", required=True, help="ISO-8601 date or timestamp")
    p_snap_add.add_argument("--equity", required=True, help="total equity the broker reports")
    p_snap_add.add_argument("--cash", required=True, help="cash balance the broker reports")
    p_snap_add.add_argument("--note", default=None)
    p_snap_add.set_defaults(fn=cmd_snapshot_add)
```

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`

- [ ] **Step 5: Mutation gate**

- Disable the future-date guard → `test_snapshot_add_refuses_a_future_as_of...` must FAIL.
- Remove the `is_finite()` check → `test_snapshot_add_refuses_a_non_finite_figure...` must FAIL.
- Move validation after `create_pool()` → the "without writing" assertions must still hold; if they do, say so — it means those tests are checking the write, not the ordering, and the ordering is unpinned.

- [ ] **Step 6: Commit**

```bash
git add cli.py tests/db/test_cli.py
git commit -m "feat(cli): deadband snapshot add"
```

---

## Task 6: `deadband reconcile`

**Files:**
- Modify: `cli.py`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `db.snapshots.latest_snapshot`, `db.positions.open_positions`, `db.marks.latest_marks`, `db.cash.{account_cash, MixedCurrencyError}`, `ledger.reconcile.{reconcile, Snapshot, Position, UnvaluableRef, ReconcileVerdict}`
- Produces: `cmd_reconcile(args)`

Arguments: `--account` (required), `--as-of` (optional, defaults to now), `--tolerance` (optional, defaults to `0.01`).

**Flow** (spec §7):

1. Resolve the account. **Unknown id refuses**, exit 2.
2. `latest_snapshot(conn, account_id, as_of)`. **None refuses**, exit 2 — reporting "zero drift" against nothing is the silent-success shape.
3. `open_positions(conn, account_id)`.
4. **Partition on `unvaluable_reason`, never on `direction`.** `None` → build a `Position`; otherwise an `UnvaluableRef` carrying the symbol and reason.
5. `latest_marks` for the valuable instrument ids only.
6. `account_cash(conn, account_id)`; `MixedCurrencyError` refuses, exit 2.
7. `reconcile(...)` → `Drift`.
8. Render by `verdict`. Exit 0 only on `OK`.

**The output must explain the alarming number.** On `UNRELIABLE`, `computed_equity` excludes the unvaluable position entirely, so the drift reads as a large negative figure that is expected and not necessarily wrong. Print that on the same screen, or the first real use sends someone chasing a phantom.

- [ ] **Step 1: Write the failing tests**

```python
async def test_reconcile_agrees_and_exits_zero(conn, reconcilable_account, monkeypatch, capsys):
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_reconcile(_args(account=str(reconcilable_account), as_of=None, tolerance=None))
    assert rc == 0
    assert "ok" in capsys.readouterr().out.lower()


async def test_reconcile_refuses_an_account_with_no_snapshot(conn, an_account, monkeypatch, capsys):
    """Reporting zero drift against nothing is the silent-success shape."""
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_reconcile(_args(account=str(an_account), as_of=None, tolerance=None))
    assert rc == 2
    assert "snapshot" in capsys.readouterr().err.lower()


async def test_an_unvaluable_position_never_exits_zero_even_when_numbers_agree(
    conn, unreliable_but_agreeing_account, monkeypatch, capsys
):
    """The whole point of the verdict. A caller reading is_within_tolerance
    alone would print a clean pass here."""
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_reconcile(_args(
        account=str(unreliable_but_agreeing_account), as_of=None, tolerance=None))
    out = capsys.readouterr().out
    assert rc == 1
    assert "unreliable" in out.lower()


async def test_an_unreliable_run_explains_why_its_drift_looks_large(
    conn, unreliable_account, monkeypatch, capsys
):
    """computed_equity excludes the unvalued position, so the drift reads as a
    big negative number that is expected. Saying so is the difference between a
    useful report and a phantom hunt."""
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    await cli.cmd_reconcile(_args(account=str(unreliable_account), as_of=None, tolerance=None))
    out = capsys.readouterr().out.lower()
    assert "excluded" in out or "not included" in out


async def test_reconcile_refuses_an_unknown_account(conn, monkeypatch, capsys):
    monkeypatch.setattr(cli, "create_pool", _fake_pool(conn))
    rc = await cli.cmd_reconcile(_args(account=str(uuid4()), as_of=None, tolerance=None))
    assert rc == 2
```

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`
Expected: FAIL — no `cmd_reconcile`.

- [ ] **Step 3: Implement**

Write `cmd_reconcile` following the flow above and register it:

```python
    p_reconcile = sub.add_parser("reconcile", help="compare the ledger against a statement snapshot")
    p_reconcile.add_argument("--account", required=True)
    p_reconcile.add_argument("--as-of", default=None, help="ISO-8601; defaults to now")
    p_reconcile.add_argument("--tolerance", default=None, help="default 0.01")
    p_reconcile.set_defaults(fn=cmd_reconcile)
```

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -v`

- [ ] **Step 5: Mutation gate**

- Render `is_within_tolerance` instead of `verdict` → `test_an_unvaluable_position_never_exits_zero...` must FAIL.
- Partition on `direction is None` instead of `unvaluable_reason is not None` → the unreliable tests must FAIL (a position can carry a real direction *and* a reason).
- Treat a missing snapshot as zero equity → `test_reconcile_refuses_an_account_with_no_snapshot` must FAIL.

- [ ] **Step 6: Full suite, then commit**

```bash
set -a && . ./.env && set +a && uv run pytest -q
```
Confirm neither "skipped" nor a stale count.

```bash
git add cli.py tests/db/test_cli.py
git commit -m "feat(cli): deadband reconcile"
```

---

## Task 7: Close gap #13 and record what this leaves open

**Files:**
- Modify: `docs/known-gaps.md`, `README.md`

- [ ] **Step 1: Close gap #13** with what shipped: `snapshot add` and `reconcile`, the verdict, and the three-part answer to what was missing — the write path, the type adapter, *and* the cash derivation that the gap never mentioned.

- [ ] **Step 2: Record the six gaps the spec's §10 names**, in the gap table, each in its own row: no statement parsing; mixed-currency accounts refused rather than handled; no snapshot history view; a snapshot cannot be deleted, only overwritten; `reconcile` refuses an unknown account id while `positions` does not; and reconcile cannot distinguish a missing fill from an unvaluable position when an account has both.

  Copy the numbering forward from whatever the file currently ends at — **check, do not assume.** This branch sits on top of PR #7, which renumbered once already.

- [ ] **Step 3: README** — document both commands with a worked example, and state plainly that `reconcile` exits 0 only on `OK`.

- [ ] **Step 4: Verify and commit.** Open every file you cite and check the line numbers. Stage, run `.githooks/pre-commit` without bypassing, confirm `git diff --cached --stat` shows only documentation.

```bash
git add docs/known-gaps.md README.md
git commit -m "docs: close gap 13, record what reconcile leaves open"
```

---

## Self-Review

**Spec coverage.** R1 (unvaluable ⇒ UNRELIABLE) → Task 2. R2 (stored snapshots) → Tasks 3, 5. R3 (judgement in the pure layer) → Task 2. R4 (single `verdict`) → Task 2, gated in Task 6. R5 (`Position` kept) → Task 6's adapter. R6 (cash from movements *and* fills) → Tasks 1, 4. R7 (mixed currency refused) → Task 4. R8 (manual entry) → Task 5. §8's failure policy → Tasks 4, 5, 6. §10's six gaps → Task 7.

**Placeholders.** None. Task 4's Step 3 and Task 6's Step 3 describe structure rather than transcribing full function bodies — deliberate, because both are assembly of already-specified pieces and the exact shape depends on helpers the implementer must read first (`tests/db/test_cli.py`'s pool fake, `test_importing.py`'s cash inserts). Every *decision* in them is stated; only the glue is left.

**Type consistency.** `net_cash(movements, fills) -> Decimal` (Task 1) is called with those types in Task 4. `UnvaluableRef(instrument_id, symbol, reason)` (Task 2) is built in Task 6. `latest_snapshot(conn, account_id, as_of=None) -> Record | None` (Task 3) is consumed in Task 6. `ReconcileVerdict` members are `OK`/`DRIFT`/`UNRELIABLE` throughout.

**Known soft spot.** Task 6 has the most fixtures of any task in this plan and they are the least specified — four accounts in different states (reconcilable, no-snapshot, unreliable-but-agreeing, unreliable-with-drift). That is where this plan is most likely to run long, and where an implementer is most likely to build a fifth harness instead of reusing `tests/db/test_positions.py`'s. Watch for it in review.
