# Deadband A-1 — Ledger Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A headless, tested trade ledger — pure domain logic, a Postgres schema, and CSV importers for Coinbase and Fidelity — that can load real trading history and answer "what do I hold, what did I make, and where does it disagree with my broker."

**Architecture:** Three layers with a hard boundary between them. `ledger/` holds pure domain logic (grouping, P&L, corporate actions, reconciliation) with zero I/O — no database, no network, no clock. `importers/` holds pure mappers from venue CSV to canonical rows. `db/` is the only layer that touches Postgres. A CLI ties them together so the core is usable and provable before any web layer exists (A-2).

**Tech Stack:** Python 3.11+, uv, asyncpg, pytest, pytest-asyncio, hypothesis, ruff. Postgres 16.

## Global Constraints

Every task's requirements implicitly include these.

- **Python 3.11+.** Environment managed with `uv`, never pip or venv directly.
- **`Decimal` for every monetary and quantity value. Never `float`.** Floats silently
  lose cents; this is a financial ledger.
- **All timestamps are timezone-aware UTC** (`datetime.timezone.utc`). Naive datetimes are
  a bug.
- **`ledger/` and `importers/` are pure.** No `asyncpg`, no `open()`, no `requests`, no
  `datetime.now()`. Anything time-dependent takes the time as a parameter. A test in
  `tests/test_purity.py` enforces this by inspecting imports.
- **Fill quantities are always positive.** Direction lives in `side`. Signedness is
  derived, never stored.
- **Database tests are gated on `TEST_PG_DSN`** and skip when it is unset, matching the
  sibling QuantConnect project's convention.
- **Repository is public.** No credentials, no real account numbers, no real venue
  exports. Test fixtures are synthetic. See `.claude/skills/public-repo-hygiene/`.
- Lint with `ruff check .` and `ruff format --check .`.

---

## File Structure

```
pyproject.toml                  uv project, deps, pytest + ruff config
.env.example                    PG_DSN / TEST_PG_DSN template (no real values)

ledger/                         PURE — no I/O anywhere
  types.py                      enums, Fill, Instrument, natural keys
  grouping.py                   fills → trade groups (allocation-based)
  pnl.py                        running-average cost, realized P&L
  corporate.py                  split / merger / spinoff / symbol-change adjustment
  reconcile.py                  computed vs. statement drift

importers/                      PURE — mappers only, never touch the DB
  base.py                       CanonicalFill, CanonicalCash, ImportBatch, content hash
  coinbase.py                   Coinbase transaction CSV
  fidelity.py                   Fidelity account-activity CSV
  registry.py                   name → importer lookup

db/
  schema.sql                    tables, constraints, indexes
  migrations/                   0001_*.sql onward
  pool.py                       asyncpg pool lifecycle
  migrate.py                    idempotent migration runner
  accounts.py                   account + funded_account_rule queries
  instruments.py                instrument upsert by natural key
  fills.py                      fill insert (idempotent), fetch
  trades.py                     trade + trade_fill persistence, regroup pipeline
  cash.py                       cash_movement queries
  marks.py                      mark queries
  snapshots.py                  account_snapshot queries

cli.py                          import / regroup / positions / reconcile

tests/
  conftest.py                   DB gating, synthetic factories
  test_purity.py                enforces the purity constraint
  test_types.py  test_grouping.py  test_grouping_properties.py
  test_pnl.py  test_corporate.py  test_reconcile.py
  test_importer_base.py  test_coinbase.py  test_fidelity.py
  db/test_migrations.py  db/test_fills.py  db/test_trades.py
  fixtures/coinbase/*.csv  fixtures/fidelity/*.csv    (synthetic)
```

Files split by responsibility, not by layer-within-entity. Each domain module owns one
computation and is independently testable.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `.env.example`, `ledger/__init__.py`, `importers/__init__.py`, `db/__init__.py`, `tests/__init__.py`
- Create: `tests/test_purity.py`

**Interfaces:**
- Consumes: nothing
- Produces: a working `uv run pytest`; the package layout every later task imports from

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "deadband"
version = "0.1.0"
description = "Trade and position ledger"
requires-python = ">=3.11"
dependencies = [
    "asyncpg>=0.30",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "hypothesis>=6.100",
    "ruff>=0.6",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["ledger", "importers", "db"]
```

- [ ] **Step 2: Write `.env.example`**

```bash
# Copy to .env and fill in. .env is gitignored — never commit real values.
PG_DSN=postgresql://deadband:CHANGEME@127.0.0.1:5432/deadband
TEST_PG_DSN=postgresql://deadband:CHANGEME@127.0.0.1:5432/deadband_test
```

- [ ] **Step 3: Create empty package files**

```bash
mkdir -p ledger importers db/migrations tests/fixtures
touch ledger/__init__.py importers/__init__.py db/__init__.py tests/__init__.py
```

- [ ] **Step 4: Write the purity test**

This is a real constraint with a real test, not a comment nobody reads.

```python
# tests/test_purity.py
import ast
import pathlib

FORBIDDEN = {"asyncpg", "psycopg", "requests", "httpx", "aiohttp", "socket", "sqlite3"}
PURE_PACKAGES = ["ledger", "importers"]


def _imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_pure_packages_have_no_io_imports():
    offenders = []
    for pkg in PURE_PACKAGES:
        for path in pathlib.Path(pkg).rglob("*.py"):
            bad = _imports(path) & FORBIDDEN
            if bad:
                offenders.append(f"{path}: {sorted(bad)}")
    assert not offenders, "I/O imports in pure package:\n" + "\n".join(offenders)


def test_pure_packages_do_not_read_the_clock():
    offenders = []
    for pkg in PURE_PACKAGES:
        for path in pathlib.Path(pkg).rglob("*.py"):
            src = path.read_text()
            for needle in ("datetime.now(", "datetime.utcnow(", "time.time("):
                if needle in src:
                    offenders.append(f"{path}: {needle}")
    assert not offenders, (
        "Pure code must take time as a parameter:\n" + "\n".join(offenders)
    )
```

- [ ] **Step 5: Run it**

Run: `uv run pytest tests/test_purity.py -v`
Expected: PASS (both tests, trivially — the packages are empty)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .env.example ledger importers db tests
git commit -m "chore: project scaffold with enforced purity boundary"
```

---

### Task 2: Domain types

**Files:**
- Create: `ledger/types.py`
- Test: `tests/test_types.py`

**Interfaces:**
- Consumes: nothing
- Produces: `Side`, `AssetClass`, `FillSource`, `TradeStatus`, `TradeIntent`, `Direction`,
  `GroupingMode`, `Instrument`, `Fill`, `Fill.signed_quantity`,
  `instrument_natural_key(instrument) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_types.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from ledger.types import (
    AssetClass,
    Fill,
    FillSource,
    Instrument,
    Side,
    instrument_natural_key,
)

ACC = UUID("00000000-0000-0000-0000-0000000000a1")


def make_fill(side: Side, qty: str, price: str) -> Fill:
    return Fill(
        id=uuid4(),
        account_id=ACC,
        instrument_id=UUID("00000000-0000-0000-0000-0000000000b1"),
        executed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=None,
        is_estimated=False,
    )


def test_buy_has_positive_signed_quantity():
    assert make_fill(Side.BUY, "1.5", "100").signed_quantity == Decimal("1.5")


def test_sell_has_negative_signed_quantity():
    assert make_fill(Side.SELL, "1.5", "100").signed_quantity == Decimal("-1.5")


def test_negative_quantity_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        make_fill(Side.BUY, "-1", "100")


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        Fill(
            id=None,
            account_id=ACC,
            instrument_id=ACC,
            executed_at=datetime(2026, 8, 1, 12, 0),  # naive
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("1"),
            fee=Decimal("0"),
            fee_currency="USD",
            source=FillSource.MANUAL,
            venue_fill_id=None,
            is_estimated=False,
        )


def test_equity_natural_key():
    inst = Instrument(
        id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"
    )
    assert instrument_natural_key(inst) == "equity:SPY:USD"


def test_option_natural_key_includes_all_contract_terms():
    inst = Instrument(
        id=None,
        asset_class=AssetClass.OPTION,
        symbol="SPY 26SEP19 500 C",
        quote_currency="USD",
        underlying="SPY",
        strike=Decimal("500"),
        expiry=datetime(2026, 9, 19, tzinfo=UTC).date(),
        option_right="call",
        contract_multiplier=Decimal("100"),
    )
    assert instrument_natural_key(inst) == "option:SPY:2026-09-19:500:call:USD"


def test_option_natural_key_is_stable_across_strike_formatting():
    """500 and 500.00 are the same strike and must not create two instruments."""
    base = dict(
        id=None,
        asset_class=AssetClass.OPTION,
        symbol="SPY 26SEP19 500 C",
        quote_currency="USD",
        underlying="SPY",
        expiry=datetime(2026, 9, 19, tzinfo=UTC).date(),
        option_right="call",
        contract_multiplier=Decimal("100"),
    )
    a = instrument_natural_key(Instrument(**base, strike=Decimal("500")))
    b = instrument_natural_key(Instrument(**base, strike=Decimal("500.00")))
    assert a == b


def test_onchain_natural_key_lowercases_address():
    inst = Instrument(
        id=None,
        asset_class=AssetClass.CRYPTO_SPOT,
        symbol="WETH",
        quote_currency="USD",
        chain="ethereum",
        contract_address="0xC02AAA39B223FE8D0A0E5C4F27EAD9083C756CC2",
    )
    key = instrument_natural_key(inst)
    assert key == "crypto_spot:ethereum:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2:USD"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_types.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ledger.types'`

- [ ] **Step 3: Write the implementation**

```python
# ledger/types.py
"""Domain types. Pure — no I/O, no clock."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(StrEnum):
    CRYPTO_SPOT = "crypto_spot"
    CRYPTO_PERP = "crypto_perp"
    EQUITY = "equity"
    OPTION = "option"
    FUTURE = "future"


class FillSource(StrEnum):
    MANUAL = "manual"
    CSV = "csv"
    API = "api"
    OPENING_BALANCE = "opening_balance"


class TradeStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class TradeIntent(StrEnum):
    TRADE = "trade"
    INVESTMENT = "investment"
    UNASSIGNED = "unassigned"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    SPREAD = "spread"


class GroupingMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Instrument:
    id: UUID | None
    asset_class: AssetClass
    symbol: str
    quote_currency: str
    underlying: str | None = None
    strike: Decimal | None = None
    expiry: date | None = None
    option_right: str | None = None          # "call" | "put"
    root: str | None = None
    contract_multiplier: Decimal = Decimal(1)
    chain: str | None = None
    contract_address: str | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    id: UUID | None
    account_id: UUID
    instrument_id: UUID
    executed_at: datetime
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    source: FillSource
    venue_fill_id: str | None
    is_estimated: bool
    venue_order_id: str | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"fill quantity must be positive, got {self.quantity}")
        if self.price < 0:
            raise ValueError(f"fill price must not be negative, got {self.price}")
        if self.executed_at.tzinfo is None:
            raise ValueError("executed_at must be timezone-aware (UTC)")

    @property
    def signed_quantity(self) -> Decimal:
        """Position delta. Direction lives here, never in the stored quantity."""
        return self.quantity if self.side is Side.BUY else -self.quantity


def _normalize_decimal(value: Decimal) -> str:
    """500 and 500.00 must produce the same key, or one contract becomes two."""
    normalized = value.normalize()
    # normalize() renders large integers in scientific notation; undo that.
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return str(normalized)


def instrument_natural_key(instrument: Instrument) -> str:
    """Stable identity for an instrument. The uniqueness constraint in the database
    is built on this, so two spellings of the same contract must collapse to one key."""
    cls = instrument.asset_class
    quote = instrument.quote_currency.upper()

    if cls is AssetClass.OPTION:
        if not (
            instrument.underlying
            and instrument.strike is not None
            and instrument.expiry
            and instrument.option_right
        ):
            raise ValueError("option instruments require underlying, strike, expiry, right")
        return ":".join(
            [
                cls.value,
                instrument.underlying.upper(),
                instrument.expiry.isoformat(),
                _normalize_decimal(instrument.strike),
                instrument.option_right.lower(),
                quote,
            ]
        )

    if cls is AssetClass.FUTURE:
        if not (instrument.root and instrument.expiry):
            raise ValueError("future instruments require root and expiry")
        return ":".join(
            [cls.value, instrument.root.upper(), instrument.expiry.isoformat(), quote]
        )

    if instrument.contract_address:
        chain = (instrument.chain or "unknown").lower()
        return ":".join([cls.value, chain, instrument.contract_address.lower(), quote])

    return ":".join([cls.value, instrument.symbol.upper(), quote])
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_types.py tests/test_purity.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add ledger/types.py tests/test_types.py
git commit -m "feat: domain types with stable instrument natural keys"
```

---

### Task 3: Fill grouping

The single most important algorithm in the system. An error here silently corrupts every
metric that will ever be computed.

**Files:**
- Create: `ledger/grouping.py`
- Test: `tests/test_grouping.py`

**Interfaces:**
- Consumes: `ledger.types.Fill`, `Direction`, `TradeStatus`
- Produces: `FillAllocation(fill_id, quantity)`, `TradeGroup(allocations, direction, status,
  opened_at, closed_at, account_id, instrument_ids)`, `group_fills(fills) -> list[TradeGroup]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_grouping.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from ledger.grouping import group_fills
from ledger.types import Direction, Fill, FillSource, Side, TradeStatus

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
BTC = UUID("00000000-0000-0000-0000-0000000000b1")
ETH = UUID("00000000-0000-0000-0000-0000000000b2")
T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def fill(side, qty, price, minutes=0, instrument=BTC, account=ACC) -> Fill:
    return Fill(
        id=uuid4(),
        account_id=account,
        instrument_id=instrument,
        executed_at=T0 + timedelta(minutes=minutes),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=None,
        is_estimated=False,
    )


def total(group) -> Decimal:
    return sum((a.quantity for a in group.allocations), Decimal(0))


def test_simple_round_trip_is_one_closed_trade():
    fills = [fill(Side.BUY, "1", "100", 0), fill(Side.SELL, "1", "120", 10)]
    groups = group_fills(fills)
    assert len(groups) == 1
    g = groups[0]
    assert g.status is TradeStatus.CLOSED
    assert g.direction is Direction.LONG
    assert g.opened_at == T0
    assert g.closed_at == T0 + timedelta(minutes=10)
    assert total(g) == Decimal("2")


def test_scale_in_and_partial_exit_stays_one_open_trade():
    fills = [
        fill(Side.BUY, "0.5", "61200", 0),
        fill(Side.BUY, "0.5", "60800", 10),
        fill(Side.BUY, "1.0", "60100", 20),
        fill(Side.SELL, "1.0", "63400", 30),
    ]
    groups = group_fills(fills)
    assert len(groups) == 1
    assert groups[0].status is TradeStatus.OPEN
    assert groups[0].closed_at is None
    assert len(groups[0].allocations) == 4


def test_flat_then_reopen_is_two_trades():
    fills = [
        fill(Side.BUY, "1", "100", 0),
        fill(Side.SELL, "1", "110", 10),
        fill(Side.BUY, "1", "105", 20),
        fill(Side.SELL, "1", "115", 30),
    ]
    groups = group_fills(fills)
    assert len(groups) == 2
    assert all(g.status is TradeStatus.CLOSED for g in groups)


def test_short_trade_is_detected():
    fills = [fill(Side.SELL, "2", "100", 0), fill(Side.BUY, "2", "90", 10)]
    groups = group_fills(fills)
    assert len(groups) == 1
    assert groups[0].direction is Direction.SHORT
    assert groups[0].status is TradeStatus.CLOSED


def test_fill_crossing_zero_splits_across_two_trades():
    """Long 2, sell 3 => closes the long with 2 and opens a short with 1."""
    crossing = fill(Side.SELL, "3", "110", 10)
    fills = [fill(Side.BUY, "2", "100", 0), crossing]
    groups = group_fills(fills)

    assert len(groups) == 2
    closed, opened = groups[0], groups[1]

    assert closed.direction is Direction.LONG
    assert closed.status is TradeStatus.CLOSED
    assert {a.fill_id for a in closed.allocations} == {fills[0].id, crossing.id}
    assert next(a.quantity for a in closed.allocations if a.fill_id == crossing.id) == Decimal("2")

    assert opened.direction is Direction.SHORT
    assert opened.status is TradeStatus.OPEN
    assert opened.allocations == tuple(
        a for a in opened.allocations if a.fill_id == crossing.id
    )
    assert total(opened) == Decimal("1")


def test_allocations_of_a_fill_always_sum_to_its_quantity():
    crossing = fill(Side.SELL, "3", "110", 10)
    fills = [fill(Side.BUY, "2", "100", 0), crossing]
    groups = group_fills(fills)
    allocated = sum(
        (a.quantity for g in groups for a in g.allocations if a.fill_id == crossing.id),
        Decimal(0),
    )
    assert allocated == crossing.quantity


def test_different_instruments_do_not_mix():
    fills = [
        fill(Side.BUY, "1", "100", 0, instrument=BTC),
        fill(Side.BUY, "1", "50", 5, instrument=ETH),
        fill(Side.SELL, "1", "110", 10, instrument=BTC),
    ]
    groups = group_fills(fills)
    assert len(groups) == 2
    btc = [g for g in groups if g.instrument_ids == (BTC,)][0]
    eth = [g for g in groups if g.instrument_ids == (ETH,)][0]
    assert btc.status is TradeStatus.CLOSED
    assert eth.status is TradeStatus.OPEN


def test_same_instrument_in_different_accounts_does_not_mix():
    other = UUID("00000000-0000-0000-0000-0000000000a2")
    fills = [
        fill(Side.BUY, "1", "100", 0, account=ACC),
        fill(Side.SELL, "1", "110", 10, account=other),
    ]
    groups = group_fills(fills)
    assert len(groups) == 2
    assert all(g.status is TradeStatus.OPEN for g in groups)


def test_input_order_does_not_matter():
    a = fill(Side.BUY, "1", "100", 0)
    b = fill(Side.SELL, "1", "120", 10)
    assert group_fills([a, b]) == group_fills([b, a])


def test_empty_input_returns_empty_list():
    assert group_fills([]) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_grouping.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ledger.grouping'`

- [ ] **Step 3: Write the implementation**

```python
# ledger/grouping.py
"""Group fills into trades. Pure — no I/O, no clock.

A trade opens when position moves from flat to non-flat and closes when it returns
to flat. A fill that crosses zero is split by quantity across two trades, which is
why association is an allocation rather than a foreign key.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from ledger.types import Direction, Fill, Side, TradeStatus


@dataclass(frozen=True, slots=True)
class FillAllocation:
    fill_id: UUID
    quantity: Decimal          # always positive; the portion of the fill in this trade


@dataclass(frozen=True, slots=True)
class TradeGroup:
    account_id: UUID
    instrument_ids: tuple[UUID, ...]
    allocations: tuple[FillAllocation, ...]
    direction: Direction
    status: TradeStatus
    opened_at: datetime
    closed_at: datetime | None


def _sort_key(f: Fill) -> tuple[datetime, str]:
    # Ties broken by id so grouping is deterministic for simultaneous fills.
    return (f.executed_at, str(f.id))


def group_fills(fills: list[Fill]) -> list[TradeGroup]:
    """Group fills into trades by walking signed position per (account, instrument)."""
    buckets: dict[tuple[UUID, UUID], list[Fill]] = defaultdict(list)
    for f in fills:
        buckets[(f.account_id, f.instrument_id)].append(f)

    groups: list[TradeGroup] = []

    for (account_id, instrument_id) in sorted(buckets, key=lambda k: (str(k[0]), str(k[1]))):
        bucket = sorted(buckets[(account_id, instrument_id)], key=_sort_key)

        position = Decimal(0)
        allocations: list[FillAllocation] = []
        opened_at: datetime | None = None
        direction: Direction | None = None

        def flush(closed_at: datetime | None) -> None:
            nonlocal allocations, opened_at, direction
            if not allocations:
                return
            groups.append(
                TradeGroup(
                    account_id=account_id,
                    instrument_ids=(instrument_id,),
                    allocations=tuple(allocations),
                    direction=direction,                       # type: ignore[arg-type]
                    status=TradeStatus.CLOSED if closed_at else TradeStatus.OPEN,
                    opened_at=opened_at,                       # type: ignore[arg-type]
                    closed_at=closed_at,
                )
            )
            allocations = []
            opened_at = None
            direction = None

        for f in bucket:
            remaining = f.quantity                    # positive magnitude left to allocate
            delta_sign = Decimal(1) if f.side is Side.BUY else Decimal(-1)

            while remaining > 0:
                if position == 0:
                    # Opening a new trade with whatever is left of this fill.
                    opened_at = f.executed_at
                    direction = Direction.LONG if delta_sign > 0 else Direction.SHORT
                    allocations.append(FillAllocation(f.id, remaining))
                    position = delta_sign * remaining
                    remaining = Decimal(0)

                elif (position > 0) == (delta_sign > 0):
                    # Same direction — scaling in.
                    allocations.append(FillAllocation(f.id, remaining))
                    position += delta_sign * remaining
                    remaining = Decimal(0)

                else:
                    # Opposite direction — reducing, possibly through zero.
                    reducible = min(remaining, abs(position))
                    allocations.append(FillAllocation(f.id, reducible))
                    position += delta_sign * reducible
                    remaining -= reducible
                    if position == 0:
                        flush(closed_at=f.executed_at)
                    # Any leftover re-enters the loop and opens an opposite trade.

        flush(closed_at=None)

    return groups
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_grouping.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add ledger/grouping.py tests/test_grouping.py
git commit -m "feat: position-based fill grouping with zero-crossing allocation"
```

---

### Task 4: Grouping invariants (property-based)

Example tests prove the cases you thought of. Property tests find the ones you didn't.

**Files:**
- Create: `tests/test_grouping_properties.py`

**Interfaces:**
- Consumes: `ledger.grouping.group_fills`
- Produces: nothing (test-only)

- [ ] **Step 1: Write the property tests**

```python
# tests/test_grouping_properties.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from ledger.grouping import group_fills
from ledger.types import Fill, FillSource, Side, TradeStatus

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
INST = UUID("00000000-0000-0000-0000-0000000000b1")
T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)

quantities = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("1000"), places=2, allow_nan=False
)
prices = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("100000"), places=2, allow_nan=False
)


@st.composite
def fill_lists(draw):
    n = draw(st.integers(min_value=1, max_value=25))
    out = []
    for i in range(n):
        out.append(
            Fill(
                id=uuid4(),
                account_id=ACC,
                instrument_id=INST,
                executed_at=T0 + timedelta(minutes=i),
                side=draw(st.sampled_from([Side.BUY, Side.SELL])),
                quantity=draw(quantities),
                price=draw(prices),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id=None,
                is_estimated=False,
            )
        )
    return out


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_every_fill_is_fully_allocated(fills):
    """No quantity may be lost or duplicated by grouping."""
    groups = group_fills(fills)
    allocated: dict[UUID, Decimal] = {}
    for g in groups:
        for a in g.allocations:
            allocated[a.fill_id] = allocated.get(a.fill_id, Decimal(0)) + a.quantity
    for f in fills:
        assert allocated.get(f.id, Decimal(0)) == f.quantity


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_closed_trades_net_to_flat(fills):
    """A closed trade's allocations must net exactly to zero position."""
    by_id = {f.id: f for f in fills}
    for g in group_fills(fills):
        if g.status is not TradeStatus.CLOSED:
            continue
        net = sum(
            (
                a.quantity if by_id[a.fill_id].side is Side.BUY else -a.quantity
                for a in g.allocations
            ),
            Decimal(0),
        )
        assert net == Decimal(0)


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_at_most_one_open_trade_per_instrument(fills):
    groups = group_fills(fills)
    assert sum(1 for g in groups if g.status is TradeStatus.OPEN) <= 1


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_grouping_is_idempotent_under_reordering(fills):
    """Order of input must not change the result."""
    assert group_fills(fills) == group_fills(list(reversed(fills)))


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_allocations_are_always_positive(fills):
    for g in group_fills(fills):
        for a in g.allocations:
            assert a.quantity > 0
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_grouping_properties.py -v`
Expected: PASS (5 properties, 200 examples each)

If any fail, hypothesis prints a minimal counterexample. Fix `ledger/grouping.py`, not the
property — the properties are the specification.

- [ ] **Step 3: Commit**

```bash
git add tests/test_grouping_properties.py
git commit -m "test: property-based invariants for fill grouping"
```

---

### Task 5: P&L computation

**Files:**
- Create: `ledger/pnl.py`
- Test: `tests/test_pnl.py`

**Interfaces:**
- Consumes: `ledger.types.Fill`, `Side`, `Direction`; `ledger.grouping.FillAllocation`
- Produces: `TradePnL(qty_opened, qty_closed, avg_entry, avg_exit, gross_realized_pnl,
  fees_total, realized_pnl, open_quantity, open_cost_basis)`,
  `compute_pnl(allocations, fills_by_id, multipliers, direction) -> TradePnL`,
  `unrealized_pnl(open_quantity, open_cost_basis, mark_price, multiplier, direction) -> Decimal`,
  `r_multiple(realized_pnl, planned_risk) -> Decimal | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_pnl.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from ledger.grouping import FillAllocation, group_fills
from ledger.pnl import compute_pnl, r_multiple, unrealized_pnl
from ledger.types import Direction, Fill, FillSource, Side

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
INST = UUID("00000000-0000-0000-0000-0000000000b1")
T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
ONE = {INST: Decimal(1)}


def fill(side, qty, price, minutes=0, fee="0", instrument=INST) -> Fill:
    return Fill(
        id=uuid4(),
        account_id=ACC,
        instrument_id=instrument,
        executed_at=T0 + timedelta(minutes=minutes),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=None,
        is_estimated=False,
    )


def pnl_for(fills, multipliers=None):
    groups = group_fills(fills)
    assert len(groups) == 1
    g = groups[0]
    return compute_pnl(g.allocations, {f.id: f for f in fills}, multipliers or ONE, g.direction)


def test_simple_long_profit():
    result = pnl_for([fill(Side.BUY, "1", "100", 0), fill(Side.SELL, "1", "120", 10)])
    assert result.gross_realized_pnl == Decimal("20")
    assert result.avg_entry == Decimal("100")
    assert result.avg_exit == Decimal("120")
    assert result.qty_opened == Decimal("1")
    assert result.qty_closed == Decimal("1")


def test_simple_short_profit():
    result = pnl_for([fill(Side.SELL, "1", "120", 0), fill(Side.BUY, "1", "100", 10)])
    assert result.gross_realized_pnl == Decimal("20")
    assert result.avg_entry == Decimal("120")
    assert result.avg_exit == Decimal("100")


def test_fees_reduce_net_but_not_gross():
    result = pnl_for(
        [fill(Side.BUY, "1", "100", 0, fee="1.50"), fill(Side.SELL, "1", "120", 10, fee="1.50")]
    )
    assert result.gross_realized_pnl == Decimal("20")
    assert result.fees_total == Decimal("3.00")
    assert result.realized_pnl == Decimal("17.00")


def test_scale_in_uses_average_cost():
    """Buy 1@100 and 1@200, sell 1@200 => avg cost 150, realized 50."""
    result = pnl_for(
        [
            fill(Side.BUY, "1", "100", 0),
            fill(Side.BUY, "1", "200", 10),
            fill(Side.SELL, "1", "200", 20),
        ]
    )
    assert result.avg_entry == Decimal("150")
    assert result.gross_realized_pnl == Decimal("50")
    assert result.open_quantity == Decimal("1")
    assert result.open_cost_basis == Decimal("150")


def test_partial_exit_leaves_open_position():
    result = pnl_for(
        [
            fill(Side.BUY, "2", "100", 0),
            fill(Side.SELL, "1", "130", 10),
        ]
    )
    assert result.gross_realized_pnl == Decimal("30")
    assert result.open_quantity == Decimal("1")
    assert result.open_cost_basis == Decimal("100")
    assert result.qty_closed == Decimal("1")


def test_option_multiplier_scales_pnl():
    """One contract, $1.00 to $2.50, multiplier 100 => $150."""
    result = pnl_for(
        [fill(Side.BUY, "1", "1.00", 0), fill(Side.SELL, "1", "2.50", 10)],
        multipliers={INST: Decimal("100")},
    )
    assert result.gross_realized_pnl == Decimal("150.00")
    assert result.avg_entry == Decimal("1.00")


def test_fee_is_prorated_when_a_fill_is_split_across_trades():
    """A crossing fill's fee must be split by quantity, not double-counted."""
    crossing = fill(Side.SELL, "3", "110", 10, fee="3.00")
    fills = [fill(Side.BUY, "2", "100", 0, fee="2.00"), crossing]
    groups = group_fills(fills)
    by_id = {f.id: f for f in fills}
    closed = compute_pnl(groups[0].allocations, by_id, ONE, groups[0].direction)
    opened = compute_pnl(groups[1].allocations, by_id, ONE, groups[1].direction)
    # crossing fee 3.00 over 3 units => 2.00 to the closed trade, 1.00 to the new one
    assert closed.fees_total == Decimal("4.00")
    assert opened.fees_total == Decimal("1.00")
    assert closed.fees_total + opened.fees_total == Decimal("5.00")


def test_unrealized_long():
    assert unrealized_pnl(
        open_quantity=Decimal("2"),
        open_cost_basis=Decimal("100"),
        mark_price=Decimal("110"),
        multiplier=Decimal("1"),
        direction=Direction.LONG,
    ) == Decimal("20")


def test_unrealized_short():
    assert unrealized_pnl(
        open_quantity=Decimal("2"),
        open_cost_basis=Decimal("100"),
        mark_price=Decimal("90"),
        multiplier=Decimal("1"),
        direction=Direction.SHORT,
    ) == Decimal("20")


def test_r_multiple():
    assert r_multiple(Decimal("210"), Decimal("100")) == Decimal("2.1")


def test_r_multiple_is_none_without_planned_risk():
    assert r_multiple(Decimal("210"), None) is None
    assert r_multiple(Decimal("210"), Decimal("0")) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_pnl.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ledger.pnl'`

- [ ] **Step 3: Write the implementation**

```python
# ledger/pnl.py
"""Realized and unrealized P&L using average-cost basis. Pure — no I/O, no clock.

Average cost per trade, not FIFO tax lots. Deadband is a performance journal,
not a tax tool (spec D6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from ledger.grouping import FillAllocation
from ledger.types import Direction, Fill, Side


@dataclass(frozen=True, slots=True)
class TradePnL:
    qty_opened: Decimal
    qty_closed: Decimal
    avg_entry: Decimal
    avg_exit: Decimal | None
    gross_realized_pnl: Decimal
    fees_total: Decimal
    realized_pnl: Decimal          # net of fees
    open_quantity: Decimal
    open_cost_basis: Decimal       # per unit, excluding multiplier


def compute_pnl(
    allocations: Sequence[FillAllocation],
    fills_by_id: Mapping[UUID, Fill],
    multipliers: Mapping[UUID, Decimal],
    direction: Direction,
) -> TradePnL:
    """Walk allocations chronologically, maintaining a running average cost."""
    ordered = sorted(
        allocations,
        key=lambda a: (fills_by_id[a.fill_id].executed_at, str(a.fill_id)),
    )
    opening_side = Side.SELL if direction is Direction.SHORT else Side.BUY

    position = Decimal(0)          # units of open position
    basis_total = Decimal(0)       # cost of the open position, per-unit terms
    qty_opened = Decimal(0)
    qty_closed = Decimal(0)
    entry_notional = Decimal(0)
    exit_notional = Decimal(0)
    gross = Decimal(0)
    fees = Decimal(0)

    for alloc in ordered:
        f = fills_by_id[alloc.fill_id]
        qty = alloc.quantity
        mult = multipliers.get(f.instrument_id, Decimal(1))

        # Pro-rate the fee by this allocation's share of the fill.
        fees += (f.fee * qty / f.quantity) if f.quantity else Decimal(0)

        if f.side is opening_side:
            basis_total += qty * f.price
            position += qty
            qty_opened += qty
            entry_notional += qty * f.price
        else:
            avg_cost = (basis_total / position) if position else Decimal(0)
            per_unit = (f.price - avg_cost) if direction is not Direction.SHORT else (avg_cost - f.price)
            gross += per_unit * qty * mult
            basis_total -= avg_cost * qty
            position -= qty
            qty_closed += qty
            exit_notional += qty * f.price

    return TradePnL(
        qty_opened=qty_opened,
        qty_closed=qty_closed,
        avg_entry=(entry_notional / qty_opened) if qty_opened else Decimal(0),
        avg_exit=(exit_notional / qty_closed) if qty_closed else None,
        gross_realized_pnl=gross,
        fees_total=fees,
        realized_pnl=gross - fees,
        open_quantity=position,
        open_cost_basis=(basis_total / position) if position else Decimal(0),
    )


def unrealized_pnl(
    open_quantity: Decimal,
    open_cost_basis: Decimal,
    mark_price: Decimal,
    multiplier: Decimal,
    direction: Direction,
) -> Decimal:
    if open_quantity == 0:
        return Decimal(0)
    per_unit = (
        (open_cost_basis - mark_price)
        if direction is Direction.SHORT
        else (mark_price - open_cost_basis)
    )
    return per_unit * open_quantity * multiplier


def r_multiple(realized_pnl: Decimal, planned_risk: Decimal | None) -> Decimal | None:
    """R-multiple, or None when risk was never recorded. Never guess it."""
    if planned_risk is None or planned_risk == 0:
        return None
    return realized_pnl / planned_risk
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_pnl.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add ledger/pnl.py tests/test_pnl.py
git commit -m "feat: average-cost P&L with multipliers and pro-rated fees"
```

---

### Task 6: Corporate actions

**Files:**
- Create: `ledger/corporate.py`
- Test: `tests/test_corporate.py`

**Interfaces:**
- Consumes: `ledger.types.Fill`
- Produces: `ActionType` (StrEnum), `CorporateAction(instrument_id, action_type, ex_date,
  ratio_numerator, ratio_denominator, resulting_instrument_id, cash_component)`,
  `adjust_fills(fills, actions) -> list[Fill]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_corporate.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from ledger.corporate import ActionType, CorporateAction, adjust_fills
from ledger.types import Fill, FillSource, Side

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
OLD = UUID("00000000-0000-0000-0000-0000000000b1")
NEW = UUID("00000000-0000-0000-0000-0000000000b2")


def fill(qty, price, day, instrument=OLD) -> Fill:
    return Fill(
        id=uuid4(),
        account_id=ACC,
        instrument_id=instrument,
        executed_at=datetime(2026, 6, day, 15, 0, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.CSV,
        venue_fill_id=None,
        is_estimated=False,
    )


def split(day, num, den, instrument=OLD) -> CorporateAction:
    return CorporateAction(
        instrument_id=instrument,
        action_type=ActionType.SPLIT,
        ex_date=datetime(2026, 6, day, tzinfo=UTC).date(),
        ratio_numerator=Decimal(num),
        ratio_denominator=Decimal(den),
    )


def test_forward_split_multiplies_quantity_and_divides_price():
    before = fill("10", "500", 1)
    adjusted = adjust_fills([before], [split(15, 4, 1)])
    assert adjusted[0].quantity == Decimal("40")
    assert adjusted[0].price == Decimal("125")


def test_split_leaves_notional_value_unchanged():
    before = fill("10", "500", 1)
    adjusted = adjust_fills([before], [split(15, 4, 1)])
    assert adjusted[0].quantity * adjusted[0].price == before.quantity * before.price


def test_fills_after_ex_date_are_untouched():
    after = fill("10", "125", 20)
    adjusted = adjust_fills([after], [split(15, 4, 1)])
    assert adjusted[0].quantity == Decimal("10")
    assert adjusted[0].price == Decimal("125")


def test_reverse_split_divides_quantity_and_multiplies_price():
    before = fill("100", "2", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.REVERSE_SPLIT,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(10),
    )
    adjusted = adjust_fills([before], [action])
    assert adjusted[0].quantity == Decimal("10")
    assert adjusted[0].price == Decimal("20")


def test_two_sequential_splits_compound():
    before = fill("10", "400", 1)
    adjusted = adjust_fills([before], [split(10, 2, 1), split(20, 2, 1)])
    assert adjusted[0].quantity == Decimal("40")
    assert adjusted[0].price == Decimal("100")


def test_symbol_change_remaps_instrument_without_touching_quantity():
    before = fill("10", "50", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SYMBOL_CHANGE,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(1),
        resulting_instrument_id=NEW,
    )
    adjusted = adjust_fills([before], [action])
    assert adjusted[0].instrument_id == NEW
    assert adjusted[0].quantity == Decimal("10")
    assert adjusted[0].price == Decimal("50")


def test_merger_remaps_and_applies_exchange_ratio():
    """0.5 shares of NEW per share of OLD."""
    before = fill("10", "50", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(2),
        resulting_instrument_id=NEW,
    )
    adjusted = adjust_fills([before], [action])
    assert adjusted[0].instrument_id == NEW
    assert adjusted[0].quantity == Decimal("5")
    assert adjusted[0].price == Decimal("100")


def test_spinoff_allocates_cost_basis_and_adds_a_position():
    """20% of basis moves to the spun-off instrument."""
    before = fill("10", "100", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),      # 1 new share per 5 held
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0.20"),
    )
    adjusted = adjust_fills([before], [action])
    parent = [f for f in adjusted if f.instrument_id == OLD][0]
    spun = [f for f in adjusted if f.instrument_id == NEW][0]
    assert parent.quantity == Decimal("10")
    assert parent.price == Decimal("80")            # 80% of basis retained
    assert spun.quantity == Decimal("2")            # 10 / 5
    assert spun.price == Decimal("100")             # 20% of 1000 over 2 shares
    assert spun.is_estimated is True


def test_actions_never_mutate_the_input():
    before = fill("10", "500", 1)
    adjust_fills([before], [split(15, 4, 1)])
    assert before.quantity == Decimal("10")
    assert before.price == Decimal("500")


def test_unrelated_instrument_is_untouched():
    other = fill("10", "500", 1, instrument=NEW)
    adjusted = adjust_fills([other], [split(15, 4, 1)])
    assert adjusted[0].quantity == Decimal("10")


def test_zero_ratio_is_rejected():
    with pytest.raises(ValueError, match="ratio"):
        CorporateAction(
            instrument_id=OLD,
            action_type=ActionType.SPLIT,
            ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
            ratio_numerator=Decimal(0),
            ratio_denominator=Decimal(1),
        )
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_corporate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ledger.corporate'`

- [ ] **Step 3: Write the implementation**

```python
# ledger/corporate.py
"""Corporate action adjustments as a computed layer. Pure — no I/O, no clock.

Raw fills are never mutated (spec D10). These functions return adjusted copies,
so a wrong adjustment is fixable and ground truth stays intact.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from ledger.types import Fill


class ActionType(StrEnum):
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    MERGER = "merger"
    SPINOFF = "spinoff"
    SYMBOL_CHANGE = "symbol_change"


@dataclass(frozen=True, slots=True)
class CorporateAction:
    instrument_id: UUID
    action_type: ActionType
    ex_date: date
    ratio_numerator: Decimal
    ratio_denominator: Decimal
    resulting_instrument_id: UUID | None = None
    cash_component: Decimal | None = None
    basis_allocation: Decimal | None = None    # spinoff: fraction of basis moved

    def __post_init__(self) -> None:
        if self.ratio_numerator <= 0 or self.ratio_denominator <= 0:
            raise ValueError("corporate action ratio components must be positive")
        if self.action_type in {ActionType.MERGER, ActionType.SPINOFF, ActionType.SYMBOL_CHANGE}:
            if self.resulting_instrument_id is None:
                raise ValueError(f"{self.action_type} requires resulting_instrument_id")


def adjust_fills(
    fills: Sequence[Fill], actions: Sequence[CorporateAction]
) -> list[Fill]:
    """Return adjusted copies of `fills`, applying `actions` in ex-date order."""
    result = list(fills)

    for action in sorted(actions, key=lambda a: (a.ex_date, a.action_type.value)):
        ratio = action.ratio_numerator / action.ratio_denominator
        next_result: list[Fill] = []

        for f in result:
            if f.instrument_id != action.instrument_id or f.executed_at.date() >= action.ex_date:
                next_result.append(f)
                continue

            if action.action_type in {ActionType.SPLIT, ActionType.REVERSE_SPLIT}:
                next_result.append(
                    dataclasses.replace(f, quantity=f.quantity * ratio, price=f.price / ratio)
                )

            elif action.action_type is ActionType.SYMBOL_CHANGE:
                next_result.append(
                    dataclasses.replace(f, instrument_id=action.resulting_instrument_id)
                )

            elif action.action_type is ActionType.MERGER:
                next_result.append(
                    dataclasses.replace(
                        f,
                        instrument_id=action.resulting_instrument_id,
                        quantity=f.quantity * ratio,
                        price=f.price / ratio,
                    )
                )

            elif action.action_type is ActionType.SPINOFF:
                fraction = action.basis_allocation or Decimal(0)
                spun_qty = f.quantity * ratio
                total_basis = f.quantity * f.price
                next_result.append(
                    dataclasses.replace(f, price=f.price * (Decimal(1) - fraction))
                )
                if spun_qty > 0:
                    next_result.append(
                        dataclasses.replace(
                            f,
                            id=uuid4(),
                            instrument_id=action.resulting_instrument_id,
                            quantity=spun_qty,
                            price=(total_basis * fraction) / spun_qty,
                            fee=Decimal(0),
                            is_estimated=True,
                            venue_fill_id=None,
                            content_hash=None,
                        )
                    )

        result = next_result

    return result
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_corporate.py -v`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add ledger/corporate.py tests/test_corporate.py
git commit -m "feat: corporate action adjustment layer"
```

---

### Task 7: Reconciliation

**Files:**
- Create: `ledger/reconcile.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `ledger.types` only
- Produces: `Position(instrument_id, quantity, cost_basis, multiplier)`,
  `Snapshot(account_id, as_of, cash_balance, total_equity)`,
  `Drift(account_id, as_of, computed_equity, reported_equity, equity_difference,
  computed_cash, reported_cash, cash_difference, is_within_tolerance)`,
  `reconcile(snapshot, positions, marks, computed_cash, tolerance) -> Drift`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reconcile.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from ledger.reconcile import Position, Snapshot, reconcile

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
SPY = UUID("00000000-0000-0000-0000-0000000000b1")
AAPL = UUID("00000000-0000-0000-0000-0000000000b2")
AS_OF = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)


def snapshot(cash, equity) -> Snapshot:
    return Snapshot(
        account_id=ACC, as_of=AS_OF, cash_balance=Decimal(cash), total_equity=Decimal(equity)
    )


def test_matching_account_reports_no_drift():
    positions = [Position(SPY, Decimal("10"), Decimal("500"), Decimal("1"))]
    drift = reconcile(
        snapshot=snapshot("1000", "6000"),
        positions=positions,
        marks={SPY: Decimal("500")},
        computed_cash=Decimal("1000"),
    )
    assert drift.computed_equity == Decimal("6000")
    assert drift.equity_difference == Decimal("0")
    assert drift.is_within_tolerance is True


def test_equity_drift_is_reported_with_sign():
    positions = [Position(SPY, Decimal("10"), Decimal("500"), Decimal("1"))]
    drift = reconcile(
        snapshot=snapshot("1000", "6312"),
        positions=positions,
        marks={SPY: Decimal("500")},
        computed_cash=Decimal("1000"),
    )
    assert drift.computed_equity == Decimal("6000")
    assert drift.reported_equity == Decimal("6312")
    assert drift.equity_difference == Decimal("-312")
    assert drift.is_within_tolerance is False


def test_cash_drift_is_reported_separately():
    drift = reconcile(
        snapshot=snapshot("900", "900"),
        positions=[],
        marks={},
        computed_cash=Decimal("1000"),
    )
    assert drift.cash_difference == Decimal("-100")


def test_multiplier_is_applied_to_position_value():
    positions = [Position(SPY, Decimal("2"), Decimal("1.50"), Decimal("100"))]
    drift = reconcile(
        snapshot=snapshot("0", "500"),
        positions=positions,
        marks={SPY: Decimal("2.50")},
        computed_cash=Decimal("0"),
    )
    assert drift.computed_equity == Decimal("500")


def test_position_without_a_mark_falls_back_to_cost_basis():
    """An unmarked position must not silently value at zero."""
    positions = [Position(AAPL, Decimal("5"), Decimal("200"), Decimal("1"))]
    drift = reconcile(
        snapshot=snapshot("0", "1000"), positions=positions, marks={}, computed_cash=Decimal("0")
    )
    assert drift.computed_equity == Decimal("1000")
    assert drift.unmarked_instruments == (AAPL,)


def test_tolerance_is_configurable():
    drift = reconcile(
        snapshot=snapshot("1000", "1005"),
        positions=[],
        marks={},
        computed_cash=Decimal("1000"),
        tolerance=Decimal("10"),
    )
    assert drift.equity_difference == Decimal("-5")
    assert drift.is_within_tolerance is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ledger.reconcile'`

- [ ] **Step 3: Write the implementation**

```python
# ledger/reconcile.py
"""Compare the computed ledger against a broker statement. Pure — no I/O, no clock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True, slots=True)
class Position:
    instrument_id: UUID
    quantity: Decimal
    cost_basis: Decimal        # per unit, excluding multiplier
    multiplier: Decimal


@dataclass(frozen=True, slots=True)
class Snapshot:
    account_id: UUID
    as_of: datetime
    cash_balance: Decimal
    total_equity: Decimal


@dataclass(frozen=True, slots=True)
class Drift:
    account_id: UUID
    as_of: datetime
    computed_equity: Decimal
    reported_equity: Decimal
    equity_difference: Decimal      # computed - reported
    computed_cash: Decimal
    reported_cash: Decimal
    cash_difference: Decimal
    unmarked_instruments: tuple[UUID, ...]
    is_within_tolerance: bool


def reconcile(
    snapshot: Snapshot,
    positions: Sequence[Position],
    marks: Mapping[UUID, Decimal],
    computed_cash: Decimal,
    tolerance: Decimal = Decimal("0.01"),
) -> Drift:
    """Value positions at their marks, add cash, and compare to the statement."""
    market_value = Decimal(0)
    unmarked: list[UUID] = []

    for p in positions:
        price = marks.get(p.instrument_id)
        if price is None:
            # Falling back to cost basis is a knowingly stale valuation, not a zero.
            price = p.cost_basis
            unmarked.append(p.instrument_id)
        market_value += p.quantity * price * p.multiplier

    computed_equity = computed_cash + market_value
    equity_difference = computed_equity - snapshot.total_equity
    cash_difference = computed_cash - snapshot.cash_balance

    return Drift(
        account_id=snapshot.account_id,
        as_of=snapshot.as_of,
        computed_equity=computed_equity,
        reported_equity=snapshot.total_equity,
        equity_difference=equity_difference,
        computed_cash=computed_cash,
        reported_cash=snapshot.cash_balance,
        cash_difference=cash_difference,
        unmarked_instruments=tuple(unmarked),
        is_within_tolerance=abs(equity_difference) <= tolerance
        and abs(cash_difference) <= tolerance,
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/ -v`
Expected: PASS (all tests to date)

- [ ] **Step 5: Commit**

```bash
git add ledger/reconcile.py tests/test_reconcile.py
git commit -m "feat: statement reconciliation with explicit unmarked-position reporting"
```

---

### Task 8: Database schema and migration runner

**Files:**
- Create: `db/schema.sql`, `db/migrate.py`, `db/pool.py`
- Test: `tests/conftest.py`, `tests/db/test_migrations.py`

**Interfaces:**
- Consumes: `PG_DSN` / `TEST_PG_DSN` environment variables
- Produces: `db.pool.create_pool(dsn) -> asyncpg.Pool`, `db.migrate.apply(conn) -> list[str]`
  (names of migrations applied), and the schema every later task queries

- [ ] **Step 1: Write `db/schema.sql`**

```sql
-- Deadband ledger schema. All money and quantity columns are NUMERIC, never
-- FLOAT. All timestamps are TIMESTAMPTZ, stored UTC.

CREATE TABLE IF NOT EXISTS schema_migrations (
    name        TEXT PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS account (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    venue           TEXT NOT NULL,
    external_ref    TEXT,
    account_type    TEXT NOT NULL CHECK (account_type IN ('cash','margin','funded','wallet')),
    default_intent  TEXT NOT NULL DEFAULT 'trade'
                    CHECK (default_intent IN ('trade','investment','mixed')),
    base_currency   TEXT NOT NULL DEFAULT 'USD',
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    opened_at       TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (venue, external_ref)
);

CREATE TABLE IF NOT EXISTS funded_account_rule (
    account_id          UUID PRIMARY KEY REFERENCES account(id) ON DELETE CASCADE,
    max_drawdown        NUMERIC,
    drawdown_type       TEXT CHECK (drawdown_type IN ('static','trailing')),
    daily_loss_limit    NUMERIC,
    profit_target       NUMERIC,
    payout_split        NUMERIC,
    consistency_rule    TEXT,
    current_state       JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluated_at        TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS instrument (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    natural_key         TEXT NOT NULL UNIQUE,
    asset_class         TEXT NOT NULL
                        CHECK (asset_class IN
                               ('crypto_spot','crypto_perp','equity','option','future')),
    symbol              TEXT NOT NULL,
    quote_currency      TEXT NOT NULL DEFAULT 'USD',
    underlying          TEXT,
    strike              NUMERIC,
    expiry              DATE,
    option_right        TEXT CHECK (option_right IN ('call','put')),
    root                TEXT,
    contract_multiplier NUMERIC NOT NULL DEFAULT 1,
    chain               TEXT,
    contract_address    TEXT,
    active_from         DATE,
    active_to           DATE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    primary_underlying  TEXT,
    direction           TEXT NOT NULL CHECK (direction IN ('long','short','spread')),
    status              TEXT NOT NULL CHECK (status IN ('open','closed')),
    intent              TEXT NOT NULL DEFAULT 'unassigned'
                        CHECK (intent IN ('trade','investment','unassigned')),
    grouping_mode       TEXT NOT NULL DEFAULT 'auto'
                        CHECK (grouping_mode IN ('auto','manual')),
    -- Stable identity for auto trades. Regroup upserts on this instead of
    -- deleting and rebuilding, so user-authored fields survive re-imports.
    opening_fill_id     UUID REFERENCES fill(id) ON DELETE CASCADE,
    opened_at           TIMESTAMPTZ NOT NULL,
    closed_at           TIMESTAMPTZ,
    qty_opened          NUMERIC,
    qty_closed          NUMERIC,
    avg_entry           NUMERIC,
    avg_exit            NUMERIC,
    realized_pnl        NUMERIC,
    gross_realized_pnl  NUMERIC,
    fees_total          NUMERIC,
    planned_risk        NUMERIC,
    r_multiple          NUMERIC,
    strategy_tag        TEXT,
    rolled_from_id      UUID REFERENCES trade(id) ON DELETE SET NULL,
    notes               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS trade_account_status_idx ON trade (account_id, status);
CREATE INDEX IF NOT EXISTS trade_opened_at_idx ON trade (opened_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS trade_opening_fill_uniq
    ON trade (account_id, opening_fill_id) WHERE opening_fill_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS fill (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    instrument_id   UUID NOT NULL REFERENCES instrument(id),
    executed_at     TIMESTAMPTZ NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy','sell')),
    quantity        NUMERIC NOT NULL CHECK (quantity > 0),
    price           NUMERIC NOT NULL CHECK (price >= 0),
    fee             NUMERIC NOT NULL DEFAULT 0,
    fee_currency    TEXT NOT NULL DEFAULT 'USD',
    source          TEXT NOT NULL
                    CHECK (source IN ('manual','csv','api','opening_balance')),
    venue_order_id  TEXT,
    venue_fill_id   TEXT,
    content_hash    TEXT,
    is_estimated    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent import rests on these two. A venue fill id is authoritative when
-- present; the content hash is the fallback for exports that carry no id.
CREATE UNIQUE INDEX IF NOT EXISTS fill_venue_id_uniq
    ON fill (account_id, venue_fill_id) WHERE venue_fill_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS fill_content_hash_uniq
    ON fill (account_id, content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX IF NOT EXISTS fill_account_instrument_time_idx
    ON fill (account_id, instrument_id, executed_at);

-- Association is an allocation, not a foreign key on fill: one fill that crosses
-- zero belongs to two trades, split by quantity.
CREATE TABLE IF NOT EXISTS trade_fill (
    trade_id    UUID NOT NULL REFERENCES trade(id) ON DELETE CASCADE,
    fill_id     UUID NOT NULL REFERENCES fill(id) ON DELETE CASCADE,
    quantity    NUMERIC NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (trade_id, fill_id)
);

CREATE INDEX IF NOT EXISTS trade_fill_fill_idx ON trade_fill (fill_id);

CREATE TABLE IF NOT EXISTS cash_movement (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    occurred_at     TIMESTAMPTZ NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN
                    ('deposit','withdrawal','fee','funding','interest',
                     'dividend','payout','rebate')),
    amount          NUMERIC NOT NULL,
    currency        TEXT NOT NULL DEFAULT 'USD',
    instrument_id   UUID REFERENCES instrument(id),
    venue_ref       TEXT,
    content_hash    TEXT,
    note            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS cash_content_hash_uniq
    ON cash_movement (account_id, content_hash) WHERE content_hash IS NOT NULL;

CREATE TABLE IF NOT EXISTS mark (
    instrument_id   UUID NOT NULL REFERENCES instrument(id) ON DELETE CASCADE,
    as_of           TIMESTAMPTZ NOT NULL,
    price           NUMERIC NOT NULL,
    source          TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (instrument_id, as_of)
);

CREATE TABLE IF NOT EXISTS corporate_action (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_id           UUID NOT NULL REFERENCES instrument(id) ON DELETE CASCADE,
    action_type             TEXT NOT NULL CHECK (action_type IN
                            ('split','reverse_split','merger','spinoff','symbol_change')),
    ex_date                 DATE NOT NULL,
    ratio_numerator         NUMERIC NOT NULL CHECK (ratio_numerator > 0),
    ratio_denominator       NUMERIC NOT NULL CHECK (ratio_denominator > 0),
    resulting_instrument_id UUID REFERENCES instrument(id),
    cash_component          NUMERIC,
    basis_allocation        NUMERIC,
    note                    TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS account_snapshot (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES account(id) ON DELETE CASCADE,
    as_of           TIMESTAMPTZ NOT NULL,
    cash_balance    NUMERIC NOT NULL,
    total_equity    NUMERIC NOT NULL,
    source          TEXT NOT NULL DEFAULT 'statement',
    note            TEXT,
    UNIQUE (account_id, as_of)
);
```

- [ ] **Step 2: Write `db/pool.py` and `db/migrate.py`**

```python
# db/pool.py
"""asyncpg pool lifecycle. The only place that opens database connections."""

from __future__ import annotations

import os

import asyncpg


async def create_pool(dsn: str | None = None, **kwargs) -> asyncpg.Pool:
    resolved = dsn or os.environ.get("PG_DSN")
    if not resolved:
        raise RuntimeError("PG_DSN is not set and no dsn was provided")
    return await asyncpg.create_pool(resolved, min_size=1, max_size=5, **kwargs)
```

```python
# db/migrate.py
"""Idempotent migration runner. Applies schema.sql, then db/migrations/*.sql in
name order, recording each in schema_migrations so reruns are no-ops."""

from __future__ import annotations

import pathlib

import asyncpg

DB_DIR = pathlib.Path(__file__).parent
SCHEMA = DB_DIR / "schema.sql"
MIGRATIONS = DB_DIR / "migrations"


async def apply(conn: asyncpg.Connection) -> list[str]:
    """Apply pending migrations. Returns the names applied this run."""
    await conn.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    await conn.execute(SCHEMA.read_text())

    done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
    applied: list[str] = []

    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        async with conn.transaction():
            await conn.execute(path.read_text())
            await conn.execute(
                "INSERT INTO schema_migrations (name) VALUES ($1)", path.name
            )
        applied.append(path.name)

    return applied
```

- [ ] **Step 3: Write `tests/conftest.py`**

```python
# tests/conftest.py
import os

import pytest
import pytest_asyncio

from db.migrate import apply
from db.pool import create_pool

TEST_DSN = os.environ.get("TEST_PG_DSN")

requires_db = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_PG_DSN not set — database tests are opt-in"
)


@pytest_asyncio.fixture
async def pool():
    if not TEST_DSN:
        pytest.skip("TEST_PG_DSN not set")
    p = await create_pool(TEST_DSN)
    async with p.acquire() as conn:
        await apply(conn)
    yield p
    await p.close()


@pytest_asyncio.fixture
async def conn(pool):
    """A connection inside a transaction that is always rolled back, so tests
    never leave residue and can run in any order."""
    async with pool.acquire() as c:
        tx = c.transaction()
        await tx.start()
        try:
            yield c
        finally:
            await tx.rollback()
```

- [ ] **Step 4: Write the migration test**

```python
# tests/db/test_migrations.py
import pytest

from db.migrate import apply
from tests.conftest import requires_db

pytestmark = requires_db


async def test_schema_creates_expected_tables(conn):
    rows = await conn.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
    )
    names = {r["tablename"] for r in rows}
    assert {
        "account",
        "instrument",
        "fill",
        "trade",
        "trade_fill",
        "cash_movement",
        "mark",
        "corporate_action",
        "account_snapshot",
        "funded_account_rule",
    } <= names


async def test_apply_is_idempotent(pool):
    async with pool.acquire() as c:
        assert await apply(c) == []      # already applied by the fixture


async def test_fill_rejects_non_positive_quantity(conn):
    acc = await conn.fetchval(
        "INSERT INTO account (name, venue, account_type) "
        "VALUES ('t', 'manual', 'cash') RETURNING id"
    )
    inst = await conn.fetchval(
        "INSERT INTO instrument (natural_key, asset_class, symbol) "
        "VALUES ('equity:T:USD', 'equity', 'T') RETURNING id"
    )
    with pytest.raises(Exception, match="quantity"):
        await conn.execute(
            "INSERT INTO fill (account_id, instrument_id, executed_at, side, "
            "quantity, price, source) VALUES ($1, $2, now(), 'buy', 0, 1, 'manual')",
            acc,
            inst,
        )
```

- [ ] **Step 5: Run the tests**

```bash
mkdir -p tests/db && touch tests/db/__init__.py
createdb deadband_test 2>/dev/null || true
TEST_PG_DSN=postgresql://localhost/deadband_test uv run pytest tests/db -v
```

Expected: PASS. Without `TEST_PG_DSN` they SKIP, which is also correct.

- [ ] **Step 6: Commit**

```bash
git add db/ tests/conftest.py tests/db/
git commit -m "feat: ledger schema with allocation-based trade_fill and migration runner"
```

---

### Task 9: Account and instrument repositories

**Files:**
- Create: `db/accounts.py`, `db/instruments.py`
- Test: `tests/db/test_instruments.py`

**Interfaces:**
- Consumes: `ledger.types.Instrument`, `instrument_natural_key`
- Produces: `db.accounts.create_account(conn, ...) -> UUID`,
  `db.accounts.get_account(conn, account_id) -> Record | None`,
  `db.accounts.find_by_external_ref(conn, venue, external_ref) -> UUID | None`,
  `db.instruments.upsert_instrument(conn, instrument) -> UUID`,
  `db.instruments.get_multipliers(conn, instrument_ids) -> dict[UUID, Decimal]`

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_instruments.py
from datetime import UTC, datetime
from decimal import Decimal

from db.instruments import get_multipliers, upsert_instrument
from ledger.types import AssetClass, Instrument
from tests.conftest import requires_db

pytestmark = requires_db


def equity(symbol="SPY") -> Instrument:
    return Instrument(
        id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency="USD"
    )


def option(strike="500") -> Instrument:
    return Instrument(
        id=None,
        asset_class=AssetClass.OPTION,
        symbol=f"SPY 26SEP19 {strike} C",
        quote_currency="USD",
        underlying="SPY",
        strike=Decimal(strike),
        expiry=datetime(2026, 9, 19, tzinfo=UTC).date(),
        option_right="call",
        contract_multiplier=Decimal("100"),
    )


async def test_upsert_returns_the_same_id_for_the_same_instrument(conn):
    first = await upsert_instrument(conn, equity())
    second = await upsert_instrument(conn, equity())
    assert first == second


async def test_differently_formatted_strikes_collapse_to_one_row(conn):
    a = await upsert_instrument(conn, option("500"))
    b = await upsert_instrument(conn, option("500.00"))
    assert a == b
    count = await conn.fetchval("SELECT count(*) FROM instrument")
    assert count == 1


async def test_different_instruments_get_different_ids(conn):
    a = await upsert_instrument(conn, equity("SPY"))
    b = await upsert_instrument(conn, equity("QQQ"))
    assert a != b


async def test_multipliers_are_fetched_for_pnl(conn):
    opt = await upsert_instrument(conn, option())
    eq = await upsert_instrument(conn, equity())
    mults = await get_multipliers(conn, [opt, eq])
    assert mults[opt] == Decimal("100")
    assert mults[eq] == Decimal("1")
```

- [ ] **Step 2: Run to verify it fails**

Run: `TEST_PG_DSN=postgresql://localhost/deadband_test uv run pytest tests/db/test_instruments.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.instruments'`

- [ ] **Step 3: Write the implementations**

```python
# db/instruments.py
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

import asyncpg

from ledger.types import Instrument, instrument_natural_key


async def upsert_instrument(conn: asyncpg.Connection, instrument: Instrument) -> UUID:
    """Insert or fetch by natural key. Two spellings of one contract collapse to one row."""
    key = instrument_natural_key(instrument)
    return await conn.fetchval(
        """
        INSERT INTO instrument (
            natural_key, asset_class, symbol, quote_currency, underlying, strike,
            expiry, option_right, root, contract_multiplier, chain, contract_address
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        ON CONFLICT (natural_key) DO UPDATE SET symbol = EXCLUDED.symbol
        RETURNING id
        """,
        key,
        instrument.asset_class.value,
        instrument.symbol,
        instrument.quote_currency.upper(),
        instrument.underlying,
        instrument.strike,
        instrument.expiry,
        instrument.option_right,
        instrument.root,
        instrument.contract_multiplier,
        instrument.chain,
        instrument.contract_address,
    )


async def get_multipliers(
    conn: asyncpg.Connection, instrument_ids: Sequence[UUID]
) -> dict[UUID, Decimal]:
    if not instrument_ids:
        return {}
    rows = await conn.fetch(
        "SELECT id, contract_multiplier FROM instrument WHERE id = ANY($1::uuid[])",
        list(instrument_ids),
    )
    return {r["id"]: r["contract_multiplier"] for r in rows}
```

```python
# db/accounts.py
from __future__ import annotations

from uuid import UUID

import asyncpg


async def create_account(
    conn: asyncpg.Connection,
    *,
    name: str,
    venue: str,
    account_type: str,
    default_intent: str = "trade",
    external_ref: str | None = None,
    base_currency: str = "USD",
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO account (name, venue, external_ref, account_type,
                             default_intent, base_currency)
        VALUES ($1,$2,$3,$4,$5,$6)
        RETURNING id
        """,
        name,
        venue,
        external_ref,
        account_type,
        default_intent,
        base_currency,
    )


async def get_account(conn: asyncpg.Connection, account_id: UUID) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM account WHERE id = $1", account_id)


async def find_by_external_ref(
    conn: asyncpg.Connection, venue: str, external_ref: str
) -> UUID | None:
    """Route imported rows to the right account when a venue has several."""
    return await conn.fetchval(
        "SELECT id FROM account WHERE venue = $1 AND external_ref = $2",
        venue,
        external_ref,
    )


async def list_accounts(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch("SELECT * FROM account ORDER BY name")
```

- [ ] **Step 4: Run to verify it passes**

Run: `TEST_PG_DSN=postgresql://localhost/deadband_test uv run pytest tests/db -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add db/accounts.py db/instruments.py tests/db/test_instruments.py
git commit -m "feat: account and instrument repositories with natural-key upsert"
```

---

### Task 10: Fill persistence and the regroup pipeline

**Files:**
- Create: `db/fills.py`, `db/trades.py`
- Test: `tests/db/test_fills.py`, `tests/db/test_trades.py`

**Interfaces:**
- Consumes: `ledger.grouping.group_fills`, `ledger.pnl.compute_pnl`,
  `db.instruments.get_multipliers`
- Produces: `db.fills.insert_fills(conn, fills) -> InsertResult(inserted, skipped)`,
  `db.fills.fetch_fills(conn, account_id=None) -> list[Fill]`,
  `db.trades.regroup_account(conn, account_id) -> int` (trades written),
  `db.trades.list_trades(conn, account_id=None) -> list[Record]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/db/test_fills.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.fills import fetch_fills, insert_fills
from db.instruments import upsert_instrument
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db


async def setup_account_and_instrument(conn):
    acc = await create_account(conn, name="Test", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"),
    )
    return acc, inst


def make_fill(acc, inst, *, venue_fill_id=None, content_hash=None, qty="1") -> Fill:
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal("500"),
        fee=Decimal("1"),
        fee_currency="USD",
        source=FillSource.CSV,
        venue_fill_id=venue_fill_id,
        content_hash=content_hash,
        is_estimated=False,
    )


async def test_insert_and_fetch_round_trips_decimals(conn):
    acc, inst = await setup_account_and_instrument(conn)
    result = await insert_fills(conn, [make_fill(acc, inst, venue_fill_id="v1")])
    assert result.inserted == 1
    fetched = await fetch_fills(conn, acc)
    assert fetched[0].quantity == Decimal("1")
    assert fetched[0].price == Decimal("500")
    assert isinstance(fetched[0].price, Decimal)


async def test_reimporting_the_same_venue_fill_id_is_a_no_op(conn):
    acc, inst = await setup_account_and_instrument(conn)
    await insert_fills(conn, [make_fill(acc, inst, venue_fill_id="v1")])
    result = await insert_fills(conn, [make_fill(acc, inst, venue_fill_id="v1")])
    assert result.inserted == 0
    assert result.skipped == 1
    assert len(await fetch_fills(conn, acc)) == 1


async def test_reimporting_the_same_content_hash_is_a_no_op(conn):
    acc, inst = await setup_account_and_instrument(conn)
    await insert_fills(conn, [make_fill(acc, inst, content_hash="h1")])
    result = await insert_fills(conn, [make_fill(acc, inst, content_hash="h1")])
    assert result.inserted == 0
    assert result.skipped == 1


async def test_same_venue_fill_id_in_a_different_account_is_not_a_duplicate(conn):
    acc, inst = await setup_account_and_instrument(conn)
    other = await create_account(conn, name="Other", venue="manual", account_type="cash")
    await insert_fills(conn, [make_fill(acc, inst, venue_fill_id="v1")])
    result = await insert_fills(conn, [make_fill(other, inst, venue_fill_id="v1")])
    assert result.inserted == 1
```

```python
# tests/db/test_trades.py
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.trades import list_trades, regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


async def seed(conn, specs):
    acc = await create_account(conn, name="T", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"),
    )
    fills = [
        Fill(
            id=uuid4(),
            account_id=acc,
            instrument_id=inst,
            executed_at=T0 + timedelta(minutes=i * 10),
            side=side,
            quantity=Decimal(q),
            price=Decimal(p),
            fee=Decimal("0"),
            fee_currency="USD",
            source=FillSource.MANUAL,
            venue_fill_id=f"v{i}",
            is_estimated=False,
        )
        for i, (side, q, p) in enumerate(specs)
    ]
    await insert_fills(conn, fills)
    return acc


async def test_regroup_writes_one_closed_trade_with_pnl(conn):
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    assert await regroup_account(conn, acc) == 1
    trades = await list_trades(conn, acc)
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"
    assert trades[0]["realized_pnl"] == Decimal("20")
    assert trades[0]["avg_entry"] == Decimal("100")


async def test_allocations_are_persisted(conn):
    acc = await seed(conn, [(Side.BUY, "2", "100"), (Side.SELL, "3", "110")])
    await regroup_account(conn, acc)
    rows = await conn.fetch(
        "SELECT tf.quantity FROM trade_fill tf "
        "JOIN trade t ON t.id = tf.trade_id WHERE t.account_id = $1 "
        "ORDER BY tf.quantity",
        acc,
    )
    quantities = sorted(r["quantity"] for r in rows)
    assert quantities == [Decimal("1"), Decimal("2"), Decimal("2")]


async def test_regroup_is_idempotent(conn):
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    await regroup_account(conn, acc)
    assert len(await list_trades(conn, acc)) == 1


async def test_regroup_preserves_user_authored_fields(conn):
    """The whole point of upserting instead of rebuilding. A routine re-import
    must never silently destroy hand-entered judgment."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)

    await conn.execute(
        """
        UPDATE trade SET notes = 'thesis: CPI hot', planned_risk = 50,
                         strategy_tag = 'orb', intent = 'trade'
         WHERE account_id = $1
        """,
        acc,
    )

    await regroup_account(conn, acc)

    t = (await list_trades(conn, acc))[0]
    assert t["notes"] == "thesis: CPI hot"
    assert t["planned_risk"] == Decimal("50")
    assert t["strategy_tag"] == "orb"
    assert t["realized_pnl"] == Decimal("20")          # derived value still refreshed
    assert t["r_multiple"] == Decimal("0.4")           # recomputed from planned_risk


async def test_regroup_keeps_the_same_trade_id_across_runs(conn):
    """A stable id is what lets subsystem B attach a thesis to a trade."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    first = (await list_trades(conn, acc))[0]["id"]
    await regroup_account(conn, acc)
    assert (await list_trades(conn, acc))[0]["id"] == first


async def test_appending_a_later_fill_updates_the_same_trade(conn):
    """Scaling into an open position must not create a second trade."""
    acc = await seed(conn, [(Side.BUY, "1", "100")])
    await regroup_account(conn, acc)
    original = (await list_trades(conn, acc))[0]["id"]

    inst = await conn.fetchval("SELECT id FROM instrument LIMIT 1")
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst,
                executed_at=T0 + timedelta(hours=2),
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("110"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="later",
                is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, acc)

    trades = await list_trades(conn, acc)
    assert len(trades) == 1
    assert trades[0]["id"] == original
    assert trades[0]["avg_entry"] == Decimal("105")


async def test_regroup_does_not_touch_manual_trades(conn):
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    await conn.execute(
        "UPDATE trade SET grouping_mode = 'manual', notes = 'keep me' WHERE account_id = $1",
        acc,
    )
    await regroup_account(conn, acc)
    trades = await list_trades(conn, acc)
    assert len(trades) == 1
    assert trades[0]["notes"] == "keep me"


async def test_intent_defaults_from_the_account(conn):
    acc = await create_account(
        conn, name="IRA", venue="manual", account_type="cash", default_intent="investment"
    )
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="VTI", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst,
                executed_at=T0,
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("250"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="x1",
                is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, acc)
    assert (await list_trades(conn, acc))[0]["intent"] == "investment"


async def test_mixed_account_leaves_intent_unassigned(conn):
    acc = await create_account(
        conn, name="Brokerage", venue="manual", account_type="cash", default_intent="mixed"
    )
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="AAPL", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst,
                executed_at=T0,
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("200"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="y1",
                is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, acc)
    assert (await list_trades(conn, acc))[0]["intent"] == "unassigned"
```

- [ ] **Step 2: Run to verify they fail**

Run: `TEST_PG_DSN=postgresql://localhost/deadband_test uv run pytest tests/db/test_fills.py tests/db/test_trades.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.fills'`

- [ ] **Step 3: Write `db/fills.py`**

```python
# db/fills.py
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg

from ledger.types import Fill, FillSource, Side


@dataclass(frozen=True, slots=True)
class InsertResult:
    inserted: int
    skipped: int


async def insert_fills(conn: asyncpg.Connection, fills: list[Fill]) -> InsertResult:
    """Insert fills idempotently. Duplicates by venue_fill_id or content_hash are
    skipped, which is what makes re-importing overlapping exports safe."""
    inserted = 0
    for f in fills:
        row = await conn.fetchval(
            """
            INSERT INTO fill (
                id, account_id, instrument_id, executed_at, side, quantity, price,
                fee, fee_currency, source, venue_order_id, venue_fill_id,
                content_hash, is_estimated
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            f.id,
            f.account_id,
            f.instrument_id,
            f.executed_at,
            f.side.value,
            f.quantity,
            f.price,
            f.fee,
            f.fee_currency,
            f.source.value,
            f.venue_order_id,
            f.venue_fill_id,
            f.content_hash,
            f.is_estimated,
        )
        if row is not None:
            inserted += 1
    return InsertResult(inserted=inserted, skipped=len(fills) - inserted)


def _to_fill(r: asyncpg.Record) -> Fill:
    return Fill(
        id=r["id"],
        account_id=r["account_id"],
        instrument_id=r["instrument_id"],
        executed_at=r["executed_at"],
        side=Side(r["side"]),
        quantity=r["quantity"],
        price=r["price"],
        fee=r["fee"],
        fee_currency=r["fee_currency"],
        source=FillSource(r["source"]),
        venue_order_id=r["venue_order_id"],
        venue_fill_id=r["venue_fill_id"],
        content_hash=r["content_hash"],
        is_estimated=r["is_estimated"],
    )


async def fetch_fills(
    conn: asyncpg.Connection, account_id: UUID | None = None
) -> list[Fill]:
    if account_id:
        rows = await conn.fetch(
            "SELECT * FROM fill WHERE account_id = $1 ORDER BY executed_at, id",
            account_id,
        )
    else:
        rows = await conn.fetch("SELECT * FROM fill ORDER BY executed_at, id")
    return [_to_fill(r) for r in rows]
```

- [ ] **Step 4: Write `db/trades.py`**

```python
# db/trades.py
"""Persist derived trades. The grouping and P&L logic itself lives in ledger/ —
this module only moves data between that pure core and Postgres."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from db.fills import fetch_fills
from db.instruments import get_multipliers
from ledger.grouping import group_fills
from ledger.pnl import compute_pnl
from ledger.types import TradeIntent


async def regroup_account(conn: asyncpg.Connection, account_id: UUID) -> int:
    """Recompute auto-grouped trades for an account. Manual groupings are permanent
    and are never touched (spec §5)."""
    default_intent = await conn.fetchval(
        "SELECT default_intent FROM account WHERE id = $1", account_id
    )
    intent = (
        TradeIntent.UNASSIGNED.value
        if default_intent == "mixed"
        else TradeIntent(default_intent).value
    )

    manual_fill_ids = {
        r["fill_id"]
        for r in await conn.fetch(
            """
            SELECT tf.fill_id FROM trade_fill tf
            JOIN trade t ON t.id = tf.trade_id
            WHERE t.account_id = $1 AND t.grouping_mode = 'manual'
            """,
            account_id,
        )
    }

    fills = [f for f in await fetch_fills(conn, account_id) if f.id not in manual_fill_ids]
    if not fills:
        return 0

    by_id = {f.id: f for f in fills}
    multipliers = await get_multipliers(conn, [f.instrument_id for f in fills])
    symbols = {
        r["id"]: r["symbol"]
        for r in await conn.fetch(
            "SELECT id, symbol, underlying FROM instrument WHERE id = ANY($1::uuid[])",
            list({f.instrument_id for f in fills}),
        )
    }

    groups = group_fills(fills)
    seen_openings: list[UUID] = []
    written = 0

    for g in groups:
        pnl = compute_pnl(g.allocations, by_id, multipliers, g.direction)
        # The opening allocation is this trade's stable identity across regroups.
        opening_fill_id = min(
            g.allocations, key=lambda a: (by_id[a.fill_id].executed_at, str(a.fill_id))
        ).fill_id
        seen_openings.append(opening_fill_id)

        # UPSERT, never delete-and-rebuild: derived columns are overwritten,
        # user-authored ones (intent override, planned_risk, strategy_tag, notes,
        # and B's thesis link) are left exactly as the user set them.
        trade_id = await conn.fetchval(
            """
            INSERT INTO trade (
                account_id, opening_fill_id, primary_underlying, direction, status,
                intent, grouping_mode, opened_at, closed_at, qty_opened, qty_closed,
                avg_entry, avg_exit, realized_pnl, gross_realized_pnl, fees_total
            ) VALUES ($1,$2,$3,$4,$5,$6,'auto',$7,$8,$9,$10,$11,$12,$13,$14,$15)
            ON CONFLICT (account_id, opening_fill_id) DO UPDATE SET
                primary_underlying = EXCLUDED.primary_underlying,
                direction          = EXCLUDED.direction,
                status             = EXCLUDED.status,
                opened_at          = EXCLUDED.opened_at,
                closed_at          = EXCLUDED.closed_at,
                qty_opened         = EXCLUDED.qty_opened,
                qty_closed         = EXCLUDED.qty_closed,
                avg_entry          = EXCLUDED.avg_entry,
                avg_exit           = EXCLUDED.avg_exit,
                realized_pnl       = EXCLUDED.realized_pnl,
                gross_realized_pnl = EXCLUDED.gross_realized_pnl,
                fees_total         = EXCLUDED.fees_total,
                updated_at         = now()
            RETURNING id
            """,
            account_id,
            opening_fill_id,
            symbols.get(g.instrument_ids[0]),
            g.direction.value,
            g.status.value,
            intent,
            g.opened_at,
            g.closed_at,
            pnl.qty_opened,
            pnl.qty_closed,
            pnl.avg_entry,
            pnl.avg_exit,
            pnl.realized_pnl,
            pnl.gross_realized_pnl,
            pnl.fees_total,
        )

        # r_multiple depends on planned_risk, which is user-authored — recompute it
        # from whatever risk the user has recorded rather than overwriting with NULL.
        await conn.execute(
            """
            UPDATE trade
               SET r_multiple = CASE
                     WHEN planned_risk IS NULL OR planned_risk = 0 THEN NULL
                     ELSE realized_pnl / planned_risk
                   END
             WHERE id = $1
            """,
            trade_id,
        )

        await conn.execute("DELETE FROM trade_fill WHERE trade_id = $1", trade_id)
        await conn.executemany(
            "INSERT INTO trade_fill (trade_id, fill_id, quantity) VALUES ($1,$2,$3)",
            [(trade_id, a.fill_id, a.quantity) for a in g.allocations],
        )
        written += 1

    # An auto trade whose opening fill no longer opens anything (a backdated fill
    # changed the grouping) is genuinely stale and is removed.
    await conn.execute(
        """
        DELETE FROM trade
         WHERE account_id = $1 AND grouping_mode = 'auto'
           AND (opening_fill_id IS NULL OR NOT (opening_fill_id = ANY($2::uuid[])))
        """,
        account_id,
        seen_openings,
    )

    return written


async def list_trades(
    conn: asyncpg.Connection, account_id: UUID | None = None
) -> list[asyncpg.Record]:
    if account_id:
        return await conn.fetch(
            "SELECT * FROM trade WHERE account_id = $1 ORDER BY opened_at DESC",
            account_id,
        )
    return await conn.fetch("SELECT * FROM trade ORDER BY opened_at DESC")
```

- [ ] **Step 5: Run to verify they pass**

Run: `TEST_PG_DSN=postgresql://localhost/deadband_test uv run pytest tests/db -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add db/fills.py db/trades.py tests/db/test_fills.py tests/db/test_trades.py
git commit -m "feat: idempotent fill persistence and the regroup pipeline"
```

---

### Task 11: Importer contract

**Files:**
- Create: `importers/base.py`, `importers/registry.py`
- Test: `tests/test_importer_base.py`

**Interfaces:**
- Consumes: `ledger.types`
- Produces: `CanonicalFill`, `CanonicalCash`, `ImportBatch(fills, cash, warnings)`,
  `content_hash(account_id, executed_at, symbol, side, quantity, price) -> str`,
  `Importer` protocol with `venue: str` and `parse(text: str) -> ImportBatch`,
  `registry.get_importer(name) -> Importer`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_importer_base.py
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from importers.base import content_hash
from importers.registry import get_importer, list_importers

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
T = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)


def test_hash_is_stable_across_calls():
    args = (ACC, T, "SPY", "buy", Decimal("10"), Decimal("500.00"))
    assert content_hash(*args) == content_hash(*args)


def test_hash_ignores_decimal_formatting():
    """500 and 500.00 are the same price; they must not defeat deduplication."""
    a = content_hash(ACC, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    b = content_hash(ACC, T, "SPY", "buy", Decimal("10.0"), Decimal("500.00"))
    assert a == b


def test_hash_changes_when_any_field_changes():
    base = content_hash(ACC, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    assert content_hash(ACC, T, "SPY", "sell", Decimal("10"), Decimal("500")) != base
    assert content_hash(ACC, T, "QQQ", "buy", Decimal("10"), Decimal("500")) != base
    assert content_hash(ACC, T, "SPY", "buy", Decimal("11"), Decimal("500")) != base


def test_hash_is_account_scoped():
    other = UUID("00000000-0000-0000-0000-0000000000a2")
    a = content_hash(ACC, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    b = content_hash(other, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    assert a != b


def test_registry_rejects_unknown_venue():
    with pytest.raises(KeyError, match="unknown importer"):
        get_importer("etrade")


def test_registry_lists_available_importers():
    assert set(list_importers()) >= {"coinbase", "fidelity"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_importer_base.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'importers.base'`

- [ ] **Step 3: Write `importers/base.py`**

```python
# importers/base.py
"""Canonical import types and the dedupe hash. Pure — no I/O, no clock.

Importers map venue rows to these types and never touch the database. Import is
three-phase — parse, preview, commit — so nothing is written before it is seen.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from ledger.types import Instrument, Side


def _canon(value: Decimal) -> str:
    """Render a Decimal so 10, 10.0 and 10.00 hash identically."""
    normalized = value.normalize()
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return str(normalized)


def content_hash(
    account_id: UUID,
    executed_at: datetime,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
) -> str:
    """Dedupe key for exports that carry no venue fill id.

    Account-scoped, so the same trade in two accounts is two fills.
    """
    payload = "|".join(
        [
            str(account_id),
            executed_at.astimezone(tz=executed_at.tzinfo).isoformat(),
            symbol.upper(),
            side.lower(),
            _canon(quantity),
            _canon(price),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalFill:
    instrument: Instrument
    executed_at: datetime
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    venue_fill_id: str | None = None
    venue_order_id: str | None = None
    external_ref: str | None = None       # venue's account number, for routing


@dataclass(frozen=True, slots=True)
class CanonicalCash:
    occurred_at: datetime
    kind: str
    amount: Decimal
    currency: str
    symbol: str | None = None
    venue_ref: str | None = None
    external_ref: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ImportBatch:
    fills: tuple[CanonicalFill, ...] = ()
    cash: tuple[CanonicalCash, ...] = ()
    warnings: tuple[str, ...] = ()
    unmapped_rows: tuple[str, ...] = ()


class Importer(Protocol):
    venue: str

    def parse(self, text: str) -> ImportBatch:
        """Map a venue export to canonical rows. Never writes anything."""
        ...
```

- [ ] **Step 4: Write `importers/registry.py`**

```python
# importers/registry.py
from __future__ import annotations

from importers.base import Importer
from importers.coinbase import CoinbaseImporter
from importers.fidelity import FidelityImporter

_IMPORTERS: dict[str, Importer] = {
    "coinbase": CoinbaseImporter(),
    "fidelity": FidelityImporter(),
}


def get_importer(name: str) -> Importer:
    try:
        return _IMPORTERS[name.lower()]
    except KeyError:
        raise KeyError(
            f"unknown importer {name!r}; available: {sorted(_IMPORTERS)}"
        ) from None


def list_importers() -> list[str]:
    return sorted(_IMPORTERS)
```

Note: `registry.py` imports the two importers written in Tasks 12 and 13. Write those
first if executing strictly in order, or stub them as empty classes and let the
registry test fail until Task 13 lands. The commit below assumes Tasks 12–13 are done.

- [ ] **Step 5: Run**

Run: `uv run pytest tests/test_importer_base.py -v`
Expected: PASS once Tasks 12 and 13 exist. The `content_hash` tests pass immediately.

- [ ] **Step 6: Commit**

```bash
git add importers/base.py importers/registry.py tests/test_importer_base.py
git commit -m "feat: canonical import types and account-scoped dedupe hash"
```

---

### Task 12: Coinbase importer

**Files:**
- Create: `importers/coinbase.py`, `tests/fixtures/coinbase/transactions.csv`
- Test: `tests/test_coinbase.py`

**Interfaces:**
- Consumes: `importers.base.{CanonicalFill, CanonicalCash, ImportBatch, Importer}`
- Produces: `CoinbaseImporter` with `venue = "coinbase"` and `parse(text) -> ImportBatch`

- [ ] **Step 1: Write the synthetic fixture**

Synthetic data only — never a real export.

```csv
Timestamp,Transaction Type,Asset,Quantity Transacted,Spot Price Currency,Spot Price at Transaction,Subtotal,Total (inclusive of fees and/or spread),Fees and/or Spread,Notes
2026-01-15T14:30:00Z,Buy,BTC,0.50000000,USD,61200.00,30600.00,30753.00,153.00,Bought 0.5 BTC
2026-01-16T09:05:00Z,Buy,BTC,0.50000000,USD,60800.00,30400.00,30552.00,152.00,Bought 0.5 BTC
2026-02-03T11:20:00Z,Sell,BTC,0.25000000,USD,68000.00,17000.00,16915.00,85.00,Sold 0.25 BTC
2026-02-10T00:00:00Z,Deposit,USD,5000.00,USD,1.00,5000.00,5000.00,0.00,Deposited from bank
2026-03-01T00:00:00Z,Rewards Income,ETH,0.01000000,USD,3200.00,32.00,32.00,0.00,Staking reward
2026-03-15T16:45:00Z,Convert,BTC,0.10000000,USD,70000.00,7000.00,7000.00,0.00,Converted to ETH
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_coinbase.py
import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from importers.coinbase import CoinbaseImporter
from ledger.types import AssetClass, Side

FIXTURE = pathlib.Path("tests/fixtures/coinbase/transactions.csv").read_text()


def batch():
    return CoinbaseImporter().parse(FIXTURE)


def test_buys_and_sells_become_fills():
    fills = batch().fills
    assert len(fills) == 3
    assert [f.side for f in fills] == [Side.BUY, Side.BUY, Side.SELL]


def test_fill_fields_are_mapped():
    f = batch().fills[0]
    assert f.executed_at == datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
    assert f.quantity == Decimal("0.50000000")
    assert f.price == Decimal("61200.00")
    assert f.fee == Decimal("153.00")
    assert f.fee_currency == "USD"
    assert f.instrument.asset_class is AssetClass.CRYPTO_SPOT
    assert f.instrument.symbol == "BTC"
    assert f.instrument.quote_currency == "USD"


def test_deposits_become_cash_movements():
    cash = [c for c in batch().cash if c.kind == "deposit"]
    assert len(cash) == 1
    assert cash[0].amount == Decimal("5000.00")


def test_rewards_become_interest_cash_movements():
    cash = [c for c in batch().cash if c.kind == "interest"]
    assert len(cash) == 1
    assert cash[0].symbol == "ETH"


def test_unhandled_row_types_are_reported_not_silently_dropped():
    result = batch()
    assert any("Convert" in w for w in result.warnings)
    assert len(result.unmapped_rows) == 1


def test_empty_input_yields_empty_batch():
    result = CoinbaseImporter().parse("")
    assert result.fills == ()
    assert result.cash == ()


def test_header_only_input_yields_empty_batch():
    header = FIXTURE.splitlines()[0]
    assert CoinbaseImporter().parse(header + "\n").fills == ()


def test_malformed_row_is_warned_about_and_skipped():
    bad = FIXTURE.splitlines()[0] + "\n2026-01-15T14:30:00Z,Buy,BTC,notanumber,USD,1,1,1,0,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert len(result.warnings) == 1
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_coinbase.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'importers.coinbase'`

- [ ] **Step 4: Write the implementation**

```python
# importers/coinbase.py
"""Coinbase transaction CSV → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation

from importers.base import CanonicalCash, CanonicalFill, ImportBatch
from ledger.types import AssetClass, Instrument, Side

_FILL_TYPES = {
    "buy": Side.BUY,
    "advanced trade buy": Side.BUY,
    "sell": Side.SELL,
    "advanced trade sell": Side.SELL,
}

_CASH_TYPES = {
    "deposit": "deposit",
    "withdrawal": "withdrawal",
    "rewards income": "interest",
    "staking income": "interest",
    "inflation reward": "interest",
    "interest": "interest",
}


def _decimal(raw: str) -> Decimal:
    return Decimal((raw or "0").replace("$", "").replace(",", "").strip() or "0")


class CoinbaseImporter:
    venue = "coinbase"

    def parse(self, text: str) -> ImportBatch:
        fills: list[CanonicalFill] = []
        cash: list[CanonicalCash] = []
        warnings: list[str] = []
        unmapped: list[str] = []

        if not text.strip():
            return ImportBatch()

        reader = csv.DictReader(io.StringIO(text))
        for line_no, row in enumerate(reader, start=2):
            kind = (row.get("Transaction Type") or "").strip().lower()
            asset = (row.get("Asset") or "").strip().upper()
            currency = (row.get("Spot Price Currency") or "USD").strip().upper()

            try:
                when = datetime.fromisoformat(
                    (row.get("Timestamp") or "").replace("Z", "+00:00")
                )
                quantity = _decimal(row.get("Quantity Transacted", ""))
                price = _decimal(row.get("Spot Price at Transaction", ""))
                fee = _decimal(row.get("Fees and/or Spread", ""))
            except (ValueError, InvalidOperation) as exc:
                warnings.append(f"line {line_no}: could not parse row ({exc})")
                unmapped.append(str(row))
                continue

            if kind in _FILL_TYPES:
                fills.append(
                    CanonicalFill(
                        instrument=Instrument(
                            id=None,
                            asset_class=AssetClass.CRYPTO_SPOT,
                            symbol=asset,
                            quote_currency=currency,
                        ),
                        executed_at=when,
                        side=_FILL_TYPES[kind],
                        quantity=quantity,
                        price=price,
                        fee=fee,
                        fee_currency=currency,
                    )
                )
            elif kind in _CASH_TYPES:
                cash.append(
                    CanonicalCash(
                        occurred_at=when,
                        kind=_CASH_TYPES[kind],
                        amount=quantity if asset == currency else quantity * price,
                        currency=currency,
                        symbol=None if asset == currency else asset,
                        note=(row.get("Notes") or "").strip() or None,
                    )
                )
            else:
                # Never drop a row silently — an unrecognized type is a reporting gap.
                warnings.append(
                    f"line {line_no}: unhandled transaction type "
                    f"{row.get('Transaction Type')!r}"
                )
                unmapped.append(str(row))

        return ImportBatch(
            fills=tuple(fills),
            cash=tuple(cash),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
        )
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_coinbase.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Commit**

```bash
git add importers/coinbase.py tests/test_coinbase.py tests/fixtures/coinbase/
git commit -m "feat: Coinbase CSV importer with explicit unmapped-row reporting"
```

---

### Task 13: Fidelity importer

Fidelity is the harder one: equities and multi-leg options in one file, with option
contracts encoded in a description string.

**Files:**
- Create: `importers/fidelity.py`, `tests/fixtures/fidelity/activity.csv`
- Test: `tests/test_fidelity.py`

**Interfaces:**
- Consumes: `importers.base.*`, `ledger.types.Instrument`
- Produces: `FidelityImporter` with `venue = "fidelity"`, `parse(text) -> ImportBatch`,
  and `parse_option_symbol(text) -> Instrument | None`

- [ ] **Step 1: Write the synthetic fixture**

```csv
Run Date,Account,Action,Symbol,Description,Quantity,Price,Commission,Fees,Amount
01/15/2026,X12345678,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00
02/20/2026,X12345678,YOU SOLD,SPY,SPDR S&P 500 ETF TRUST,-10,520.00,0.00,0.03,5199.97
03/05/2026,X12345678,YOU BOUGHT OPENING TRANSACTION,-SPY260919C500,CALL (SPY) SPDR S&P 500 SEP 19 26 $500,2,3.50,1.30,0.10,-701.40
03/20/2026,X12345678,YOU SOLD CLOSING TRANSACTION,-SPY260919C500,CALL (SPY) SPDR S&P 500 SEP 19 26 $500,-2,5.75,1.30,0.10,1148.60
04/01/2026,X12345678,DIVIDEND RECEIVED,SPY,SPDR S&P 500 ETF TRUST,,,,,42.15
04/15/2026,X87654321,YOU BOUGHT,VTI,VANGUARD TOTAL STOCK MARKET ETF,5,250.00,0.00,0.00,-1250.00
05/01/2026,X12345678,ELECTRONIC FUNDS TRANSFER RECEIVED,,,,,,,2000.00
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_fidelity.py
import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from importers.fidelity import FidelityImporter, parse_option_symbol
from ledger.types import AssetClass, Side

FIXTURE = pathlib.Path("tests/fixtures/fidelity/activity.csv").read_text()


def batch():
    return FidelityImporter().parse(FIXTURE)


def test_equity_buy_is_mapped():
    f = batch().fills[0]
    assert f.side is Side.BUY
    assert f.instrument.symbol == "SPY"
    assert f.instrument.asset_class is AssetClass.EQUITY
    assert f.quantity == Decimal("10")
    assert f.price == Decimal("500.00")
    assert f.executed_at == datetime(2026, 1, 15, tzinfo=UTC)


def test_negative_quantity_becomes_a_sell_with_positive_quantity():
    """Fidelity signs quantity; the ledger never stores a negative quantity."""
    f = batch().fills[1]
    assert f.side is Side.SELL
    assert f.quantity == Decimal("10")


def test_commission_and_fees_are_summed():
    f = batch().fills[2]
    assert f.fee == Decimal("1.40")


def test_option_symbol_is_parsed_into_contract_terms():
    inst = parse_option_symbol("-SPY260919C500")
    assert inst is not None
    assert inst.asset_class is AssetClass.OPTION
    assert inst.underlying == "SPY"
    assert inst.expiry == datetime(2026, 9, 19, tzinfo=UTC).date()
    assert inst.option_right == "call"
    assert inst.strike == Decimal("500")
    assert inst.contract_multiplier == Decimal("100")


def test_put_option_symbol_is_parsed():
    inst = parse_option_symbol("-QQQ261218P400.5")
    assert inst is not None
    assert inst.option_right == "put"
    assert inst.strike == Decimal("400.5")


def test_non_option_symbol_returns_none():
    assert parse_option_symbol("SPY") is None


def test_option_fills_use_the_option_instrument():
    opt_fills = [f for f in batch().fills if f.instrument.asset_class is AssetClass.OPTION]
    assert len(opt_fills) == 2
    assert opt_fills[0].instrument.contract_multiplier == Decimal("100")


def test_dividend_becomes_an_attributed_cash_movement():
    dividends = [c for c in batch().cash if c.kind == "dividend"]
    assert len(dividends) == 1
    assert dividends[0].amount == Decimal("42.15")
    assert dividends[0].symbol == "SPY"


def test_transfer_becomes_a_deposit():
    deposits = [c for c in batch().cash if c.kind == "deposit"]
    assert len(deposits) == 1
    assert deposits[0].amount == Decimal("2000.00")


def test_account_number_is_carried_for_routing():
    """A venue with several accounts must route rows to the right one."""
    refs = {f.external_ref for f in batch().fills}
    assert refs == {"X12345678", "X87654321"}
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_fidelity.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'importers.fidelity'`

- [ ] **Step 4: Write the implementation**

```python
# importers/fidelity.py
"""Fidelity account-activity CSV → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from importers.base import CanonicalCash, CanonicalFill, ImportBatch
from ledger.types import AssetClass, Instrument, Side

# -SPY260919C500  →  underlying SPY, 2026-09-19, call, strike 500
_OPTION_RE = re.compile(
    r"^-(?P<underlying>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<right>[CP])(?P<strike>\d+(?:\.\d+)?)$"
)

_CASH_ACTIONS = {
    "DIVIDEND RECEIVED": "dividend",
    "ELECTRONIC FUNDS TRANSFER RECEIVED": "deposit",
    "ELECTRONIC FUNDS TRANSFER PAID": "withdrawal",
    "INTEREST EARNED": "interest",
}


def parse_option_symbol(symbol: str) -> Instrument | None:
    """Parse Fidelity's option symbol. Returns None for anything that isn't one."""
    match = _OPTION_RE.match((symbol or "").strip().upper())
    if not match:
        return None
    g = match.groupdict()
    expiry = datetime(2000 + int(g["yy"]), int(g["mm"]), int(g["dd"]), tzinfo=UTC).date()
    return Instrument(
        id=None,
        asset_class=AssetClass.OPTION,
        symbol=symbol.strip().upper(),
        quote_currency="USD",
        underlying=g["underlying"],
        strike=Decimal(g["strike"]),
        expiry=expiry,
        option_right="call" if g["right"] == "C" else "put",
        contract_multiplier=Decimal("100"),
    )


def _decimal(raw: str | None) -> Decimal:
    cleaned = (raw or "").replace("$", "").replace(",", "").strip()
    return Decimal(cleaned) if cleaned else Decimal("0")


class FidelityImporter:
    venue = "fidelity"

    def parse(self, text: str) -> ImportBatch:
        fills: list[CanonicalFill] = []
        cash: list[CanonicalCash] = []
        warnings: list[str] = []
        unmapped: list[str] = []

        if not text.strip():
            return ImportBatch()

        reader = csv.DictReader(io.StringIO(text))
        for line_no, row in enumerate(reader, start=2):
            action = (row.get("Action") or "").strip().upper()
            symbol = (row.get("Symbol") or "").strip()
            account = (row.get("Account") or "").strip() or None

            try:
                when = datetime.strptime(
                    (row.get("Run Date") or "").strip(), "%m/%d/%Y"
                ).replace(tzinfo=UTC)
            except ValueError as exc:
                warnings.append(f"line {line_no}: bad date ({exc})")
                unmapped.append(str(row))
                continue

            cash_kind = next((v for k, v in _CASH_ACTIONS.items() if action.startswith(k)), None)
            if cash_kind:
                cash.append(
                    CanonicalCash(
                        occurred_at=when,
                        kind=cash_kind,
                        amount=_decimal(row.get("Amount")),
                        currency="USD",
                        symbol=symbol or None,
                        external_ref=account,
                        note=(row.get("Description") or "").strip() or None,
                    )
                )
                continue

            if "BOUGHT" not in action and "SOLD" not in action:
                warnings.append(f"line {line_no}: unhandled action {action!r}")
                unmapped.append(str(row))
                continue

            try:
                raw_qty = _decimal(row.get("Quantity"))
                price = _decimal(row.get("Price"))
                fee = _decimal(row.get("Commission")) + _decimal(row.get("Fees"))
            except InvalidOperation as exc:
                warnings.append(f"line {line_no}: bad number ({exc})")
                unmapped.append(str(row))
                continue

            if raw_qty == 0:
                warnings.append(f"line {line_no}: zero quantity, skipped")
                unmapped.append(str(row))
                continue

            instrument = parse_option_symbol(symbol) or Instrument(
                id=None,
                asset_class=AssetClass.EQUITY,
                symbol=symbol.upper(),
                quote_currency="USD",
            )

            fills.append(
                CanonicalFill(
                    instrument=instrument,
                    executed_at=when,
                    # Direction comes from the action, not the sign — "SOLD" is
                    # authoritative and the sign is corroboration.
                    side=Side.SELL if "SOLD" in action else Side.BUY,
                    quantity=abs(raw_qty),
                    price=price,
                    fee=fee,
                    fee_currency="USD",
                    external_ref=account,
                )
            )

        return ImportBatch(
            fills=tuple(fills),
            cash=tuple(cash),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
        )
```

- [ ] **Step 5: Run to verify it passes**

Run: `uv run pytest tests/ -v`
Expected: PASS (all non-DB tests; DB tests skip without `TEST_PG_DSN`)

- [ ] **Step 6: Commit**

```bash
git add importers/fidelity.py tests/test_fidelity.py tests/fixtures/fidelity/
git commit -m "feat: Fidelity importer with option symbol parsing"
```

---

### Task 14: CLI

Makes the ledger usable and provable before any web layer exists.

**Files:**
- Create: `cli.py`, `db/importing.py`
- Test: `tests/db/test_importing.py`

**Interfaces:**
- Consumes: everything above
- Produces: `db.importing.commit_batch(conn, account_id, batch, source) -> CommitResult`,
  and the commands `import`, `regroup`, `positions`, `reconcile`

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_importing.py
from datetime import UTC, datetime
from decimal import Decimal

from db.accounts import create_account
from db.importing import commit_batch
from importers.base import CanonicalFill, ImportBatch
from ledger.types import AssetClass, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db


def batch_of(n: int) -> ImportBatch:
    return ImportBatch(
        fills=tuple(
            CanonicalFill(
                instrument=Instrument(
                    id=None,
                    asset_class=AssetClass.EQUITY,
                    symbol="SPY",
                    quote_currency="USD",
                ),
                executed_at=datetime(2026, 1, 15 + i, 14, 30, tzinfo=UTC),
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("500"),
                fee=Decimal("0"),
                fee_currency="USD",
            )
            for i in range(n)
        )
    )


async def test_commit_inserts_fills_and_creates_instruments(conn):
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    result = await commit_batch(conn, acc, batch_of(2), source="csv")
    assert result.fills_inserted == 2
    assert await conn.fetchval("SELECT count(*) FROM instrument") == 1


async def test_recommitting_the_same_batch_inserts_nothing(conn):
    """Re-importing an overlapping export must not duplicate history."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, batch_of(2), source="csv")
    result = await commit_batch(conn, acc, batch_of(2), source="csv")
    assert result.fills_inserted == 0
    assert result.fills_skipped == 2
    assert await conn.fetchval("SELECT count(*) FROM fill") == 2


async def test_overlapping_batch_inserts_only_the_new_rows(conn):
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, batch_of(2), source="csv")
    result = await commit_batch(conn, acc, batch_of(3), source="csv")
    assert result.fills_inserted == 1
    assert await conn.fetchval("SELECT count(*) FROM fill") == 3
```

- [ ] **Step 2: Run to verify it fails**

Run: `TEST_PG_DSN=postgresql://localhost/deadband_test uv run pytest tests/db/test_importing.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db.importing'`

- [ ] **Step 3: Write `db/importing.py`**

```python
# db/importing.py
"""Commit a parsed import batch. The parse and preview phases are pure and live in
importers/; this is the only phase that writes."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import asyncpg

from db.fills import insert_fills
from db.instruments import upsert_instrument
from importers.base import ImportBatch, content_hash
from ledger.types import Fill, FillSource


@dataclass(frozen=True, slots=True)
class CommitResult:
    fills_inserted: int
    fills_skipped: int
    cash_inserted: int
    warnings: tuple[str, ...]


async def commit_batch(
    conn: asyncpg.Connection,
    account_id: UUID,
    batch: ImportBatch,
    source: str = "csv",
) -> CommitResult:
    fills: list[Fill] = []

    for cf in batch.fills:
        instrument_id = await upsert_instrument(conn, cf.instrument)
        fills.append(
            Fill(
                id=uuid4(),
                account_id=account_id,
                instrument_id=instrument_id,
                executed_at=cf.executed_at,
                side=cf.side,
                quantity=cf.quantity,
                price=cf.price,
                fee=cf.fee,
                fee_currency=cf.fee_currency,
                source=FillSource(source),
                venue_order_id=cf.venue_order_id,
                venue_fill_id=cf.venue_fill_id,
                content_hash=(
                    None
                    if cf.venue_fill_id
                    else content_hash(
                        account_id,
                        cf.executed_at,
                        cf.instrument.symbol,
                        cf.side.value,
                        cf.quantity,
                        cf.price,
                    )
                ),
                is_estimated=False,
            )
        )

    fill_result = await insert_fills(conn, fills)

    cash_inserted = 0
    for c in batch.cash:
        instrument_id = None
        row = await conn.fetchval(
            """
            INSERT INTO cash_movement (account_id, occurred_at, kind, amount,
                                       currency, instrument_id, venue_ref,
                                       content_hash, note)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT DO NOTHING
            RETURNING id
            """,
            account_id,
            c.occurred_at,
            c.kind,
            c.amount,
            c.currency,
            instrument_id,
            c.venue_ref,
            content_hash(
                account_id, c.occurred_at, c.symbol or c.kind, c.kind, c.amount, Decimal(0)
            ),
            c.note,
        )
        if row is not None:
            cash_inserted += 1

    return CommitResult(
        fills_inserted=fill_result.inserted,
        fills_skipped=fill_result.skipped,
        cash_inserted=cash_inserted,
        warnings=batch.warnings,
    )
```

Add `from decimal import Decimal` to the imports at the top of that file.

- [ ] **Step 4: Write `cli.py`**

```python
# cli.py
"""Deadband CLI. Preview before commit, always."""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from uuid import UUID

from db.accounts import find_by_external_ref, list_accounts
from db.importing import commit_batch
from db.pool import create_pool
from db.trades import list_trades, regroup_account
from importers.registry import get_importer, list_importers


async def cmd_accounts(_args) -> int:
    pool = await create_pool()
    async with pool.acquire() as conn:
        for a in await list_accounts(conn):
            print(f"{a['id']}  {a['venue']:<10} {a['name']:<24} {a['external_ref'] or '-'}")
    await pool.close()
    return 0


async def cmd_import(args) -> int:
    importer = get_importer(args.venue)
    batch = importer.parse(pathlib.Path(args.file).read_text())

    print(f"parsed {len(batch.fills)} fills, {len(batch.cash)} cash movements")
    for w in batch.warnings:
        print(f"  warning: {w}", file=sys.stderr)
    if batch.unmapped_rows:
        print(f"  {len(batch.unmapped_rows)} row(s) not mapped", file=sys.stderr)

    if not args.commit:
        print("\npreview only — rerun with --commit to write")
        return 0

    pool = await create_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            account_id = UUID(args.account)
            result = await commit_batch(conn, account_id, batch)
            written = await regroup_account(conn, account_id)
    await pool.close()

    print(
        f"inserted {result.fills_inserted} fills "
        f"({result.fills_skipped} already present), "
        f"{result.cash_inserted} cash movements, {written} trades regrouped"
    )
    return 0


async def cmd_regroup(args) -> int:
    pool = await create_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            written = await regroup_account(conn, UUID(args.account))
    await pool.close()
    print(f"{written} trades")
    return 0


async def cmd_trades(args) -> int:
    pool = await create_pool()
    async with pool.acquire() as conn:
        rows = await list_trades(conn, UUID(args.account) if args.account else None)
    await pool.close()
    for t in rows:
        print(
            f"{t['opened_at']:%Y-%m-%d}  {t['primary_underlying'] or '?':<8} "
            f"{t['direction']:<6} {t['status']:<6} "
            f"pnl={t['realized_pnl'] or 0:>12}  intent={t['intent']}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="deadband")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("accounts").set_defaults(fn=cmd_accounts)

    p_import = sub.add_parser("import", help="parse a venue export")
    p_import.add_argument("venue", choices=list_importers())
    p_import.add_argument("file")
    p_import.add_argument("--account", help="account UUID (required with --commit)")
    p_import.add_argument("--commit", action="store_true", help="write to the database")
    p_import.set_defaults(fn=cmd_import)

    p_regroup = sub.add_parser("regroup")
    p_regroup.add_argument("--account", required=True)
    p_regroup.set_defaults(fn=cmd_regroup)

    p_trades = sub.add_parser("trades")
    p_trades.add_argument("--account")
    p_trades.set_defaults(fn=cmd_trades)

    args = parser.parse_args()
    if getattr(args, "commit", False) and not args.account:
        parser.error("--commit requires --account")
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Verify end to end**

```bash
TEST_PG_DSN=postgresql://localhost/deadband_test uv run pytest tests/ -v
uv run python cli.py import coinbase tests/fixtures/coinbase/transactions.csv
```

Expected: all tests pass; the import prints 3 fills, 2 cash movements, and a warning
about the unhandled `Convert` row, writing nothing without `--commit`.

- [ ] **Step 6: Commit**

```bash
git add cli.py db/importing.py tests/db/test_importing.py
git commit -m "feat: import commit phase and CLI"
```

---

## Self-Review

**Spec coverage.** Walked every section of the design spec against these tasks:

| Spec section | Covered by |
|---|---|
| §4 `account`, `funded_account_rule` | Task 8 (schema), Task 9 (repository) |
| §4 `instrument` + natural keys | Tasks 2, 8, 9 |
| §4 `fill`, opening balances | Tasks 2, 8, 10 |
| §4 `trade`, `trade_fill` allocations | Tasks 3, 8, 10 |
| §4 `cash_movement`, `mark`, `corporate_action`, `account_snapshot` | Task 8 (schema), Task 14 (cash writes) |
| §5 grouping, manual override | Tasks 3, 4, 10 |
| §6 corporate actions | Task 6 |
| §7 import pipeline, dedupe | Tasks 11, 12, 13, 14 |
| §9 testing discipline | Tasks 1, 4, and every task's TDD cycle |

**Known gaps, deliberate and deferred to A-2:** `funded_account_rule` state evaluation,
`mark` and `account_snapshot` write paths, and wiring `ledger/reconcile.py` to the
database are all read/write surface that the API layer needs. The pure logic and the
schema for all three exist and are tested here; only the plumbing waits. Task 14's
`reconcile` command is named in the interfaces block but not implemented — it needs the
snapshot write path, so it moves to A-2. **This is the one place the plan knowingly
under-delivers against its own task list, and it is called out rather than hidden.**

**Placeholder scan.** No TBDs. Every code step contains runnable code.

**Type consistency.** `FillAllocation`, `TradeGroup`, `TradePnL`, `CanonicalFill`,
`ImportBatch`, and `CommitResult` are defined once and referenced with matching field
names throughout. `compute_pnl` takes `(allocations, fills_by_id, multipliers, direction)`
in Tasks 5 and 10 alike. `content_hash` takes the same six arguments in Tasks 11 and 14.

**One risk worth naming:** Task 11's `registry.py` imports modules created in Tasks 12
and 13. Execute 12 and 13 before 11, or accept a failing import until they land. The
task order here is written for reading, not strictly for execution.
