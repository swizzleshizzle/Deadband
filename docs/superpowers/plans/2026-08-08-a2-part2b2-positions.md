# A-2 part 2b-2: the `positions` command — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close known gap #12 — `deadband positions` shows open positions per instrument with unrealized P&L where a mark exists — and add the minimal manual mark path that makes unrealized reachable at all.

**Architecture:** The same purity seam the rest of the codebase uses. A pure aggregator in `ledger/positions.py` turns trade rows into positions (weighted-average basis, estimated rollup, spread handling); `db/positions.py` and `db/marks.py` do the I/O; `cli.py` renders. The aggregator is the piece with the judgement in it, so it is the piece that is pure and exhaustively testable.

**Tech Stack:** Python 3.11+, `uv`, asyncpg, pytest, hypothesis (already a dev dependency).

---

## Scope corrections — READ FIRST

**Known gaps #10 and #11 are already closed.** The gap list is stale. Verified on 2026-08-08:

- `db/trades.py:128,145-147,167-169` persists `open_quantity`, `open_cost_basis` and `is_estimated` on every upsert.
- `db/trades.py:115` rolls `is_estimated` up with `any(...)` over allocations, not just the opening fill.
- Covered by `tests/db/test_trades.py::test_a_trade_containing_an_estimated_fill_is_itself_estimated`, `::test_a_trade_of_only_exact_fills_is_not_estimated`, and `::test_a_trade_with_an_estimated_closing_fill_is_also_estimated`.

A-2 part 1 did this work and never struck the entries. Task 6 removes them from the list. **Do not re-implement them** — if you find yourself adding a column or a rollup, stop and re-read this section.

**A `Position` dataclass already exists** at `ledger/reconcile.py:13-18` (`instrument_id`, `quantity`, `cost_basis`, `multiplier`), consumed by `reconcile()`. Nothing builds one from the database. This plan builds that missing piece, which also unblocks gap #13 (`reconcile`) — but `reconcile` is **not** in this plan.

---

## Global Constraints

- **Purity.** `ledger/` and `importers/` are pure: no I/O, no clock, no randomness. Database access lives in `db/`. `tests/test_purity.py` enforces it, including first-party `db` and `venues` imports.
- **`Decimal`, never `float`.** Money and quantities are `Decimal` end to end. Pin precision with `localcontext()` in any pure function doing division — `ledger/pnl.py` and `ledger/reconcile.py` both do this with `ctx.prec = 50`; follow them.
- **A row must never vanish silently.** A position the ledger cannot value must still be listed, with the reason named. This project's defining defect class is code that reports success while quietly producing nothing.
- **`NULL` is not zero.** `trade.open_quantity` is NULL on a protected (orphaned) trade — see `tests/db/test_trades.py:652`. SQL `SUM` skips NULLs, so a naive aggregate would silently omit them. Every task here must treat NULL as "unknown, report it", never as "nothing".
- **`instrument.symbol` is NOT unique** — only `natural_key` is. Two instruments can share a symbol. Any symbol-based lookup must refuse ambiguity rather than pick one.
- **Tests must be able to fail.** For each assertion ask what mutation turns it red. This project has repeatedly shipped assertions whose slack exceeded the defect they watched for.
- **Every new test is gated against a mutant before acceptance.** Report each CAUGHT or SURVIVED honestly.
- **Run the full suite yourself** with `set -a && . ./.env && set +a && uv run pytest`, and confirm the summary says neither "skipped" nor a stale count. DB tests skip silently without `TEST_PG_DSN`. It takes ~215s. Never run a mutation harness while it is running.

---

## File Structure

| File | Responsibility |
|---|---|
| `ledger/positions.py` | **pure**: trade rows → position rows. All the judgement lives here |
| `db/positions.py` | fetch open trades joined to their instrument; call the pure aggregator |
| `db/marks.py` | read the latest mark per instrument; write a manual mark |
| `cli.py` | modify: `positions` and `marks set` subcommands |
| `docs/known-gaps.md` | modify: strike #10/#11, close #12, note #13 unblocked |
| `tests/test_positions.py`, `tests/db/test_positions.py`, `tests/db/test_marks.py` | tests |

---

## Task 1: The pure position aggregator

**Files:**
- Create: `ledger/positions.py`
- Test: `tests/test_positions.py`

**Interfaces:**
- Consumes: `ledger.reconcile.Position` (existing), `ledger.types.Direction`
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class OpenPosition:
      instrument_id: UUID
      symbol: str
      quantity: Decimal
      cost_basis: Decimal        # per unit, excluding multiplier
      multiplier: Decimal
      direction: Direction | None  # None when contributing trades disagree
      is_estimated: bool
      unvaluable_reason: str | None
      trade_count: int

  @dataclass(frozen=True, slots=True)
  class TradeRow:
      instrument_id: UUID
      symbol: str
      multiplier: Decimal
      direction: Direction
      open_quantity: Decimal | None
      open_cost_basis: Decimal | None
      is_estimated: bool

  def aggregate_positions(rows: Sequence[TradeRow]) -> tuple[OpenPosition, ...]
  ```

`aggregate_positions` groups by `instrument_id` and produces one `OpenPosition` each, sorted by symbol then `instrument_id` (a stable order, since symbols are not unique).

**The four judgement calls this function exists to make.** Each is a test:

1. **Weighted-average cost basis.** `cost_basis = Σ(qty × basis) / Σ(qty)`, computed under `localcontext()` with `ctx.prec = 50` — same pin `ledger/pnl.py` uses. A plain average across trades of different sizes is wrong.
2. **`open_quantity IS NULL` means unknown.** A protected/orphaned trade carries NULL. It must not be summed as zero and must not be dropped: the position is emitted with `unvaluable_reason` set, so the row appears and says why.
3. **Spread trades cannot be valued.** `unrealized_pnl()` raises `NotImplementedError` for `Direction.SPREAD`. A position with any spread contributor gets `unvaluable_reason="spread"` and `direction=None`.
4. **Conflicting directions.** If long and short trades on one instrument coexist, no single direction applies; emit `direction=None` and `unvaluable_reason="mixed direction"`. Do not net them — netting is a modelling decision nobody has made.

`is_estimated` is `any(...)` across contributors, matching `db/trades.py:115`'s rollup.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_positions.py
from decimal import Decimal
from uuid import UUID

from ledger.positions import OpenPosition, TradeRow, aggregate_positions
from ledger.types import Direction

I1 = UUID("11111111-1111-1111-1111-111111111111")
I2 = UUID("22222222-2222-2222-2222-222222222222")


def row(instrument_id=I1, symbol="ZXCO", multiplier="1", direction=Direction.LONG,
        qty="10", basis="20", estimated=False):
    return TradeRow(
        instrument_id=instrument_id,
        symbol=symbol,
        multiplier=Decimal(multiplier),
        direction=direction,
        open_quantity=None if qty is None else Decimal(qty),
        open_cost_basis=None if basis is None else Decimal(basis),
        is_estimated=estimated,
    )


def test_a_single_open_trade_becomes_one_position():
    (p,) = aggregate_positions([row(qty="10", basis="20")])
    assert p.instrument_id == I1
    assert p.quantity == Decimal("10")
    assert p.cost_basis == Decimal("20")
    assert p.direction is Direction.LONG
    assert p.unvaluable_reason is None
    assert p.trade_count == 1


def test_cost_basis_is_weighted_by_quantity_not_a_plain_average():
    """The defect this catches: averaging 20 and 50 to 35 ignores that the
    30-unit lot dominates the 10-unit one. Correct answer is 42.5; a plain
    mean gives 35, and both are plausible-looking numbers."""
    (p,) = aggregate_positions([row(qty="10", basis="20"), row(qty="30", basis="50")])
    assert p.quantity == Decimal("40")
    assert p.cost_basis == Decimal("42.5")
    assert p.trade_count == 2


def test_a_null_open_quantity_makes_the_position_unvaluable_rather_than_vanishing():
    """A protected/orphaned trade carries NULL open_quantity. SQL SUM skips
    NULLs, so the naive aggregate silently under-reports the position and
    nothing says so. The row must appear and name the problem."""
    (p,) = aggregate_positions([row(qty="10", basis="20"), row(qty=None, basis=None)])
    assert p.unvaluable_reason is not None
    assert "unknown" in p.unvaluable_reason
    assert p.trade_count == 2


def test_a_spread_contributor_makes_the_position_unvaluable():
    (p,) = aggregate_positions([row(direction=Direction.SPREAD)])
    assert p.unvaluable_reason == "spread"
    assert p.direction is None


def test_conflicting_directions_are_not_netted():
    """Long 10 and short 4 of one instrument is not 'long 6' -- netting is a
    modelling decision nobody has made. Refuse to imply one."""
    (p,) = aggregate_positions([
        row(qty="10", direction=Direction.LONG),
        row(qty="4", direction=Direction.SHORT),
    ])
    assert p.direction is None
    assert p.unvaluable_reason == "mixed direction"


def test_estimated_rolls_up_with_any_not_all():
    (p,) = aggregate_positions([row(estimated=False), row(estimated=True)])
    assert p.is_estimated is True


def test_positions_are_grouped_by_instrument_and_stably_ordered():
    ps = aggregate_positions([
        row(instrument_id=I2, symbol="ZZZZ"),
        row(instrument_id=I1, symbol="AAAA"),
        row(instrument_id=I1, symbol="AAAA"),
    ])
    assert [p.symbol for p in ps] == ["AAAA", "ZZZZ"]
    assert [p.trade_count for p in ps] == [2, 1]


def test_no_rows_is_no_positions_not_an_error():
    assert aggregate_positions([]) == ()
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_positions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'ledger.positions'`

- [ ] **Step 3: Implement**

```python
# ledger/positions.py
"""Open trades → positions per instrument. Pure — no I/O, no clock."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from uuid import UUID

from ledger.types import Direction


@dataclass(frozen=True, slots=True)
class TradeRow:
    """One open trade, as the database hands it over."""

    instrument_id: UUID
    symbol: str
    multiplier: Decimal
    direction: Direction
    # NULL on a protected (orphaned) trade -- see db/trades.py's protect path
    # and tests/db/test_trades.py:652. NOT zero.
    open_quantity: Decimal | None
    open_cost_basis: Decimal | None
    is_estimated: bool


@dataclass(frozen=True, slots=True)
class OpenPosition:
    instrument_id: UUID
    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    multiplier: Decimal
    # None when the contributing trades do not agree on one direction.
    direction: Direction | None
    is_estimated: bool
    # None means "this position can be valued against a mark". Any other
    # value is a human-readable reason it cannot be, and the caller must
    # show the row anyway -- a position omitted from a position listing is
    # the silent-loss shape this codebase keeps rediscovering.
    unvaluable_reason: str | None
    trade_count: int


def aggregate_positions(rows: Sequence[TradeRow]) -> tuple[OpenPosition, ...]:
    grouped: dict[UUID, list[TradeRow]] = {}
    for r in rows:
        grouped.setdefault(r.instrument_id, []).append(r)

    out: list[OpenPosition] = []
    for instrument_id, group in grouped.items():
        first = group[0]
        reasons: list[str] = []

        # NULL is unknown, never zero. Checked BEFORE any arithmetic so a
        # missing quantity can never be summed away as if it were nothing.
        if any(r.open_quantity is None or r.open_cost_basis is None for r in group):
            reasons.append("open quantity unknown on at least one trade")

        directions = {r.direction for r in group}
        if Direction.SPREAD in directions:
            reasons.append("spread")
        elif len(directions) > 1:
            reasons.append("mixed direction")

        priced = [r for r in group if r.open_quantity is not None and r.open_cost_basis is not None]
        with localcontext() as ctx:
            # Same pin as ledger/pnl.py and ledger/reconcile.py: an ambient
            # low precision would silently round the weighting.
            ctx.prec = 50
            quantity = sum((r.open_quantity for r in priced), Decimal(0))
            if quantity != 0:
                weighted = sum(
                    (r.open_quantity * r.open_cost_basis for r in priced), Decimal(0)
                )
                cost_basis = weighted / quantity
            else:
                cost_basis = Decimal(0)

        out.append(
            OpenPosition(
                instrument_id=instrument_id,
                symbol=first.symbol,
                quantity=quantity,
                cost_basis=cost_basis,
                multiplier=first.multiplier,
                direction=next(iter(directions)) if len(directions) == 1
                and Direction.SPREAD not in directions else None,
                is_estimated=any(r.is_estimated for r in group),
                unvaluable_reason="; ".join(reasons) if reasons else None,
                trade_count=len(group),
            )
        )

    # Symbols are NOT unique (only instrument.natural_key is), so the
    # instrument id is the tiebreaker that makes this order deterministic.
    return tuple(sorted(out, key=lambda p: (p.symbol, str(p.instrument_id))))
```

- [ ] **Step 4: Run and confirm pass**

Run: `uv run pytest tests/test_positions.py tests/test_purity.py -v`
Expected: all pass, including purity.

- [ ] **Step 5: Mutation gate**

- Replace the weighted mean with a plain `sum(basis)/len(...)` → `test_cost_basis_is_weighted...` must FAIL.
- Drop the NULL check → `test_a_null_open_quantity...` must FAIL.
- Change `any(r.is_estimated ...)` to `all(...)` → `test_estimated_rolls_up_with_any_not_all` must FAIL.
- Remove the `Direction.SPREAD` branch → `test_a_spread_contributor...` must FAIL.
- Remove `str(p.instrument_id)` from the sort key → `test_positions_are_grouped...` may still pass; if so, say so rather than claiming a catch. Stable ordering with duplicate symbols needs its own test if you want it gated.

- [ ] **Step 6: Commit**

```bash
git add ledger/positions.py tests/test_positions.py
git commit -m "feat(ledger): pure open-position aggregation"
```

---

## Task 2: The database position query

**Files:**
- Create: `db/positions.py`
- Test: `tests/db/test_positions.py`

**Interfaces:**
- Consumes: `ledger.positions.{TradeRow, aggregate_positions, OpenPosition}`
- Produces: `async def open_positions(conn, account_id: UUID | None = None) -> tuple[OpenPosition, ...]`

Trades reach their instrument through `trade.opening_fill_id → fill.instrument_id`. That is the trade's anchor and is guaranteed present for any auto-grouped trade; a protected trade may have it NULL (`ON DELETE SET NULL`), and such a trade must be **reported, not dropped**.

- [ ] **Step 1: Write the failing tests**

```python
# tests/db/test_positions.py
from decimal import Decimal

import pytest

from db.positions import open_positions
from tests.conftest import requires_db

pytestmark = requires_db


async def test_an_open_trade_appears_as_a_position(conn, seeded_account):
    """Seed via the same path production uses -- commit fills, regroup --
    so this test breaks if the persistence of open_quantity regresses."""
    ps = await open_positions(conn, seeded_account)
    assert [p.symbol for p in ps] == ["ZXCO"]
    assert ps[0].quantity == Decimal("10")


async def test_a_closed_trade_is_not_a_position(conn, closed_trade_account):
    assert await open_positions(conn, closed_trade_account) == ()


async def test_a_trade_whose_opening_fill_was_deleted_is_reported_not_dropped(
    conn, orphaned_trade_account
):
    """A protected trade has opening_fill_id NULL, so it cannot be joined to
    an instrument. Dropping it would understate the account's exposure with
    nothing saying so."""
    ps = await open_positions(conn, orphaned_trade_account)
    assert len(ps) == 1
    assert ps[0].unvaluable_reason is not None


async def test_positions_are_scoped_to_the_account_asked_for(conn, two_accounts):
    a, b = two_accounts
    assert {p.symbol for p in await open_positions(conn, a)} != {
        p.symbol for p in await open_positions(conn, b)
    }
```

Build the fixtures from the helpers already in `tests/db/test_trades.py` — read that file and reuse its account/fill/regroup setup rather than inventing a second one. **Scope every assertion to the account under test**: the test database is shared and persistent, and `instrument` rows are global and survive their account's deletion. Never assert on an unqualified `SELECT count(*)`.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_positions.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db.positions'`. **If instead you see "skipped", `TEST_PG_DSN` is not set and you are testing nothing** — fix that before continuing.

- [ ] **Step 3: Implement**

```python
# db/positions.py
"""Open positions, read from the database and aggregated by the pure layer."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import asyncpg

from ledger.positions import OpenPosition, TradeRow, aggregate_positions
from ledger.types import Direction

# LEFT JOIN, not INNER: a protected trade has opening_fill_id NULL (the
# composite FK is ON DELETE SET NULL), and an inner join would silently drop
# it from a listing whose whole job is to show everything the account holds.
_SQL = """
    SELECT t.id,
           t.direction,
           t.open_quantity,
           t.open_cost_basis,
           t.is_estimated,
           i.id     AS instrument_id,
           i.symbol AS symbol,
           i.contract_multiplier AS multiplier
      FROM trade t
      LEFT JOIN fill f       ON f.id = t.opening_fill_id
      LEFT JOIN instrument i ON i.id = f.instrument_id
     WHERE t.status = 'open'
       AND ($1::uuid IS NULL OR t.account_id = $1)
"""

# A trade with no reachable instrument still has to appear. It is grouped
# under this sentinel so the aggregator's own "unknown" path renders it,
# rather than the query silently deciding it does not exist.
_UNKNOWN_INSTRUMENT = UUID("00000000-0000-0000-0000-000000000000")


async def open_positions(
    conn: asyncpg.Connection, account_id: UUID | None = None
) -> tuple[OpenPosition, ...]:
    records = await conn.fetch(_SQL, account_id)
    rows = [
        TradeRow(
            instrument_id=r["instrument_id"] or _UNKNOWN_INSTRUMENT,
            symbol=r["symbol"] or "(unknown instrument)",
            multiplier=r["multiplier"] if r["multiplier"] is not None else Decimal(1),
            direction=Direction(r["direction"]),
            open_quantity=r["open_quantity"],
            open_cost_basis=r["open_cost_basis"],
            is_estimated=r["is_estimated"],
        )
        for r in records
    ]
    return aggregate_positions(rows)
```

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_positions.py -v`

- [ ] **Step 5: Mutation gate**

- Change `LEFT JOIN` to `INNER JOIN` → `test_a_trade_whose_opening_fill_was_deleted...` must FAIL.
- Remove `t.status = 'open'` → `test_a_closed_trade_is_not_a_position` must FAIL.
- Remove the account predicate → `test_positions_are_scoped_to_the_account_asked_for` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add db/positions.py tests/db/test_positions.py
git commit -m "feat(db): open_positions query feeding the pure aggregator"
```

---

## Task 3: Marks — read and write

**Files:**
- Create: `db/marks.py`
- Test: `tests/db/test_marks.py`

**Interfaces:**
- Produces:
  ```python
  async def set_mark(conn, instrument_id: UUID, price: Decimal, as_of: datetime,
                     source: str = "manual") -> None
  async def latest_marks(conn, instrument_ids: Sequence[UUID]) -> dict[UUID, tuple[Decimal, datetime]]
  async def resolve_instrument_by_symbol(conn, symbol: str) -> UUID
  ```

**`resolve_instrument_by_symbol` must refuse ambiguity.** `instrument.symbol` is not unique — only `natural_key` is. Two instruments can share a symbol (the same ticker quoted in two currencies, for instance). Picking the first match would silently mark the wrong instrument, which then silently produces a wrong unrealized figure. Raise, naming every candidate's `natural_key` so the user can disambiguate.

`latest_marks` returns the most recent mark per instrument **and its timestamp**, so the caller can show the mark's age. A stale mark rendered indistinguishably from a fresh one is a quiet way to mislead.

- [ ] **Step 1: Write the failing tests**

```python
# tests/db/test_marks.py
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from db.marks import latest_marks, resolve_instrument_by_symbol, set_mark
from tests.conftest import requires_db

pytestmark = requires_db


async def test_a_mark_round_trips(conn, an_instrument):
    when = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    await set_mark(conn, an_instrument, Decimal("24.50"), when)
    got = await latest_marks(conn, [an_instrument])
    assert got[an_instrument] == (Decimal("24.50"), when)


async def test_the_latest_mark_wins_not_the_last_written(conn, an_instrument):
    """Marks are keyed (instrument_id, as_of), so a backfilled OLDER mark can
    be written after a newer one. Ordering must be by as_of, not by
    insertion."""
    newer = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    older = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    await set_mark(conn, an_instrument, Decimal("30"), newer)
    await set_mark(conn, an_instrument, Decimal("10"), older)
    assert (await latest_marks(conn, [an_instrument]))[an_instrument][0] == Decimal("30")


async def test_rewriting_the_same_timestamp_updates_rather_than_failing(conn, an_instrument):
    when = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    await set_mark(conn, an_instrument, Decimal("24.50"), when)
    await set_mark(conn, an_instrument, Decimal("25.00"), when)
    assert (await latest_marks(conn, [an_instrument]))[an_instrument][0] == Decimal("25.00")


async def test_an_unmarked_instrument_is_absent_not_zero(conn, an_instrument):
    """Absent must be distinguishable from a genuine zero price -- the mark
    table permits price = 0."""
    assert await latest_marks(conn, [an_instrument]) == {}


async def test_an_ambiguous_symbol_is_refused_naming_the_candidates(conn, two_same_symbol):
    """symbol is not unique; only natural_key is. Marking 'the first one'
    would silently value the wrong instrument."""
    with pytest.raises(ValueError) as exc:
        await resolve_instrument_by_symbol(conn, "DUPE")
    assert "natural_key" in str(exc.value) or "natural key" in str(exc.value).lower()


async def test_an_unknown_symbol_is_refused(conn):
    with pytest.raises(ValueError):
        await resolve_instrument_by_symbol(conn, "NOSUCHSYMBOL")
```

Build `an_instrument` and `two_same_symbol` with `db.instruments.upsert_instrument`, giving the two duplicates different `natural_key`s (e.g. differing `quote_currency`) but the same `symbol`.

- [ ] **Step 2: Run and watch them fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_marks.py -v`
Expected: FAIL — no module `db.marks`. Confirm it says FAIL, not "skipped".

- [ ] **Step 3: Implement**

```python
# db/marks.py
"""Price marks. The only MarkSource is manual entry -- A does not fetch prices."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import UUID

import asyncpg


async def set_mark(
    conn: asyncpg.Connection,
    instrument_id: UUID,
    price: Decimal,
    as_of: datetime,
    source: str = "manual",
) -> None:
    if as_of.tzinfo is None:
        raise ValueError("mark as_of must be timezone-aware")
    await conn.execute(
        """
        INSERT INTO mark (instrument_id, as_of, price, source)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (instrument_id, as_of)
        DO UPDATE SET price = EXCLUDED.price, source = EXCLUDED.source
        """,
        instrument_id,
        as_of,
        price,
        source,
    )


async def latest_marks(
    conn: asyncpg.Connection, instrument_ids: Sequence[UUID]
) -> dict[UUID, tuple[Decimal, datetime]]:
    """Most recent mark per instrument, with its timestamp.

    The timestamp is returned, not discarded, so a caller can show a mark's
    age. A month-old mark rendered identically to a fresh one is a quiet way
    to mislead. An instrument with no mark is ABSENT from the mapping --
    never present with a zero, since `mark_price_chk` permits a genuine 0.
    """
    if not instrument_ids:
        return {}
    rows = await conn.fetch(
        """
        SELECT DISTINCT ON (instrument_id) instrument_id, price, as_of
          FROM mark
         WHERE instrument_id = ANY($1::uuid[])
         ORDER BY instrument_id, as_of DESC
        """,
        list(instrument_ids),
    )
    return {r["instrument_id"]: (r["price"], r["as_of"]) for r in rows}


async def resolve_instrument_by_symbol(conn: asyncpg.Connection, symbol: str) -> UUID:
    """Symbol → instrument id, refusing ambiguity.

    `instrument.symbol` is NOT unique -- only `natural_key` is. Two
    instruments can legitimately share a symbol. Returning "the first match"
    would mark the wrong instrument and produce a wrong unrealized figure
    with nothing indicating it, so an ambiguous symbol raises and names every
    candidate.
    """
    rows = await conn.fetch(
        "SELECT id, natural_key FROM instrument WHERE upper(symbol) = upper($1) ORDER BY natural_key",
        symbol,
    )
    if not rows:
        raise ValueError(f"no instrument with symbol {symbol!r}")
    if len(rows) > 1:
        keys = ", ".join(r["natural_key"] for r in rows)
        raise ValueError(
            f"symbol {symbol!r} matches {len(rows)} instruments; "
            f"disambiguate with --natural-key (candidates: {keys})"
        )
    return rows[0]["id"]
```

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_marks.py -v`

- [ ] **Step 5: Mutation gate**

- Change `ORDER BY instrument_id, as_of DESC` to `as_of ASC` → `test_the_latest_mark_wins...` must FAIL.
- Make `resolve_instrument_by_symbol` return `rows[0]["id"]` unconditionally → `test_an_ambiguous_symbol_is_refused...` must FAIL.
- Make `latest_marks` return `Decimal(0)` for absent instruments → `test_an_unmarked_instrument_is_absent_not_zero` must FAIL.

- [ ] **Step 6: Commit**

```bash
git add db/marks.py tests/db/test_marks.py
git commit -m "feat(db): manual price marks with ambiguity-refusing symbol lookup"
```

---

## Task 4: `deadband marks set`

**Files:**
- Modify: `cli.py`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `db.marks.{set_mark, resolve_instrument_by_symbol}`
- Produces: `cmd_marks_set(args)`

Follow `cmd_trades`'s shape exactly, including its pool-close comment: `pool.close()` runs **after** the `async with pool.acquire()` block exits, never inside it, or `close()` deadlocks waiting for a release that never comes.

Arguments: `--symbol` or `--natural-key` (exactly one required), `--price` (required), `--as-of` (ISO-8601, optional). **When `--as-of` is omitted the CLI supplies `datetime.now(UTC)`** — the clock lives here, in the I/O layer, never in `db/marks.py` or anything under `ledger/`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_marks_set_records_a_price(conn, an_instrument_named_zxco, capsys):
    rc = await cli.cmd_marks_set(_args(symbol="ZXCO", natural_key=None,
                                       price="24.50", as_of="2026-08-08T12:00:00+00:00"))
    assert rc == 0
    assert (await latest_marks(conn, [an_instrument_named_zxco]))[
        an_instrument_named_zxco][0] == Decimal("24.50")


async def test_marks_set_refuses_an_ambiguous_symbol_without_writing(conn, two_same_symbol, capsys):
    """The refusal must happen before any write -- a partially applied mark
    is worse than none."""
    rc = await cli.cmd_marks_set(_args(symbol="DUPE", natural_key=None, price="1", as_of=None))
    assert rc == 2
    assert "natural-key" in capsys.readouterr().err
    for iid in two_same_symbol:
        assert await latest_marks(conn, [iid]) == {}


async def test_marks_set_requires_exactly_one_of_symbol_or_natural_key():
    with pytest.raises(SystemExit):
        cli.main_with_argv(["marks", "set", "--price", "1"])
```

Adapt `_args` and the argv helper to whatever `tests/db/test_cli.py` already uses — read it first rather than inventing a new harness.

- [ ] **Step 2: Run and watch it fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -k marks -v`
Expected: FAIL — `cli` has no attribute `cmd_marks_set`.

- [ ] **Step 3: Implement**

Add to `main()`:

```python
    p_marks = sub.add_parser("marks", help="manual price marks")
    marks_sub = p_marks.add_subparsers(dest="marks_command", required=True)
    p_marks_set = marks_sub.add_parser("set", help="record a price mark for an instrument")
    group = p_marks_set.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbol", help="instrument symbol; refused if it is ambiguous")
    group.add_argument("--natural-key", help="exact instrument natural key")
    p_marks_set.add_argument("--price", required=True)
    p_marks_set.add_argument(
        "--as-of", default=None, help="ISO-8601 timestamp; defaults to now (UTC)"
    )
    p_marks_set.set_defaults(fn=cmd_marks_set)
```

`cmd_marks_set` resolves the instrument (letting `ValueError` become a stderr message and `return 2`), parses `--price` with `Decimal`, and calls `set_mark`. Resolve **before** opening a write, so an ambiguous symbol never half-applies.

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -k marks -v`

- [ ] **Step 5: Mutation gate**

- Make the ambiguous-symbol path write anyway → `test_marks_set_refuses_an_ambiguous_symbol_without_writing` must FAIL.
- Change the mutually-exclusive group to `required=False` → the third test must FAIL.

- [ ] **Step 6: Commit**

```bash
git add cli.py tests/db/test_cli.py
git commit -m "feat(cli): deadband marks set"
```

---

## Task 5: `deadband positions`

**Files:**
- Modify: `cli.py`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `db.positions.open_positions`, `db.marks.latest_marks`, `ledger.pnl.unrealized_pnl`
- Produces: `cmd_positions(args)`

`--account <uuid>` optional; omitted means every account aggregated, mirroring `cmd_trades`.

**Three rendering rules, each a test:**

1. **An unvaluable position still prints**, with its reason in the unrealized column. Never filtered out.
2. **An unmarked position prints `--`**, distinguishable from a genuine zero unrealized.
3. **A mark's age is shown.** A month-old mark must not render identically to one from a minute ago.

`unrealized_pnl()` raises `NotImplementedError` for `Direction.SPREAD`, so **only call it when `unvaluable_reason is None` and a mark exists**. Guard by the reason, not by catching the exception — a caught exception would also swallow a future genuine bug.

- [ ] **Step 1: Write the failing tests**

```python
async def test_positions_shows_unrealized_where_a_mark_exists(conn, marked_position_account, capsys):
    rc = await cli.cmd_positions(_args(account=str(marked_position_account)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ZXCO" in out
    assert "63.00" in out   # (24.50 - 18.20) * 10 * 1


async def test_an_unmarked_position_shows_a_placeholder_not_a_zero(conn, unmarked_account, capsys):
    await cli.cmd_positions(_args(account=str(unmarked_account)))
    out = capsys.readouterr().out
    assert "--" in out
    assert "0.00" not in out


async def test_an_unvaluable_position_is_listed_with_its_reason(conn, spread_account, capsys):
    """A position omitted from a position listing is the silent-loss shape
    this codebase keeps rediscovering."""
    await cli.cmd_positions(_args(account=str(spread_account)))
    out = capsys.readouterr().out
    assert "spread" in out


async def test_the_marks_age_is_shown(conn, stale_mark_account, capsys):
    await cli.cmd_positions(_args(account=str(stale_mark_account)))
    out = capsys.readouterr().out
    assert "2026-07-01" in out or "d ago" in out
```

- [ ] **Step 2: Run and watch it fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -k positions -v`
Expected: FAIL — no `cmd_positions`.

- [ ] **Step 3: Implement**

```python
async def cmd_positions(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            positions = await open_positions(
                conn, UUID(args.account) if args.account else None
            )
            marks = await latest_marks(conn, [p.instrument_id for p in positions])
    finally:
        # Same hazard as cmd_import/cmd_trades: close() after the acquire
        # block exits, never inside it, or it deadlocks.
        await pool.close()

    for p in positions:
        mark = marks.get(p.instrument_id)
        if p.unvaluable_reason is not None:
            unreal, mark_col = f"n/a ({p.unvaluable_reason})", "--"
        elif mark is None:
            unreal, mark_col = "--", "--"
        else:
            price, as_of = mark
            unreal = f"{unrealized_pnl(p.quantity, p.cost_basis, price, p.multiplier, p.direction):>12}"
            mark_col = f"{price} @{as_of:%Y-%m-%d}"
        estimated = " ~" if p.is_estimated else "  "
        print(f"{p.symbol:<10}{estimated}{p.quantity:>14} {p.cost_basis:>12} {mark_col:>22} {unreal}")
    if not positions:
        print("no open positions")
    return 0
```

Register it in `main()`:

```python
    p_positions = sub.add_parser("positions", help="open positions, with unrealized P&L where marked")
    p_positions.add_argument("--account")
    p_positions.set_defaults(fn=cmd_positions)
```

- [ ] **Step 4: Run and confirm pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -k positions -v`

- [ ] **Step 5: Mutation gate**

- Filter out positions with `unvaluable_reason` → `test_an_unvaluable_position_is_listed...` must FAIL.
- Render an absent mark as `0.00` → `test_an_unmarked_position_shows_a_placeholder...` must FAIL.
- Drop the `@{as_of}` from the mark column → `test_the_marks_age_is_shown` must FAIL.

- [ ] **Step 6: Full suite, then commit**

```bash
set -a && . ./.env && set +a && uv run pytest -q
```
Confirm the summary says neither "skipped" nor a stale count.

```bash
git add cli.py tests/db/test_cli.py
git commit -m "feat(cli): deadband positions with unrealized P&L where marked"
```

---

## Task 6: Correct the gap list

**Files:**
- Modify: `docs/known-gaps.md`

- [ ] **Step 1: Strike gaps #10 and #11 as already closed.** Mark both `~~struck~~ — CLOSED in A-2 part 1, recorded 2026-08-08`. Say plainly that they were done and never removed from the list, and name the evidence: `db/trades.py:115` for the `any()` rollup, `db/trades.py:128,145-147` for the persistence, and the three tests in `tests/db/test_trades.py`. A gap list that carries closed items is worse than no list — it costs the next reader a re-investigation, which is exactly what it cost this one.

- [ ] **Step 2: Close gap #12** with what shipped: `positions` exists, aggregates per instrument, values against manual marks, and lists unvaluable positions rather than hiding them.

- [ ] **Step 3: Record what is still open.** Gap #13 (`reconcile`) is now **unblocked** — it needs the `account_snapshot` write path, but the position query and `ledger/reconcile.py` it consumes both exist. Add a new gap: **unrealized P&L is only as fresh as the last manual mark**, there is no price feed by design (spec §3), and `positions` shows each mark's date so staleness is visible rather than assumed.

- [ ] **Step 4: Commit**

```bash
git add docs/known-gaps.md
git commit -m "docs: close gaps 10-12, record what positions leaves open"
```

---

## Self-Review

**Spec coverage.** Gap #12 (`positions`) → Tasks 1, 2, 5. Spec §3 "position views per account and aggregated" → Task 5's optional `--account`. Spec §3 "unrealized P&L where a mark exists" → Tasks 3, 4, 5. Spec §3 "MarkSource with exactly one implementation (manual)" → Task 3. Gaps #10/#11 → already closed, corrected in Task 6.

**Placeholders.** None: every code step carries real code, every test step real tests.

**Type consistency.** `TradeRow` and `OpenPosition` are defined in Task 1 and consumed with those exact field names in Tasks 2 and 5. `open_positions(conn, account_id)` (Task 2) is called with that signature in Task 5. `latest_marks` returns `dict[UUID, tuple[Decimal, datetime]]` in Task 3 and is unpacked as `price, as_of` in Task 5. `unrealized_pnl(open_quantity, open_cost_basis, mark_price, multiplier, direction)` matches `ledger/pnl.py:179-185`.

**Known soft spot.** Task 5's assertions match on formatted output, which couples them to the exact rendering. If an implementer changes the column layout the tests break for a cosmetic reason. That is deliberate — the alternative is asserting on a data structure the user never sees, which would let a rendering bug ship. Keep the assertions on the *substance* (the number, the `--`, the reason text) and never on padding widths.
