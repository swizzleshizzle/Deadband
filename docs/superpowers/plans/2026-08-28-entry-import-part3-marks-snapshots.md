# Entry & Import part 3 -- marks and statement snapshots: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the UI the two write forms that make the Dashboard's equity, unrealized P&L and drift tiles show numbers instead of dashes -- a bulk price-mark table over the instruments the ledger actually holds, and a per-account statement snapshot form.

**Architecture:** Four new HTTP routes across two thin modules (`api/marks.py`, `api/snapshots.py`) that call the already-shipped `db/marks.py` and `db/snapshots.py` functions -- the same functions `cli.py`'s `marks set` and `snapshot add` call, so there is one write path and one place decisions live (spec E6). Two new React screens hang off the existing `Entry` segmented control, taking it from three segments to five. No new nav entry, no new client route.

**Tech Stack:** FastAPI + asyncpg + Pydantic (backend), React 19 + react-router 7 + TypeScript (frontend), pytest/pytest-asyncio (tests), `uv` for Python and `pnpm` for JS.

**Spec:** `docs/superpowers/specs/2026-08-24-entry-import-design.md` -- section 4 ("Marks") names this work; E3 scopes it into this milestone; E6 fixes the CLI-first structure; section 6 fixes the identity rules.

## Global Constraints

These apply to every task below without being restated in each.

- **Money and quantities are STRINGS end to end, never `float`** (spec section 5 / D4). Parse straight to `Decimal`. Every new API test asserts `assert_no_json_floats(body)` from `tests/api/conftest.py`.
- **`require_trusted_identity` is declared BEFORE `get_write_conn`** on every write route. FastAPI resolves dependencies in declaration order; the reverse order lets a 403-bound request check out a write-pool connection on every attempt. This was a review finding on `api/fills.py`, not a style preference.
- **`request.client.host` and `X-Forwarded-For` are NOT identity and must not be read anywhere** (spec section 6, `api/identity.py`). The deployment proxies every path, so `request.client.host` reads as localhost for every remote caller.
- **Write routes exist only under `enable_writes`** (`api/app.py`). Both new routers are registered inside that `if enable_writes:` block.
- **Invented logins only, `.invalid` TLD.** This repo is public and a pre-commit hook enforces a deny-list on real tailnet logins.
- **API tests carry `pytestmark = requires_db`** from `tests/conftest.py`, and DB-lane tests must never skip -- CI fails the run if any does.
- Commit style follows the log: `feat(api): ...`, `feat(web): ...`, `test(api): ...`, `refactor: ...`.
- **Every pytest command is `uv run --env-file .env pytest ...`.** `TEST_PG_DSN` is not in
  the shell environment on this box and there is no `.envrc` or pytest-dotenv. A bare
  `uv run pytest` silently skips the whole DB and API lane — 406 tests — and reports
  green. That is this repo's documented worst failure mode; CI carries a step that
  asserts `skipped == 0` precisely because of it. Any test result reported in this plan
  that shows skips in the DB or API lane is a failed run, not a pass.
- **`uv sync --extra dev`**, not `uv sync` — the latter does not install pytest.
- Run tests in the **foreground** and let them block, with ONE exception: the full
  `tests/db/` lane measured 616.76s against the Bash tool's 600000 ms ceiling and will be
  auto-backgrounded. Run that lane per-file instead (see Task 9). Everything else fits. A 600000 ms tool timeout covers every command in this plan; piped output buffers, so an empty output file mid-run is expected and is not evidence of a hang. Do not background any test run.

## What already exists (do not rebuild)

Verified against the tree at `dad7fff`:

- `db/marks.py`: `set_mark(conn, instrument_id, price, as_of, source='manual')`, `latest_marks(conn, instrument_ids) -> dict[UUID, tuple[Decimal, datetime]]`, `resolve_instrument_by_symbol`.
- `db/snapshots.py`: `add_snapshot(conn, account_id, as_of, *, cash_balance, total_equity, source='statement', note=None)` -- the `*` is load-bearing, see its docstring -- and `latest_snapshot(conn, account_id, as_of=None)`.
- `cli.py`: `cmd_marks_set`, `cmd_snapshot_add`, `_parse_as_of`, `_MARK_FUTURE_TOLERANCE`.
- `tests/api/test_write_pool.py` and `tests/api/test_write_identity.py` walk the route table via `iter_route_contexts`, so **both new POSTs are picked up by the structural guards automatically**. The over-HTTP half of the identity guard is hand-enumerated in `_WRITE_REQUESTS` and must be extended by hand (Tasks 3 and 5).

## Schema facts the validation depends on

Read from `db/schema.sql` -- do not re-derive these from memory:

- `mark`: `PRIMARY KEY (instrument_id, as_of)`; `price NUMERIC NOT NULL CONSTRAINT mark_price_chk CHECK (price >= 0 AND price < 'Infinity'::numeric)`. **Zero is a legal mark; negative is not.** A negative price must become a 422, not an uncaught `CheckViolationError` surfacing as a 500.
- `account_snapshot`: `UNIQUE (account_id, as_of)`, `cash_balance NUMERIC NOT NULL`, `total_equity NUMERIC NOT NULL`, and **no CHECK constraints at all**. Negative cash is legal (a margin debit) and must be accepted. But Postgres `NUMERIC` accepts `'NaN'`, so the `is_finite()` guard is the only thing standing between a typo and a stored NaN -- it is load-bearing here in a way it is not for `mark`, whose `< 'Infinity'` check already rejects NaN (a NUMERIC NaN sorts above all values, so the comparison is false).

## File Structure

**Create:**
- `api/validation.py` -- shared request-value parsing: `parse_decimal`, `parse_instant`, `parse_as_of`, `refuse_future`. One home so the API cannot drift from itself the way `snapshot add` and `reconcile` drifted apart before `_parse_as_of` unified them.
- `api/marks.py` -- `GET /api/marks`, `POST /api/marks`.
- `api/snapshots.py` -- `GET /api/accounts/{account_id}/snapshot`, `POST /api/snapshots`.
- `tests/api/test_marks.py`, `tests/api/test_snapshots.py`.
- `web/src/datetime.ts` -- `toInstant`, moved out of `Entry.tsx`.
- `web/src/screens/Marks.tsx`, `web/src/screens/Snapshot.tsx`.

**Modify:**
- `db/marks.py` -- gains the `MARK_FUTURE_TOLERANCE` constant.
- `cli.py` -- imports that constant instead of declaring `_MARK_FUTURE_TOLERANCE`.
- `api/fills.py` -- its private `_decimal` gives way to `api/validation.parse_decimal`.
- `api/app.py` -- registers both routers inside `if enable_writes:`.
- `tests/api/test_write_identity.py` -- `_WRITE_REQUESTS` gains the two new POSTs.
- `web/src/api.ts` -- types and client functions.
- `web/src/screens/Entry.tsx` -- `toInstant` moves out; two segments added.
- `docs/known-gaps.md` -- records the as-of asymmetry (Task 9).

**Why two screen files rather than more of `Entry.tsx`:** `Entry.tsx` is already 812 lines and holds three unrelated flows. Two more inline would make it the largest file in the repo by a wide margin and harder to edit reliably. `Entry.tsx` keeps ownership of the segmented control and renders the new components.

---

### Task 1: Shared validation, and one home for the future-mark tolerance

**Files:**
- Create: `api/validation.py`
- Create: `tests/api/test_validation.py`
- Modify: `db/marks.py` (add constant), `cli.py:1440` (import it), `api/fills.py` (use `parse_decimal`)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `db.marks.MARK_FUTURE_TOLERANCE: timedelta` (2 minutes)
  - `api.validation.parse_decimal(raw: str, field: str) -> Decimal` -- raises `HTTPException(422)`
  - `api.validation.parse_instant(raw: str, field: str) -> datetime` -- tz-aware required, raises `HTTPException(422)`
  - `api.validation.parse_as_of(raw: str, field: str) -> datetime` -- bare date becomes midnight UTC, timestamp taken as written, naive timestamp refused
  - `api.validation.refuse_future(value: datetime, now: datetime, field: str) -> None`

**Why the constant moves:** `cli.py` and the two new API modules all need the same tolerance. `_parse_as_of`'s own docstring records what happened last time near-identical copies existed -- `snapshot add` and `reconcile` drifted, and the README's documented two-line invocation exited 2 on its second line. `db/marks.py` is the right home: it is the marks module, and a `timedelta` is a duration, not a clock, so the file stays clock-free as its design requires.

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_validation.py`:

```python
"""Shared request-value parsing (api/validation.py). All values invented.

No `requires_db` marker: nothing here touches a database, so these belong to
the pure lane and must run even when TEST_PG_DSN is unset."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from api.validation import parse_as_of, parse_decimal, parse_instant, refuse_future
from db.marks import MARK_FUTURE_TOLERANCE


def test_parse_decimal_accepts_an_exact_string():
    assert parse_decimal("238.90", "price") == Decimal("238.90")


def test_parse_decimal_refuses_a_non_number():
    # Decimal("abc") raises InvalidOperation, which does NOT descend from
    # ValueError -- a bare `except ValueError` would let it crash through as
    # a 500 instead of becoming a clean 422.
    with pytest.raises(HTTPException) as exc:
        parse_decimal("abc", "price")
    assert exc.value.status_code == 422
    assert "price" in exc.value.detail


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_parse_decimal_refuses_non_finite(raw):
    # These CONSTRUCT successfully and slip past the InvalidOperation catch
    # entirely. account_snapshot has no CHECK constraints, so nothing below
    # this line would stop a stored NaN.
    with pytest.raises(HTTPException) as exc:
        parse_decimal(raw, "cash_balance")
    assert exc.value.status_code == 422


def test_parse_instant_requires_an_offset():
    with pytest.raises(HTTPException) as exc:
        parse_instant("2026-08-28T14:02:00", "as_of")
    assert exc.value.status_code == 422
    assert "offset" in exc.value.detail


def test_parse_instant_accepts_zulu():
    assert parse_instant("2026-08-28T14:02:00Z", "as_of") == datetime(
        2026, 8, 28, 14, 2, tzinfo=UTC
    )


def test_parse_as_of_turns_a_bare_date_into_midnight_utc():
    # Matches cli.py's _parse_as_of exactly: `snapshot add` accepts the bare
    # date the README's worked example passes.
    assert parse_as_of("2026-07-31", "as_of") == datetime(2026, 7, 31, tzinfo=UTC)


def test_parse_as_of_refuses_a_timestamp_without_an_offset():
    # date.fromisoformat rejects anything carrying a time component, so this
    # falls through to the timestamp branch and hits the tz guard rather than
    # being silently swallowed as a date.
    with pytest.raises(HTTPException) as exc:
        parse_as_of("2026-07-31T12:00", "as_of")
    assert exc.value.status_code == 422


def test_refuse_future_allows_now():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    refuse_future(now, now, "as_of")


def test_refuse_future_allows_within_tolerance():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    refuse_future(now + MARK_FUTURE_TOLERANCE, now, "as_of")


def test_refuse_future_rejects_beyond_tolerance():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    with pytest.raises(HTTPException) as exc:
        refuse_future(now + MARK_FUTURE_TOLERANCE + timedelta(seconds=1), now, "as_of")
    assert exc.value.status_code == 422


def test_the_tolerance_is_two_minutes():
    """Pinned because two copies of this value drifting is the exact failure
    _parse_as_of's docstring records for its own near-identical copies."""
    assert MARK_FUTURE_TOLERANCE == timedelta(minutes=2)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/api/test_validation.py -v`
Expected: FAIL -- `ModuleNotFoundError: No module named 'api.validation'`

- [ ] **Step 3: Add the constant to `db/marks.py`**

At the top of `db/marks.py`, after the imports, add:

```python
# How far ahead of "now" a mark's as_of may sit before it is refused. Lives
# here rather than in cli.py because api/marks.py needs the same value and two
# copies of a policy constant drifting apart is precisely the failure
# cli.py's _parse_as_of docstring records for its own duplicated parser.
# A timedelta is a duration, not a clock -- this file stays clock-free.
MARK_FUTURE_TOLERANCE = timedelta(minutes=2)
```

Add `timedelta` to the existing `from datetime import datetime` line so it reads `from datetime import datetime, timedelta`.

- [ ] **Step 4: Point `cli.py` at it**

Delete the `_MARK_FUTURE_TOLERANCE = timedelta(minutes=2)` declaration at `cli.py:1440`. Extend the existing import at `cli.py:37` to:

```python
from db.marks import MARK_FUTURE_TOLERANCE, latest_marks, resolve_instrument_by_symbol, set_mark
```

Then replace both remaining uses (`cli.py:1517`, `cli.py:1520`, `cli.py:1787`, `cli.py:1790`) of `_MARK_FUTURE_TOLERANCE` with `MARK_FUTURE_TOLERANCE`:

```bash
cd /root/projects/Deadband && sed -i 's/_MARK_FUTURE_TOLERANCE/MARK_FUTURE_TOLERANCE/g' cli.py
```

Then confirm no stale declaration survives -- the sed above renames the declaration too if Step 4's delete was skipped:

```bash
cd /root/projects/Deadband && grep -n "MARK_FUTURE_TOLERANCE" cli.py
```

Expected: only the import line and four usage lines. If a `MARK_FUTURE_TOLERANCE = timedelta(minutes=2)` assignment appears in `cli.py`, delete it -- it shadows the import.

- [ ] **Step 5: Write `api/validation.py`**

```python
"""Parsing for values that arrive over HTTP, shared by every write route.

One home, deliberately. cli.py's `_parse_as_of` docstring records what
happened when `snapshot add` and `reconcile` each carried their own
near-identical copy: they drifted, and the README's own documented two-line
invocation exited 2 on its second line. The API has four routes that parse the
same three kinds of value; they get one parser, not four.

Every refusal here is an HTTPException(422) rather than a ValueError, because
every caller is a FastAPI handler and the alternative is each of them wrapping
these in try/except to say the same thing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from db.marks import MARK_FUTURE_TOLERANCE


def parse_decimal(raw: str, field: str) -> Decimal:
    """Exact decimal or 422. Mirrors cmd_marks_set's guards exactly.

    Two separate hazards, both real: Decimal("abc") raises InvalidOperation,
    which does NOT descend from ValueError, so a bare `except ValueError`
    lets it escape as a 500. And Decimal("NaN")/Decimal("Infinity")
    CONSTRUCT successfully and slip past that catch entirely -- is_finite()
    is this codebase's established second check (importers/fidelity.py,
    importers/coinbase_api.py, cli.py). Without it a NaN reaches
    account_snapshot, which has no CHECK constraints to stop it.
    """
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise HTTPException(422, f"{field}: {raw!r} is not a valid number") from None
    if not value.is_finite():
        raise HTTPException(422, f"{field}: {raw!r} must be a finite number")
    return value


def parse_instant(raw: str, field: str) -> datetime:
    """A timestamp that must carry a UTC offset. Bare dates are NOT accepted.

    Matches `marks set`, which takes a timestamp only -- widening it to bare
    dates would be a behaviour change, not a convenience (cli.py's
    _parse_as_of says so in as many words about cmd_marks_set).

    The offset requirement is not pedantry: comparing an offset-naive
    datetime against an offset-aware one downstream raises a raw TypeError
    ("can't compare offset-naive and offset-aware datetimes") that never
    reaches a clean refusal.
    """
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, f"{field}: {raw!r} is not a valid timestamp") from None
    if value.tzinfo is None:
        raise HTTPException(
            422, f"{field}: {raw!r} has no UTC offset (e.g. append +00:00 or Z)"
        )
    return value


def parse_as_of(raw: str, field: str) -> datetime:
    """A bare date becomes midnight UTC; a timestamp is taken as written.

    The API-side twin of cli.py's `_parse_as_of`, and it must stay that way:
    `snapshot add` accepts the bare date the README's worked example passes,
    so the form over it has to as well.

    The property the fallthrough depends on is that `date.fromisoformat`
    REJECTS anything carrying a time component -- verified on 3.12 for
    "2026-07-31T12:00", "2026-07-31 12:00" and "2026-07-31T12:00+00:00", all
    ValueError. That, not "it accepts only YYYY-MM-DD", is what makes this
    sound: since 3.11 it also accepts "20260801" and "2026-W31-1", both
    legitimate ways to name a day that correctly become midnight UTC. What
    would break it is a time-carrying string being swallowed by the first
    branch and never reaching parse_instant's offset guard, and that cannot
    happen.

    A bare date is exempt from the offset requirement because it is GIVEN
    UTC here, rather than implying an unnamed wall-clock zone the way a bare
    timestamp does.
    """
    try:
        return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=UTC)
    except ValueError:
        pass
    return parse_instant(raw, field)


def refuse_future(value: datetime, now: datetime, field: str) -> None:
    """Refuse an as_of beyond MARK_FUTURE_TOLERANCE ahead of `now`.

    `now` is a parameter rather than read here so the caller's single
    `datetime.now(UTC)` anchors both the omitted-as_of default and this
    guard, and the two measure against the exact same instant -- the same
    reason cmd_marks_set takes its clock at the top of the function.
    """
    if value > now + MARK_FUTURE_TOLERANCE:
        raise HTTPException(
            422,
            f"{field}: {value.isoformat()} is in the future "
            f"(tolerance: {MARK_FUTURE_TOLERANCE})",
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_validation.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 7: Retire `api/fills.py`'s private copy**

In `api/fills.py`, delete the `_decimal` function and replace its import block usage. Change the import section to add:

```python
from api.validation import parse_decimal
```

and delete:

```python
def _decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise HTTPException(422, f"{field}: {raw!r} is not a valid number") from None
    if not value.is_finite():
        raise HTTPException(422, f"{field}: {raw!r} must be finite")
    return value
```

Then rename the four call sites:

```bash
cd /root/projects/Deadband && sed -i 's/_decimal(/parse_decimal(/g' api/fills.py
```

`Decimal` and `InvalidOperation` may now be unused in `api/fills.py` -- `Decimal` is still referenced by the `quantity <= 0` comparison's operand type only implicitly, so check and remove genuinely unused imports:

```bash
cd /root/projects/Deadband && uv run ruff check api/fills.py
```

Fix whatever it reports. Note the message text changes very slightly for the non-finite case ("must be a finite number" instead of "must be finite"); that is intended -- one wording, not two.

- [ ] **Step 8: Verify nothing regressed**

Run these in the foreground and let them block; a 600000 ms tool timeout covers them.

```bash
cd /root/projects/Deadband && uv run pytest tests/api/ tests/db/test_cli.py -v
```

Expected: PASS, no skips. If `tests/api/test_fills_write.py` fails on an assertion about the exact 422 message text, update the test to the new wording -- that is the one intended behaviour change.

- [ ] **Step 9: Commit**

```bash
cd /root/projects/Deadband && git add api/validation.py tests/api/test_validation.py db/marks.py cli.py api/fills.py && git commit -m "refactor(api): one home for request-value parsing and the mark future tolerance"
```

---

### Task 2: `GET /api/marks` -- the holdings the marks table is built from

**Files:**
- Create: `api/marks.py`
- Create: `tests/api/test_marks.py`
- Modify: `api/app.py` (register the router)

**Interfaces:**
- Consumes: `api.validation` (Task 1) -- not used by this route yet, but the module is created alongside.
- Produces: `GET /api/marks` returning `{"marks": [MarkRow, ...], "generated_at": datetime}` where each `MarkRow` is:
  ```
  {"instrument_id": UUID, "symbol": str, "natural_key": str,
   "quantity": str, "accounts": [{"id": UUID, "name": str}, ...],
   "last_mark": {"price": str, "as_of": datetime} | null}
  ```
  Task 7's `Marks.tsx` renders exactly these fields.

**Three decisions this route encodes, each with a reason:**

1. **Deduped by instrument, not by (account, instrument).** `mark`'s primary key is `(instrument_id, as_of)` -- a mark is a property of the instrument, not of a holding. `open_positions` returns one row per account per instrument, so passing that through unchanged would show AAPL twice for a two-account holding and invite two conflicting prices for one database row. `quantity` is therefore the sum across accounts, and `accounts` names which ones, so the row is still traceable.
2. **Only positions with `unvaluable_reason is None`.** These are exactly the instruments `api/dashboard.py` passes to `latest_marks` -- the ones where a mark changes the answer. An unvaluable position is not priced against a mark at all, so listing it here would offer an action that accomplishes nothing.
3. **`natural_key` is returned.** Two different instruments can legitimately share a symbol (the same ticker quoted in two currencies) -- `instrument.symbol` is not unique, only `natural_key` is. Without it those render as two identical-looking rows. `resolve_instrument_by_symbol` exists precisely because of this ambiguity; picking by `instrument_id` avoids it, and showing `natural_key` is what lets a human tell the two rows apart.

**Router placement:** registered inside `api/app.py`'s `if enable_writes:` block even though this is a GET. Same reasoning `api/app.py` already applies to `POST /api/imports/preview`: it belongs to a write feature, and the published read-only instance has no legitimate use for the entry screen's supporting reads. It declares `get_conn` (the read pool), so `tests/api/test_write_pool.py` is satisfied.

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_marks.py`:

```python
"""GET /api/marks (spec section 4). All symbols and values invented."""

from datetime import UTC, datetime
from decimal import Decimal

from db.accounts import create_account
from db.instruments import upsert_instrument
from db.marks import set_mark
from db.trades import regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db


async def _held(conn, account_id, symbol, quantity="10", price="100"):
    """Give `account_id` an open position in `symbol` and return its
    instrument id. Goes through insert_fills + regroup_account rather than
    writing a position row directly, because open_positions derives
    positions from grouped trades -- a hand-written row would not appear."""
    from uuid import uuid4

    from db.fills import insert_fills

    instrument_id = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(), account_id=account_id, instrument_id=instrument_id,
                executed_at=datetime(2026, 6, 1, 15, 30, tzinfo=UTC), side=Side.BUY,
                quantity=Decimal(quantity), price=Decimal(price), fee=Decimal(0),
                fee_currency="USD", source=FillSource.MANUAL, venue_fill_id=None,
                is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, account_id)
    return instrument_id


async def test_get_marks_lists_a_held_instrument_with_no_mark(client, conn):
    acc = await create_account(conn, name="MarksA", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZM1")

    r = await client.get("/api/marks")
    assert r.status_code == 200
    body = r.json()
    assert_no_json_floats(body)

    row = next(m for m in body["marks"] if m["instrument_id"] == str(instrument_id))
    assert row["symbol"] == "ZZM1"
    assert row["natural_key"]
    assert row["quantity"] == "10"
    assert row["last_mark"] is None
    assert [a["name"] for a in row["accounts"]] == ["MarksA"]


async def test_get_marks_reports_an_existing_mark_with_its_age(client, conn):
    acc = await create_account(conn, name="MarksB", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZM2")
    marked_at = datetime(2026, 6, 15, 20, 0, tzinfo=UTC)
    await set_mark(conn, instrument_id, Decimal("241.50"), marked_at)

    body = (await client.get("/api/marks")).json()
    row = next(m for m in body["marks"] if m["instrument_id"] == str(instrument_id))
    assert row["last_mark"]["price"] == "241.50"
    assert row["last_mark"]["as_of"].startswith("2026-06-15")


async def test_get_marks_returns_one_row_for_an_instrument_held_in_two_accounts(client, conn):
    """A mark is keyed on instrument_id alone (mark's PRIMARY KEY), so one
    instrument must be ONE row here however many accounts hold it. Two rows
    would invite two conflicting prices for a single database row."""
    a = await create_account(conn, name="MarksC1", venue="manual", account_type="cash")
    b = await create_account(conn, name="MarksC2", venue="manual", account_type="cash")
    instrument_id = await _held(conn, a, "ZZM3", quantity="10")
    await _held(conn, b, "ZZM3", quantity="7")

    body = (await client.get("/api/marks")).json()
    rows = [m for m in body["marks"] if m["instrument_id"] == str(instrument_id)]
    assert len(rows) == 1
    assert rows[0]["quantity"] == "17"
    assert sorted(a["name"] for a in rows[0]["accounts"]) == ["MarksC1", "MarksC2"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_marks.py -v`
Expected: FAIL -- 404 on `/api/marks`, because the route does not exist yet.

- [ ] **Step 3: Write `api/marks.py` (GET only)**

```python
"""GET /api/marks and POST /api/marks (spec section 4).

Thin, like api/fills.py: db/marks.py holds every decision and cli.py's
`marks set` calls the same function this does (spec E6).

What this module adds over db/marks.py is the LIST the entry table is built
from -- which instruments are worth marking, deduped the way the `mark` table
is actually keyed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends

from api.deps import get_conn
from api.serialization import DeadbandJSONResponse
from db.marks import latest_marks
from db.positions import open_positions

# NOTE: this block imports only what the GET below uses. Task 3 adds the POST
# and the imports it needs (BaseModel, get_write_conn, require_trusted_identity,
# set_mark, the validation helpers) at that point — not here, where they would
# sit unused.

router = APIRouter()


@router.get("/api/marks")
async def marks(conn: asyncpg.Connection = Depends(get_conn)) -> DeadbandJSONResponse:
    """Every instrument the ledger holds that a mark would actually value.

    Deduped by instrument_id, NOT by (account_id, instrument_id) as
    open_positions returns them: `mark`'s primary key is
    (instrument_id, as_of), so one instrument is one markable thing however
    many accounts hold it. Rendering it once per account would offer two
    inputs writing to a single row, last one winning silently.

    Positions with an unvaluable_reason are omitted -- they are exactly the
    ones api/dashboard.py excludes from its latest_marks call, because they
    are not priced against a mark at all. Offering an action that changes
    nothing is worse than not offering it.
    """
    positions = [p for p in await open_positions(conn, None) if p.unvaluable_reason is None]

    # Accumulated in first-seen order so the response is stable across calls;
    # dict preserves insertion order and open_positions' own ordering is
    # deterministic.
    rolled: dict[UUID, dict] = {}
    for p in positions:
        entry = rolled.setdefault(
            p.instrument_id,
            {
                "instrument_id": p.instrument_id,
                "symbol": p.symbol,
                "natural_key": None,
                "quantity": Decimal(0),
                "accounts": [],
                "last_mark": None,
            },
        )
        entry["quantity"] += p.quantity
        entry["accounts"].append({"id": p.account_id, "name": p.account_name})

    if not rolled:
        return DeadbandJSONResponse({"marks": [], "generated_at": datetime.now(UTC)})

    # natural_key is NOT on OpenPosition, and it is what distinguishes two
    # instruments that legitimately share a symbol (the same ticker quoted in
    # two currencies). instrument.symbol is not unique; only natural_key is.
    # Without it those are two identical-looking rows and the user cannot
    # tell which one they are pricing.
    key_rows = await conn.fetch(
        "SELECT id, natural_key FROM instrument WHERE id = ANY($1::uuid[])",
        list(rolled),
    )
    for row in key_rows:
        rolled[row["id"]]["natural_key"] = row["natural_key"]

    # latest_marks returns (price, as_of) per instrument and omits -- never
    # zero-fills -- an instrument with no mark, because mark_price_chk
    # permits a genuine 0. `last_mark: null` and a 0.00 mark must stay
    # distinguishable in the payload for the same reason.
    for instrument_id, (price, as_of) in (await latest_marks(conn, list(rolled))).items():
        rolled[instrument_id]["last_mark"] = {"price": price, "as_of": as_of}

    return DeadbandJSONResponse(
        {"marks": list(rolled.values()), "generated_at": datetime.now(UTC)}
    )
```

- [ ] **Step 4: Register the router**

In `api/app.py`, inside the `if enable_writes:` block, after the existing imports, add:

```python
        from api.marks import router as marks_router

        # GET /api/marks is a read, but it exists to serve the entry screen's
        # marks table and is gated with the writes it feeds -- the same
        # reasoning applied to POST /api/imports/preview above. It declares
        # get_conn, so the read-pool guarantee is unaffected.
        app.include_router(marks_router)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_marks.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 6: Verify the structural guards still hold**

```bash
cd /root/projects/Deadband && uv run pytest tests/api/test_write_pool.py tests/api/test_write_identity.py -v
```

Expected: PASS. `GET /api/marks` uses `get_conn` and carries no identity dependency, which is what both guards require of a read route.

- [ ] **Step 7: Commit**

```bash
cd /root/projects/Deadband && git add api/marks.py tests/api/test_marks.py api/app.py && git commit -m "feat(api): GET /api/marks -- the holdings a mark would value, deduped by instrument"
```

---

### Task 3: `POST /api/marks` -- bulk mark entry

**Files:**
- Modify: `api/marks.py`, `tests/api/test_marks.py`, `tests/api/test_write_identity.py`

**Interfaces:**
- Consumes: `api.validation.parse_decimal`, `api.validation.parse_instant`, `api.validation.refuse_future` (Task 1); `db.marks.set_mark`.
- Produces: `POST /api/marks` taking `{"as_of": str, "marks": [{"instrument_id": UUID, "price": str}]}` and returning 201 `{"marks_set": int, "as_of": datetime}`. Task 6's `setMarks` client function calls it.

**No `regroup_account`.** Unlike `POST /api/fills`, a mark changes no trade grouping -- it is a valuation input, not a ledger event. The transaction exists only so a batch of marks lands whole.

**Duplicate instrument ids are refused, not merged.** Two entries for one instrument at one `as_of` would `ON CONFLICT DO UPDATE` each other inside the transaction and the last would win with nothing said. That is the same silent-last-wins hazard the GET's dedupe exists to prevent, arriving by another door.

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_marks.py`:

```python
async def test_post_marks_writes_every_row_in_one_call(client, conn):
    acc = await create_account(conn, name="MarksPost", venue="manual", account_type="cash")
    one = await _held(conn, acc, "ZZM4")
    two = await _held(conn, acc, "ZZM5")

    r = await client.post(
        "/api/marks",
        json={
            "as_of": "2026-08-01T20:00:00Z",
            "marks": [
                {"instrument_id": str(one), "price": "238.90"},
                {"instrument_id": str(two), "price": "12.05"},
            ],
        },
    )
    assert r.status_code == 201
    body = r.json()
    assert_no_json_floats(body)
    assert body["marks_set"] == 2
    assert await conn.fetchval("SELECT count(*) FROM mark") >= 2
    assert await conn.fetchval(
        "SELECT price FROM mark WHERE instrument_id = $1", one
    ) == Decimal("238.90")


async def test_post_marks_accepts_a_genuine_zero(client, conn):
    """mark_price_chk is `price >= 0`, so 0 is a legal mark -- an expired
    option is worth zero, and that is not the same as having no mark. The
    frontend's blank-means-skip rule depends on this being writable."""
    acc = await create_account(conn, name="MarksZero", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZM6")

    r = await client.post(
        "/api/marks",
        json={"as_of": "2026-08-01T20:00:00Z",
              "marks": [{"instrument_id": str(instrument_id), "price": "0"}]},
    )
    assert r.status_code == 201
    assert await conn.fetchval(
        "SELECT price FROM mark WHERE instrument_id = $1", instrument_id
    ) == Decimal("0")


async def test_post_marks_refuses_a_negative_price(client, conn):
    """mark_price_chk would refuse this in the database as an uncaught
    CheckViolationError -- a 500. It must be a 422 named to the row."""
    acc = await create_account(conn, name="MarksNeg", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZM7")

    r = await client.post(
        "/api/marks",
        json={"as_of": "2026-08-01T20:00:00Z",
              "marks": [{"instrument_id": str(instrument_id), "price": "-1"}]},
    )
    assert r.status_code == 422
    assert "marks[0].price" in r.json()["detail"]
    assert await conn.fetchval(
        "SELECT count(*) FROM mark WHERE instrument_id = $1", instrument_id
    ) == 0


async def test_post_marks_rolls_back_every_row_when_one_is_invalid(client, conn):
    acc = await create_account(conn, name="MarksAtomic", venue="manual", account_type="cash")
    good = await _held(conn, acc, "ZZM8")
    bad = await _held(conn, acc, "ZZM9")

    r = await client.post(
        "/api/marks",
        json={
            "as_of": "2026-08-01T20:00:00Z",
            "marks": [
                {"instrument_id": str(good), "price": "10"},
                {"instrument_id": str(bad), "price": "not-a-number"},
            ],
        },
    )
    assert r.status_code == 422
    assert await conn.fetchval("SELECT count(*) FROM mark WHERE instrument_id = $1", good) == 0


async def test_post_marks_refuses_an_empty_list(client):
    r = await client.post("/api/marks", json={"as_of": "2026-08-01T20:00:00Z", "marks": []})
    assert r.status_code == 422


async def test_post_marks_refuses_a_duplicate_instrument(client, conn):
    """Two rows for one instrument at one as_of would ON CONFLICT DO UPDATE
    each other inside the transaction -- last one wins, silently. Refuse."""
    acc = await create_account(conn, name="MarksDup", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZMA")

    r = await client.post(
        "/api/marks",
        json={
            "as_of": "2026-08-01T20:00:00Z",
            "marks": [
                {"instrument_id": str(instrument_id), "price": "10"},
                {"instrument_id": str(instrument_id), "price": "20"},
            ],
        },
    )
    assert r.status_code == 422
    assert "duplicate" in r.json()["detail"].lower()


async def test_post_marks_refuses_a_future_as_of(client, conn):
    acc = await create_account(conn, name="MarksFuture", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZMB")

    r = await client.post(
        "/api/marks",
        json={"as_of": "2099-01-01T00:00:00Z",
              "marks": [{"instrument_id": str(instrument_id), "price": "10"}]},
    )
    assert r.status_code == 422
    assert "future" in r.json()["detail"]


async def test_post_marks_refuses_an_as_of_without_an_offset(client, conn):
    acc = await create_account(conn, name="MarksNaive", venue="manual", account_type="cash")
    instrument_id = await _held(conn, acc, "ZZMC")

    r = await client.post(
        "/api/marks",
        json={"as_of": "2026-08-01T20:00:00",
              "marks": [{"instrument_id": str(instrument_id), "price": "10"}]},
    )
    assert r.status_code == 422
    assert "offset" in r.json()["detail"]


async def test_post_marks_404s_on_an_unknown_instrument(client):
    from uuid import uuid4

    r = await client.post(
        "/api/marks",
        json={"as_of": "2026-08-01T20:00:00Z",
              "marks": [{"instrument_id": str(uuid4()), "price": "10"}]},
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_marks.py -v -k post`
Expected: FAIL -- 405 or 404, no POST handler exists.

- [ ] **Step 3: Add the POST handler to `api/marks.py`**

Extend the imports:

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_conn, get_write_conn
from api.identity import require_trusted_identity
from api.validation import parse_decimal, parse_instant, refuse_future
from db.marks import latest_marks, set_mark
```

Then append:

```python
class MarkIn(BaseModel):
    instrument_id: UUID
    price: str


class MarksIn(BaseModel):
    as_of: str
    marks: list[MarkIn]


@router.post("/api/marks", status_code=201)
async def create_marks(
    body: MarksIn,
    # Identity is declared BEFORE get_write_conn: FastAPI resolves
    # dependencies in declaration order, so an unauthenticated caller is
    # refused before the write pool is ever touched. See api/fills.py's
    # identical comment -- the reverse order let a 403-bound request check
    # out a write-pool connection on every attempt.
    _identity: str = Depends(require_trusted_identity),
    conn: asyncpg.Connection = Depends(get_write_conn),
) -> DeadbandJSONResponse:
    """Set one price per instrument, all at one as_of, in one transaction.

    No regroup_account, unlike POST /api/fills: a mark is a valuation input,
    not a ledger event, so no trade grouping changes. The transaction is here
    so a batch lands whole, nothing more.
    """
    if not body.marks:
        raise HTTPException(422, "marks: at least one mark is required")

    # The clock is taken ONCE, here in the I/O layer, so the future-date
    # guard measures against a single instant -- cmd_marks_set does the same
    # for the same reason.
    now = datetime.now(UTC)
    as_of = parse_instant(body.as_of, "as_of")
    refuse_future(as_of, now, "as_of")

    # Refused, not merged: two entries for one instrument at one as_of would
    # ON CONFLICT DO UPDATE each other inside the transaction below, and the
    # second would win with nothing reported. That is the same silent
    # last-one-wins the GET's dedupe exists to prevent, arriving by another
    # door.
    seen: set[UUID] = set()
    for i, m in enumerate(body.marks):
        if m.instrument_id in seen:
            raise HTTPException(
                422, f"marks[{i}].instrument_id: duplicate instrument in one submission"
            )
        seen.add(m.instrument_id)

    # Validate EVERY row before opening the transaction: a bad price on row 4
    # must not leave rows 1-3 written. The transaction makes that true anyway,
    # but failing early keeps the error clean -- api/fills.py's identical
    # comment.
    parsed: list[tuple[UUID, Decimal]] = []
    for i, m in enumerate(body.marks):
        price = parse_decimal(m.price, f"marks[{i}].price")
        # mark_price_chk is `price >= 0 AND price < 'Infinity'`. Zero is a
        # LEGAL mark -- an expired option is worth zero, and that is not the
        # same as having no mark at all. Negative is not, and reaching the
        # database with one produces an uncaught CheckViolationError, i.e. a
        # 500 for what is plainly a bad request.
        if price < 0:
            raise HTTPException(422, f"marks[{i}].price: {m.price!r} must not be negative")
        parsed.append((m.instrument_id, price))

    known = {
        r["id"]
        for r in await conn.fetch(
            "SELECT id FROM instrument WHERE id = ANY($1::uuid[])", list(seen)
        )
    }
    missing = seen - known
    if missing:
        raise HTTPException(404, f"instrument not found: {sorted(str(m) for m in missing)[0]}")

    async with conn.transaction():
        for instrument_id, price in parsed:
            await set_mark(conn, instrument_id, price, as_of)

    return DeadbandJSONResponse({"marks_set": len(parsed), "as_of": as_of}, status_code=201)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_marks.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Extend the over-HTTP identity guard**

The structural guard in `tests/api/test_write_identity.py` picks up the new route by walking the route table, but the over-HTTP half enumerates requests by hand. Add to `tests/api/test_write_identity.py`, after `_delete_fill`:

```python
async def _post_marks(client):
    # A syntactically valid, never-created instrument id: identity is checked
    # before the handler queries for it, so a 403/503 here cannot be confused
    # with the 404 a real-but-missing instrument would produce.
    return await client.post(
        "/api/marks",
        json={"as_of": "2026-06-01T15:30:00Z",
              "marks": [{"instrument_id": str(uuid4()), "price": "1"}]},
    )
```

and extend `_WRITE_REQUESTS`:

```python
_WRITE_REQUESTS = [
    pytest.param(_post_fills, id="post-fills"),
    pytest.param(_delete_fill, id="delete-fill"),
    pytest.param(_commit_import, id="commit-import"),
    pytest.param(_post_marks, id="post-marks"),
]
```

- [ ] **Step 6: Run the identity and pool guards**

```bash
cd /root/projects/Deadband && uv run pytest tests/api/test_write_identity.py tests/api/test_write_pool.py -v
```

Expected: PASS. The three parametrized identity tests now run four cases each.

- [ ] **Step 7: Commit**

```bash
cd /root/projects/Deadband && git add api/marks.py tests/api/test_marks.py tests/api/test_write_identity.py && git commit -m "feat(api): POST /api/marks -- bulk mark entry in one transaction"
```

---

### Task 4: `GET /api/accounts/{account_id}/snapshot` -- the overwrite check

**Files:**
- Create: `api/snapshots.py`
- Create: `tests/api/test_snapshots.py`
- Modify: `api/app.py`

**Interfaces:**
- Consumes: `api.validation.parse_as_of` (Task 1).
- Produces: `GET /api/accounts/{account_id}/snapshot?as_of=<date>` returning `{"snapshot": {"as_of": datetime, "cash_balance": str, "total_equity": str, "note": str | null} | null}`. Task 8's `Snapshot.tsx` uses it for the replace warning.

**This is an EXACT-date lookup, not `latest_snapshot`.** `latest_snapshot` returns the most recent snapshot *on or before* `as_of` -- correct for reconciling, wrong here. The form needs to know whether saving will **replace** a row, and `add_snapshot`'s `ON CONFLICT (account_id, as_of) DO UPDATE` fires only on an exact match. Using `latest_snapshot` would warn "this replaces the July statement" when entering an August one that replaces nothing.

- [ ] **Step 1: Write the failing test**

Create `tests/api/test_snapshots.py`:

```python
"""Statement snapshot routes (spec section 4). All figures invented."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.snapshots import add_snapshot
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db


async def test_get_snapshot_returns_null_when_none_exists(client, conn):
    acc = await create_account(conn, name="SnapNone", venue="manual", account_type="cash")
    r = await client.get(f"/api/accounts/{acc}/snapshot", params={"as_of": "2026-07-31"})
    assert r.status_code == 200
    assert r.json()["snapshot"] is None


async def test_get_snapshot_returns_an_exact_date_match(client, conn):
    acc = await create_account(conn, name="SnapHit", venue="manual", account_type="cash")
    await add_snapshot(
        conn, acc, datetime(2026, 7, 31, tzinfo=UTC),
        cash_balance=Decimal("1180.00"), total_equity=Decimal("30000.00"), note="July",
    )
    r = await client.get(f"/api/accounts/{acc}/snapshot", params={"as_of": "2026-07-31"})
    body = r.json()
    assert_no_json_floats(body)
    assert body["snapshot"]["cash_balance"] == "1180.00"
    assert body["snapshot"]["note"] == "July"


async def test_get_snapshot_does_not_fall_back_to_an_earlier_one(client, conn):
    """latest_snapshot is `on or before` and is the WRONG function here. This
    route answers "will saving replace something?", and add_snapshot's
    ON CONFLICT fires only on an exact (account_id, as_of) match. Falling back
    would warn about replacing a July statement while entering an August one
    that replaces nothing."""
    acc = await create_account(conn, name="SnapNoFallback", venue="manual", account_type="cash")
    await add_snapshot(
        conn, acc, datetime(2026, 7, 31, tzinfo=UTC),
        cash_balance=Decimal("1180.00"), total_equity=Decimal("30000.00"),
    )
    r = await client.get(f"/api/accounts/{acc}/snapshot", params={"as_of": "2026-08-31"})
    assert r.json()["snapshot"] is None


async def test_get_snapshot_404s_on_an_unknown_account(client):
    r = await client.get(f"/api/accounts/{uuid4()}/snapshot", params={"as_of": "2026-07-31"})
    assert r.status_code == 404


async def test_get_snapshot_422s_on_an_unparseable_as_of(client, conn):
    acc = await create_account(conn, name="SnapBadDate", venue="manual", account_type="cash")
    r = await client.get(f"/api/accounts/{acc}/snapshot", params={"as_of": "not-a-date"})
    assert r.status_code == 422
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_snapshots.py -v`
Expected: FAIL -- the route does not exist.

- [ ] **Step 3: Write `api/snapshots.py` (GET only)**

```python
"""GET /api/accounts/{id}/snapshot and POST /api/snapshots (spec section 4).

Thin, like api/fills.py: db/snapshots.py holds every decision and cli.py's
`snapshot add` calls the same function this does (spec E6).
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_conn
from api.serialization import DeadbandJSONResponse
from api.validation import parse_as_of
from db.accounts import get_account

# NOTE: only what the GET below uses. Task 5 adds BaseModel, get_write_conn,
# require_trusted_identity, add_snapshot, parse_decimal and refuse_future when
# it adds the POST that needs them.

router = APIRouter()


@router.get("/api/accounts/{account_id}/snapshot")
async def snapshot_for_date(
    account_id: UUID,
    as_of: str = Query(...),
    conn: asyncpg.Connection = Depends(get_conn),
) -> DeadbandJSONResponse:
    """The snapshot stored for EXACTLY this account and date, or null.

    Deliberately not db.snapshots.latest_snapshot, which returns the most
    recent snapshot ON OR BEFORE its as_of. That is the right function for
    reconciling and the wrong one here: this route answers "will saving
    replace an existing row?", and add_snapshot's
    ON CONFLICT (account_id, as_of) fires only on an exact match. A fallback
    would warn about replacing July's statement while entering August's,
    which replaces nothing.
    """
    if await get_account(conn, account_id) is None:
        raise HTTPException(404, "account not found")
    parsed = parse_as_of(as_of, "as_of")
    row = await conn.fetchrow(
        """
        SELECT as_of, cash_balance, total_equity, note
          FROM account_snapshot
         WHERE account_id = $1 AND as_of = $2
        """,
        account_id,
        parsed,
    )
    return DeadbandJSONResponse({"snapshot": dict(row) if row is not None else None})
```

- [ ] **Step 4: Register the router**

In `api/app.py`, inside `if enable_writes:`, add alongside the marks router:

```python
        from api.snapshots import router as snapshots_router

        app.include_router(snapshots_router)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_snapshots.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 6: Commit**

```bash
cd /root/projects/Deadband && git add api/snapshots.py tests/api/test_snapshots.py api/app.py && git commit -m "feat(api): GET the snapshot stored for an exact account and date"
```

---

### Task 5: `POST /api/snapshots` -- record a statement

**Files:**
- Modify: `api/snapshots.py`, `tests/api/test_snapshots.py`, `tests/api/test_write_identity.py`

**Interfaces:**
- Consumes: `api.validation.parse_as_of`, `api.validation.parse_decimal`, `api.validation.refuse_future`; `db.snapshots.add_snapshot`.
- Produces: `POST /api/snapshots` taking `{"account_id": UUID, "as_of": str, "cash_balance": str, "total_equity": str, "note": str | null}`, returning 201 `{"account_id": UUID, "as_of": datetime, "replaced": bool}`. Task 6's `createSnapshot` calls it.

**`cash_balance` and `total_equity` are passed as KEYWORD arguments to `add_snapshot`.** Its signature makes them keyword-only and the `*` is load-bearing: they are adjacent parameters of the same type with no way to tell them apart at a call site. Transposed positionally, cash is stored as equity and equity as cash, both are valid NUMERIC, nothing raises, and `reconcile` reports the swap days later as unexplained drift on both lines at once. Do not "simplify" the call.

**`replaced` is returned** so the UI can say what happened. `add_snapshot`'s `ON CONFLICT DO UPDATE` is the intended edit path -- correcting a mistyped figure is the point, and the table has no history columns -- but a silent overwrite and a fresh insert should not look identical to the person who just clicked Save.

- [ ] **Step 1: Write the failing tests**

Append to `tests/api/test_snapshots.py`:

```python
def _body(account_id, **over):
    body = {
        "account_id": str(account_id), "as_of": "2026-07-31",
        "cash_balance": "1204.11", "total_equity": "30184.22", "note": "July statement",
    }
    body.update(over)
    return body


async def test_post_snapshot_stores_the_figures_in_the_right_columns(client, conn):
    """The transposition guard, end to end. add_snapshot's cash/equity are
    keyword-only because swapping them positionally stores each as the other,
    raises nothing, and surfaces days later as drift on both lines at once."""
    acc = await create_account(conn, name="SnapPost", venue="manual", account_type="cash")
    r = await client.post("/api/snapshots", json=_body(acc))
    assert r.status_code == 201
    body = r.json()
    assert_no_json_floats(body)
    assert body["replaced"] is False

    row = await conn.fetchrow(
        "SELECT cash_balance, total_equity FROM account_snapshot WHERE account_id = $1", acc
    )
    assert row["cash_balance"] == Decimal("1204.11")
    assert row["total_equity"] == Decimal("30184.22")


async def test_post_snapshot_stores_a_bare_date_as_midnight_utc(client, conn):
    acc = await create_account(conn, name="SnapDate", venue="manual", account_type="cash")
    await client.post("/api/snapshots", json=_body(acc))
    stored = await conn.fetchval(
        "SELECT as_of FROM account_snapshot WHERE account_id = $1", acc
    )
    assert stored == datetime(2026, 7, 31, tzinfo=UTC)


async def test_post_snapshot_reports_a_replacement(client, conn):
    acc = await create_account(conn, name="SnapReplace", venue="manual", account_type="cash")
    await client.post("/api/snapshots", json=_body(acc, cash_balance="1180.00"))
    r = await client.post("/api/snapshots", json=_body(acc, cash_balance="1204.11"))
    assert r.status_code == 201
    assert r.json()["replaced"] is True
    assert await conn.fetchval(
        "SELECT count(*) FROM account_snapshot WHERE account_id = $1", acc
    ) == 1
    assert await conn.fetchval(
        "SELECT cash_balance FROM account_snapshot WHERE account_id = $1", acc
    ) == Decimal("1204.11")


async def test_post_snapshot_accepts_negative_cash(client, conn):
    """account_snapshot carries NO check constraints and a margin debit is a
    legitimate negative cash balance. Refusing it would make the form unable
    to record a real statement."""
    acc = await create_account(conn, name="SnapMargin", venue="manual", account_type="margin")
    r = await client.post("/api/snapshots", json=_body(acc, cash_balance="-2500.00"))
    assert r.status_code == 201
    assert await conn.fetchval(
        "SELECT cash_balance FROM account_snapshot WHERE account_id = $1", acc
    ) == Decimal("-2500.00")


async def test_post_snapshot_refuses_a_nan_figure(client, conn):
    """Postgres NUMERIC ACCEPTS 'NaN', and account_snapshot has no CHECK to
    stop it -- parse_decimal's is_finite() is the only thing that does."""
    acc = await create_account(conn, name="SnapNaN", venue="manual", account_type="cash")
    r = await client.post("/api/snapshots", json=_body(acc, total_equity="NaN"))
    assert r.status_code == 422
    assert await conn.fetchval(
        "SELECT count(*) FROM account_snapshot WHERE account_id = $1", acc
    ) == 0


async def test_post_snapshot_refuses_a_future_date(client, conn):
    acc = await create_account(conn, name="SnapFuture", venue="manual", account_type="cash")
    r = await client.post("/api/snapshots", json=_body(acc, as_of="2099-01-01"))
    assert r.status_code == 422
    assert "future" in r.json()["detail"]


async def test_post_snapshot_404s_on_an_unknown_account(client):
    r = await client.post("/api/snapshots", json=_body(uuid4()))
    assert r.status_code == 404
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_snapshots.py -v -k post`
Expected: FAIL -- no POST handler.

- [ ] **Step 3: Add the POST handler to `api/snapshots.py`**

First extend the import block Task 4 left minimal — these are the names the
POST needs and the GET did not:

```python
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps import get_conn, get_write_conn
from api.identity import require_trusted_identity
from api.validation import parse_as_of, parse_decimal, refuse_future
from db.snapshots import add_snapshot
```

Then append:

```python
class SnapshotIn(BaseModel):
    account_id: UUID
    as_of: str
    cash_balance: str
    total_equity: str
    note: str | None = None


@router.post("/api/snapshots", status_code=201)
async def create_snapshot(
    body: SnapshotIn,
    # Identity before get_write_conn -- see api/fills.py's create_fills for
    # why the ORDER matters, not just that both are present.
    _identity: str = Depends(require_trusted_identity),
    conn: asyncpg.Connection = Depends(get_write_conn),
) -> DeadbandJSONResponse:
    """Record what the broker reported for one account on one statement date.

    Re-posting the same (account, as_of) UPDATES the row -- correcting a
    mistyped figure is the point and the table has no history columns. The
    response says which happened, because a silent overwrite and a fresh
    insert must not look identical to whoever just clicked Save.
    """
    now = datetime.now(UTC)
    if await get_account(conn, body.account_id) is None:
        raise HTTPException(404, "account not found")

    as_of = parse_as_of(body.as_of, "as_of")
    refuse_future(as_of, now, "as_of")

    # No sign guard on either figure: account_snapshot carries no CHECK
    # constraints and a margin debit is a legitimate negative cash balance.
    # is_finite() inside parse_decimal is doing real work here though --
    # Postgres NUMERIC accepts 'NaN' and nothing downstream would refuse it.
    cash_balance = parse_decimal(body.cash_balance, "cash_balance")
    total_equity = parse_decimal(body.total_equity, "total_equity")

    async with conn.transaction():
        replaced = await conn.fetchval(
            "SELECT true FROM account_snapshot WHERE account_id = $1 AND as_of = $2",
            body.account_id,
            as_of,
        )
        # KEYWORD arguments, and add_snapshot's `*` is what forces it. These
        # two are adjacent parameters of the same type with no way to tell
        # them apart positionally: transposed, cash is stored as equity and
        # equity as cash, both are valid NUMERIC, nothing raises, and
        # reconcile reports the swap days later as unexplained drift on both
        # lines at once. Do not simplify this call.
        await add_snapshot(
            conn,
            body.account_id,
            as_of,
            cash_balance=cash_balance,
            total_equity=total_equity,
            note=body.note,
        )

    return DeadbandJSONResponse(
        {"account_id": body.account_id, "as_of": as_of, "replaced": bool(replaced)},
        status_code=201,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_snapshots.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 5: Extend the over-HTTP identity guard**

In `tests/api/test_write_identity.py`, add after `_post_marks`:

```python
async def _post_snapshot(client):
    return await client.post(
        "/api/snapshots",
        json={"account_id": str(uuid4()), "as_of": "2026-06-01",
              "cash_balance": "1", "total_equity": "2"},
    )
```

and extend `_WRITE_REQUESTS`:

```python
    pytest.param(_post_snapshot, id="post-snapshot"),
```

- [ ] **Step 6: Run the full API lane**

```bash
cd /root/projects/Deadband && uv run pytest tests/api/ -v
```

Expected: PASS, no skips. `GET /api/accounts/{id}/snapshot` must NOT carry the identity dependency -- `test_every_write_route_requires_identity` asserts both halves, so a blanket application would fail here.

- [ ] **Step 7: Commit**

```bash
cd /root/projects/Deadband && git add api/snapshots.py tests/api/test_snapshots.py tests/api/test_write_identity.py && git commit -m "feat(api): POST /api/snapshots -- record a broker statement"
```

---

### Task 6: Frontend plumbing -- shared `toInstant`, types, client functions

**Files:**
- Create: `web/src/datetime.ts`
- Modify: `web/src/screens/Entry.tsx` (remove `toInstant`, import it), `web/src/api.ts`

**Interfaces:**
- Consumes: the four routes from Tasks 2-5.
- Produces:
  - `web/src/datetime.ts`: `toInstant(local: string): string`
  - `web/src/api.ts`: `MarkRow`, `MarksPage`, `MarkIn`, `SetMarksResult`, `StoredSnapshot`, `SnapshotIn`, `CreatedSnapshot` types; `fetchMarks()`, `setMarks(body)`, `fetchSnapshot(accountId, asOf)`, `createSnapshot(body)`.

**Why `toInstant` moves rather than being rewritten.** `web/` has **no test runner** -- no vitest, no `*.test.*` files, and `package.json`'s scripts are `dev`/`build`/`lint`/`preview` only. So the `datetime-local` conversion hazard cannot be pinned by a frontend test: stamping `Z` on a local string shifts every value by the browser's UTC offset, and **it tests clean on this box because the VPS runs UTC**, so a green run here proves nothing about it either way. Reusing the existing, already-reviewed function is what prevents reintroducing the bug -- enforcement by construction, since there is no assertion available. Do not add a test framework as a side effect of this feature; that is its own decision.

- [ ] **Step 1: Create `web/src/datetime.ts`**

Move the function out of `Entry.tsx` verbatim, comment included, and export it:

```typescript
// Shared by every form that reads a `datetime-local` input. Extracted from
// Entry.tsx rather than copied: `datetime-local` yields "2026-08-28T14:02"
// with NO zone, and stamping "Z" on it claims a UTC wall-clock reading that
// is wrong by the browser's offset. That silently shifts a fill's
// executed_at, and grouping orders fills by executed_at, so it can reorder
// trades. `new Date(local)` parses an offset-less string as LOCAL time (this
// also handles the with-seconds form), so `.toISOString()` yields the true
// UTC instant.
//
// This box runs UTC, so the bug is INVISIBLE to any test run here -- a green
// local run proves nothing about it. That is exactly why there is one copy
// of this function and not two.
//
// An empty or unparseable value must throw HERE rather than produce
// `Invalid Date` silently -- callers rely on this landing in their own
// try/catch so a bad date can never wedge a busy flag.
export function toInstant(local: string, label = 'executed at'): string {
  const d = new Date(local)
  if (Number.isNaN(d.getTime())) throw new Error(`${label}: enter a date and time`)
  return d.toISOString()
}
```

- [ ] **Step 2: Point `Entry.tsx` at it**

Delete the `toInstant` function from `web/src/screens/Entry.tsx` (around lines 55-66, including its comment block) and add to the imports at the top:

```typescript
import { toInstant } from '../datetime'
```

- [ ] **Step 3: Verify the move compiles and changes nothing**

```bash
cd /root/projects/Deadband/web && pnpm build && pnpm lint
```

Expected: build succeeds, lint clean. `Entry.tsx`'s two existing call sites (`submit` and `submitMultileg`) pass one argument and keep the default label, so their error text is unchanged.

- [ ] **Step 4: Add types and client functions to `web/src/api.ts`**

Append after the existing `deleteFill` export:

```typescript
// --- marks and statement snapshots (spec section 4) ---
//
// Money stays a STRING end to end here as everywhere else: these values reach
// NUMERIC columns and a round-trip through JS `number` would quietly lose
// precision the ledger is built to preserve.

export interface MarkRow {
  instrument_id: string
  symbol: string
  natural_key: string
  quantity: string
  accounts: { id: string; name: string }[]
  // null means NO mark exists -- distinct from a mark of "0", which is legal
  // (mark_price_chk is `price >= 0`) and means the thing is worth nothing.
  last_mark: { price: string; as_of: string } | null
}

export interface MarksPage {
  marks: MarkRow[]
  generated_at: string
}

export interface MarkIn {
  instrument_id: string
  price: string
}

export interface SetMarksResult {
  marks_set: number
  as_of: string
}

export interface StoredSnapshot {
  as_of: string
  cash_balance: string
  total_equity: string
  note: string | null
}

export interface SnapshotIn {
  account_id: string
  as_of: string
  cash_balance: string
  total_equity: string
  note: string | null
}

export interface CreatedSnapshot {
  account_id: string
  as_of: string
  replaced: boolean
}

export const fetchMarks = () => get<MarksPage>('/api/marks')

// `send` is the file's existing JSON helper (used by createFills/deleteFill).
// It already routes a 404 to NotFound and pulls the API's `detail` string out
// of an error body via errorMessage() -- which matters here, because the 422s
// these routes return name the offending row ("marks[2].price: ...") and the
// screens surface that text verbatim. Do not add a second POST helper.
export const setMarks = (body: { as_of: string; marks: MarkIn[] }) =>
  send<SetMarksResult>('/api/marks', 'POST', body)

export const fetchSnapshot = (accountId: string, asOf: string) =>
  get<{ snapshot: StoredSnapshot | null }>(
    `/api/accounts/${accountId}/snapshot?as_of=${encodeURIComponent(asOf)}`,
  )

export const createSnapshot = (body: SnapshotIn) =>
  send<CreatedSnapshot>('/api/snapshots', 'POST', body)
```

- [ ] **Step 5: Confirm `send` behaves as the new callers assume**

`web/src/api.ts` already defines `send<T>(path, method, body)` and an
`errorMessage(r, path)` helper behind it; `createFills` and `deleteFill` are
its existing callers. Read them and confirm two properties the new screens
depend on, rather than assuming:

```bash
cd /root/projects/Deadband && grep -n "async function send" -A 12 web/src/api.ts && grep -n "async function errorMessage" -A 14 web/src/api.ts
```

- A non-2xx response must surface the API's `detail` string, not just the
  status. The 422s from `POST /api/marks` name the offending row
  (`marks[2].price: ...`) and `Marks.tsx` shows that text verbatim; flattened
  to "400 Bad Request" it would tell the user nothing.
- A 404 throws `NotFound`. `fetchSnapshot` hits a 404 for an unknown account,
  and `Snapshot.tsx`'s `.catch(() => setExisting(null))` already absorbs it.

If either property is missing, fix `errorMessage`/`send` in place -- do not
add a parallel helper. Two request helpers in one file is the drift shape this
repo keeps paying for.

- [ ] **Step 6: Verify it compiles**

```bash
cd /root/projects/Deadband/web && pnpm build && pnpm lint
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
cd /root/projects/Deadband && git add web/src/datetime.ts web/src/api.ts web/src/screens/Entry.tsx && git commit -m "refactor(web): share toInstant, and add the marks/snapshot API client"
```

---

### Task 7: The `Marks` screen -- bulk price entry

**Files:**
- Create: `web/src/screens/Marks.tsx`
- Modify: `web/src/screens/Entry.tsx` (add the segment)

**Interfaces:**
- Consumes: `fetchMarks`, `setMarks`, `MarkRow` (Task 6); `toInstant` (Task 6); `money`, `qty` from `web/src/format.ts`.
- Produces: `export default function Marks()`.

**The blank-versus-zero rule is the whole screen.** An empty input means *leave this instrument alone*; `"0"` means *record a price of zero*. `mark_price_chk` permits 0, `latest_marks` omits an unmarked instrument rather than zero-filling it, and the API accepts `"0"` -- three layers already make that distinction, and the form must not collapse it by treating falsy input as zero.

- [ ] **Step 1: Write `web/src/screens/Marks.tsx`**

```typescript
import { useEffect, useState } from 'react'
import { fetchMarks, setMarks, type MarkRow } from '../api'
import { toInstant } from '../datetime'
import { money, qty } from '../format'

// The `datetime-local` input wants "YYYY-MM-DDTHH:MM" in LOCAL time, which is
// what the user is thinking in. It is converted back to a true UTC instant by
// toInstant() at submit -- never by stamping "Z", which would be wrong by the
// browser's offset. See web/src/datetime.ts.
function localNow(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function ageDays(asOf: string): number {
  return Math.floor((Date.now() - new Date(asOf).getTime()) / 86_400_000)
}

export default function Marks() {
  const [rows, setRows] = useState<MarkRow[] | null>(null)
  const [prices, setPrices] = useState<Record<string, string>>({})
  const [asOf, setAsOf] = useState(localNow)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  async function load() {
    try {
      const page = await fetchMarks()
      setRows(page.marks)
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    }
  }

  useEffect(() => {
    void load()
  }, [])

  // A blank input means "leave this instrument alone"; "0" means "record a
  // price of zero". These are DIFFERENT, all the way down: mark_price_chk
  // permits 0, latest_marks omits an unmarked instrument rather than
  // zero-filling it (a genuine 0 mark is legal), and the API accepts "0".
  // Collapsing them here -- `Number(p) || skip`, say -- would make it
  // impossible to mark an expired option worthless.
  const filled = Object.entries(prices).filter(([, p]) => p.trim() !== '')

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (busy || filled.length === 0) return
    setBusy(true)
    setError(null)
    setSaved(null)
    try {
      // Inside the try: toInstant throws on an unparseable value, and it must
      // land in this catch or a bad date leaves the button wedged on
      // "saving…".
      const at = toInstant(asOf, 'as of')
      const r = await setMarks({
        as_of: at,
        marks: filled.map(([instrument_id, price]) => ({ instrument_id, price: price.trim() })),
      })
      setSaved(`${r.marks_set} mark${r.marks_set === 1 ? '' : 's'} recorded`)
      setPrices({})
      // Reload rather than patching state: the server is the authority on
      // what the stored mark and its age now are, and a mark written at an
      // as_of EARLIER than an existing one does not become "the latest"
      // (latest_marks orders by as_of, not by insertion).
      await load()
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  if (rows === null) {
    return error ? <div className="error">marks failed to load — {error}</div> : <div>loading…</div>
  }

  if (rows.length === 0) {
    return (
      <div className="empty">
        nothing held — there is nothing to mark until the ledger has an open position
      </div>
    )
  }

  return (
    <form className="marks" onSubmit={submit}>
      <label className="asof">
        as of
        <input
          type="datetime-local"
          value={asOf}
          onChange={(e) => setAsOf(e.target.value)}
          required
        />
      </label>

      <table className="marks-table">
        <thead>
          <tr>
            <th>symbol</th>
            <th className="num">held</th>
            <th>last mark</th>
            <th className="num">price</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.instrument_id}>
              <td>
                {row.symbol}
                {/* instrument.symbol is NOT unique -- two instruments can
                    legitimately share a ticker (the same symbol quoted in two
                    currencies). Only natural_key is unique, so without it
                    these are two identical-looking rows and there is no way
                    to tell which one is being priced. */}
                <span className="muted"> {row.natural_key}</span>
              </td>
              <td className="num">{qty(row.quantity)}</td>
              <td>
                {row.last_mark === null ? (
                  <span className="muted">never marked</span>
                ) : (
                  <>
                    {money(row.last_mark.price)}{' '}
                    <span className="muted">({ageDays(row.last_mark.as_of)}d old)</span>
                  </>
                )}
              </td>
              <td className="num">
                <input
                  type="text"
                  inputMode="decimal"
                  value={prices[row.instrument_id] ?? ''}
                  onChange={(e) =>
                    setPrices((p) => ({ ...p, [row.instrument_id]: e.target.value }))
                  }
                  aria-label={`price for ${row.symbol}`}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="actions">
        <span className="muted">
          {filled.length} of {rows.length} filled · blank rows are left untouched
        </span>
        <button type="submit" disabled={busy || filled.length === 0}>
          {busy ? 'saving…' : 'Save marks'}
        </button>
      </div>

      {error && <div className="error">{error}</div>}
      {saved && <div className="ok">{saved}</div>}
    </form>
  )
}
```

- [ ] **Step 2: Add the segment to `Entry.tsx`**

Import it at the top:

```typescript
import Marks from './Marks'
```

Widen the mode union:

```typescript
  const [mode, setMode] = useState<'fill' | 'multileg' | 'import' | 'marks' | 'snapshot'>('fill')
```

Add a button inside the existing `<div className="segmented" role="tablist">`, following the exact shape of the three already there:

```tsx
        <button
          type="button" role="tab" aria-selected={mode === 'marks'}
          className={mode === 'marks' ? 'active' : undefined}
          onClick={() => setMode('marks')}
        >
          Marks
        </button>
```

Then add a branch to the mode chain, before its final `) : (` import branch closes -- render `{mode === 'marks' ? <Marks /> : ...}` in the position matching the existing chain's structure.

- [ ] **Step 3: Verify it compiles**

```bash
cd /root/projects/Deadband/web && pnpm build && pnpm lint
```

Expected: clean. A TypeScript error about the mode union not being exhaustive means the branch was added to the segmented control but not to the render chain.

- [ ] **Step 4: Add the table styling**

In `web/src/styles.css`, follow the existing `.legs-table` rules -- reuse them by adding `.marks-table` to the same selector rather than writing a second copy:

```bash
cd /root/projects/Deadband && grep -n "legs-table" web/src/styles.css
```

Add `.marks-table` alongside `.legs-table` in each rule it appears in, and add `.marks .asof { display: block; margin-bottom: 1rem; }` and `.marks .actions { display: flex; justify-content: space-between; align-items: center; margin-top: 1rem; }`.

- [ ] **Step 5: See it in the real app**

Serve the built frontend from the API with writes enabled and a trusted login, bound to localhost only:

```bash
cd /root/projects/Deadband/web && pnpm build && cd /root/projects/Deadband && DEADBAND_ENABLE_WRITES=1 DEADBAND_TRUSTED_LOGINS=devtest@example.invalid uv run uvicorn api.app:app --host 127.0.0.1 --port 8400
```

Then in a second terminal, confirm the route serves rows and that a mark round-trips. The identity header is what the proxy injects in production; supplying it here is how a local request stands in for a proxied one:

```bash
curl -s -H 'Tailscale-User-Login: devtest@example.invalid' http://127.0.0.1:8400/api/marks | head -40
```

Expected: a `marks` array with one entry per held instrument. Stop the server when done.

- [ ] **Step 6: Commit**

```bash
cd /root/projects/Deadband && git add web/src/screens/Marks.tsx web/src/screens/Entry.tsx web/src/styles.css && git commit -m "feat(web): bulk marks entry on the Entry screen"
```

---

### Task 8: The `Snapshot` screen -- statement entry with a ledger comparison

**Files:**
- Create: `web/src/screens/Snapshot.tsx`
- Modify: `web/src/screens/Entry.tsx` (add the segment)

**Interfaces:**
- Consumes: `fetchAccounts`, `fetchDashboard`, `fetchSnapshot`, `createSnapshot` (Task 6 and existing); `money` from `format.ts`.
- Produces: `export default function Snapshot()`.

**The comparison panel must not claim an agreement nobody verified.** `db/cash.py`'s `account_cash` and `db/positions.py`'s `open_positions` take **no `as_of`** -- both compute from the entire ledger as of *now*. The dashboard's per-account `cash` and `equity` are therefore today's figures. Comparing them to a July 31 statement and printing a tick would assert something false. This is not new to this feature; `cli.py`'s `reconcile` has the same property. So: label the column **"ledger, as of now"**, show the difference, and show a verdict **only when the statement date is today**. A transposed cash/equity pair still stands out -- it appears as two large, offsetting differences -- which is the reason the panel exists.

**Both figures are frequently unavailable, and that is correct.** `api/dashboard.py` nulls an account's `equity` whenever it holds anything unvalued, and nulls `cash` on `MixedCurrencyError`. Before any marks exist, *every* account's equity is null, so this panel is empty on first use by construction. It is an assist, never a gate: the form saves regardless.

- [ ] **Step 1: Write `web/src/screens/Snapshot.tsx`**

```typescript
import { useEffect, useState } from 'react'
import {
  createSnapshot,
  fetchAccounts,
  fetchDashboard,
  fetchSnapshot,
  type AccountSummary,
  type AccountTile,
  type StoredSnapshot,
} from '../api'
import { money } from '../format'

function todayLocal(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

// The difference the panel reports, or null when either side is unavailable.
function diff(typed: string, ledger: string | null): string | null {
  if (ledger == null || typed.trim() === '') return null
  const d = Number(ledger) - Number(typed)
  if (!Number.isFinite(d)) return null
  return d.toFixed(2)
}

export default function Snapshot() {
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [tiles, setTiles] = useState<AccountTile[]>([])
  const [account, setAccount] = useState('')
  const [asOf, setAsOf] = useState(todayLocal)
  const [cash, setCash] = useState('')
  const [equity, setEquity] = useState('')
  const [note, setNote] = useState('')
  const [existing, setExisting] = useState<StoredSnapshot | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<string | null>(null)

  useEffect(() => {
    fetchAccounts()
      .then((r) => setAccounts(r.accounts))
      .catch((err) => setError(String(err instanceof Error ? err.message : err)))
    // The dashboard already computes per-account cash and equity; this panel
    // reads them rather than adding a second endpoint that would compute the
    // same figures a second way and be free to disagree.
    fetchDashboard()
      .then((d) => setTiles(d.accounts))
      .catch(() => setTiles([]))
  }, [])

  // Whether saving REPLACES an existing row. add_snapshot's ON CONFLICT fires
  // on an exact (account_id, as_of) match, so this asks for exactly that date
  // -- not the latest on or before it, which would warn about replacing July's
  // statement while entering August's.
  useEffect(() => {
    if (!account || !asOf) {
      setExisting(null)
      return
    }
    let current = true
    fetchSnapshot(account, asOf)
      .then((r) => {
        if (current) setExisting(r.snapshot)
      })
      .catch(() => {
        if (current) setExisting(null)
      })
    return () => {
      current = false
    }
  }, [account, asOf])

  const tile = tiles.find((t) => t.id === account) ?? null
  // The ledger side is computed as of NOW -- account_cash and open_positions
  // take no as_of. A tick against a past statement date would assert an
  // agreement nobody checked, so the verdict is shown only when the statement
  // date IS today. The raw difference is always shown: a transposed
  // cash/equity pair appears as two large offsetting differences either way,
  // which is what this panel is for.
  const comparable = asOf === todayLocal()

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (busy || !account || !asOf || cash.trim() === '' || equity.trim() === '') return
    setBusy(true)
    setError(null)
    setSaved(null)
    try {
      const r = await createSnapshot({
        account_id: account,
        as_of: asOf,
        cash_balance: cash.trim(),
        total_equity: equity.trim(),
        note: note.trim() === '' ? null : note.trim(),
      })
      setSaved(
        r.replaced
          ? `snapshot for ${asOf} replaced`
          : `snapshot stored for ${asOf}`,
      )
      setCash('')
      setEquity('')
      setNote('')
      const again = await fetchSnapshot(account, asOf)
      setExisting(again.snapshot)
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form className="snapshot" onSubmit={submit}>
      <label>
        account
        <select value={account} onChange={(e) => setAccount(e.target.value)} required>
          <option value="">choose an account…</option>
          {accounts.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name} — {a.venue}
            </option>
          ))}
        </select>
      </label>

      <label>
        as of
        {/* A DATE, not a datetime: `snapshot add` runs its as_of through
            _parse_as_of, where a bare date becomes midnight UTC. That also
            sidesteps the datetime-local offset hazard entirely -- there is no
            wall-clock time here to misinterpret. */}
        <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)} required />
        <span className="muted"> the statement date, not today</span>
      </label>

      <table className="compare">
        <thead>
          <tr>
            <th />
            <th className="num">you type</th>
            <th className="num">ledger, as of now</th>
            <th className="num">difference</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <th scope="row">cash</th>
            <td className="num">
              <input
                type="text" inputMode="decimal" value={cash} required
                onChange={(e) => setCash(e.target.value)} aria-label="cash balance"
              />
            </td>
            <td className="num">{money(tile?.cash ?? null)}</td>
            <td className="num">{money(diff(cash, tile?.cash ?? null))}</td>
          </tr>
          <tr>
            <th scope="row">total equity</th>
            <td className="num">
              <input
                type="text" inputMode="decimal" value={equity} required
                onChange={(e) => setEquity(e.target.value)} aria-label="total equity"
              />
            </td>
            <td className="num">{money(tile?.equity ?? null)}</td>
            <td className="num">{money(diff(equity, tile?.equity ?? null))}</td>
          </tr>
        </tbody>
      </table>

      {!comparable && (
        <div className="muted">
          the ledger column is today's position, not {asOf}'s — treat the difference as a
          sanity check on what you typed, not as a reconciliation
        </div>
      )}
      {tile !== null && tile.equity == null && (
        <div className="muted">
          ledger equity is unavailable for this account — something it holds has no usable
          mark. Record marks first if you want the comparison.
        </div>
      )}

      <label>
        note
        <input type="text" value={note} onChange={(e) => setNote(e.target.value)} />
      </label>

      {existing !== null && (
        <div className="warn">
          a snapshot already exists for {asOf} — saving replaces it (cash{' '}
          {money(existing.cash_balance)}, equity {money(existing.total_equity)})
        </div>
      )}

      <button type="submit" disabled={busy}>
        {busy ? 'saving…' : 'Save snapshot'}
      </button>

      {error && <div className="error">{error}</div>}
      {saved && <div className="ok">{saved}</div>}
    </form>
  )
}
```

- [ ] **Step 2: Add the segment to `Entry.tsx`**

Import it, add the fifth button following the same shape as the others, and add the render branch:

```typescript
import Snapshot from './Snapshot'
```

```tsx
        <button
          type="button" role="tab" aria-selected={mode === 'snapshot'}
          className={mode === 'snapshot' ? 'active' : undefined}
          onClick={() => setMode('snapshot')}
        >
          Snapshot
        </button>
```

- [ ] **Step 3: Confirm the types it consumes actually exist**

`AccountSummary` and `AccountTile` are imported from `../api`. Verify both are exported and that `AccountTile` really carries `cash` and `equity` as `string | null`:

```bash
cd /root/projects/Deadband && grep -n "interface AccountTile" -A 14 web/src/api.ts
```

If `cash`/`equity` are typed differently, match the import to what is actually there rather than changing the interface -- it describes a payload this task does not own.

- [ ] **Step 4: Verify it compiles**

```bash
cd /root/projects/Deadband/web && pnpm build && pnpm lint
```

Expected: clean.

- [ ] **Step 5: Add the styling**

In `web/src/styles.css`, add:

```css
.snapshot label { display: block; margin-bottom: 0.75rem; }
.compare { width: 100%; margin: 1rem 0; }
.compare th[scope='row'] { text-align: left; font-weight: normal; }
.warn { border-left: 3px solid var(--warn, #b8860b); padding-left: 0.75rem; margin: 0.75rem 0; }
```

Check first whether a `--warn` custom property or a `.warn` class already exists and reuse it rather than defining a second:

```bash
cd /root/projects/Deadband && grep -n "warn\|--warn" web/src/styles.css
```

- [ ] **Step 6: See it in the real app**

Rebuild and run as in Task 7 Step 5, then exercise the round trip end to end:

```bash
cd /root/projects/Deadband && curl -s -X POST http://127.0.0.1:8400/api/snapshots \
  -H 'Content-Type: application/json' \
  -H 'Tailscale-User-Login: devtest@example.invalid' \
  -d '{"account_id":"<a real account id>","as_of":"2026-07-31","cash_balance":"1204.11","total_equity":"30184.22","note":null}'
```

Expected: `{"account_id": "...", "as_of": "2026-07-31T00:00:00+00:00", "replaced": false}`. Run it a second time and confirm `"replaced": true` and that only one row exists.

- [ ] **Step 7: Commit**

```bash
cd /root/projects/Deadband && git add web/src/screens/Snapshot.tsx web/src/screens/Entry.tsx web/src/styles.css && git commit -m "feat(web): statement snapshot entry with a ledger comparison"
```

---

### Task 9: Mutation-test the guards, then record what this leaves open

**Files:**
- Modify: `docs/known-gaps.md`
- No source changes expected -- this task verifies the tests are not vacuous and documents the residue.

**Why this task exists.** Three vacuous tests shipped or nearly shipped in the previous session: a route guard that inspected zero routes, an httpx traversal test whose `//path` was rewritten client-side before reaching the app, and security tests that skipped in CI for want of a build artifact. **Reading a test is not evidence that it tests anything.** Each guard below gets its protection deleted, the suite run, and the guard restored -- a test that stays green with the guard gone is worthless and must be fixed before this branch merges.

- [ ] **Step 1: Mutate the negative-price refusal**

In `api/marks.py`, delete these two lines from `create_marks`:

```python
        if price < 0:
            raise HTTPException(422, f"marks[{i}].price: {m.price!r} must not be negative")
```

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_marks.py -v`
Expected: **`test_post_marks_refuses_a_negative_price` FAILS.** It should now get a 500 (an uncaught `CheckViolationError` from `mark_price_chk`) instead of a 422. If it still passes, the test is not reaching the database and must be fixed.

Restore the lines with `git checkout api/marks.py` and re-run to confirm green.

- [ ] **Step 2: Mutate the duplicate-instrument refusal**

Delete the `seen`/duplicate loop from `create_marks` (keeping `seen` populated for the instrument-existence check below it -- replace it with `seen = {m.instrument_id for m in body.marks}`).

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_marks.py::test_post_marks_refuses_a_duplicate_instrument -v`
Expected: **FAIL** -- the request now returns 201 with `marks_set == 2` while only one row was written, which is exactly the silent last-one-wins this guard prevents.

Restore with `git checkout api/marks.py`.

- [ ] **Step 3: Mutate the `is_finite` guard**

In `api/validation.py`, delete these two lines from `parse_decimal`:

```python
    if not value.is_finite():
        raise HTTPException(422, f"{field}: {raw!r} must be a finite number")
```

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_validation.py tests/api/test_snapshots.py -v`
Expected: **`test_parse_decimal_refuses_non_finite` and `test_post_snapshot_refuses_a_nan_figure` both FAIL.** The snapshot one is the important half -- it proves a NaN really does reach `account_snapshot`, which has no CHECK constraint to stop it.

Restore with `git checkout api/validation.py`.

- [ ] **Step 4: Mutate the identity dependency**

In `api/snapshots.py`, delete the `_identity: str = Depends(require_trusted_identity),` line from `create_snapshot`.

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_write_identity.py -v`
Expected: **`test_every_write_route_requires_identity` FAILS**, and all three `post-snapshot` parametrized cases FAIL. If the structural test passes while the HTTP ones fail, the route walk is not seeing this route -- investigate before restoring, because that is the same shape as the guard that was found inspecting zero routes.

Restore with `git checkout api/snapshots.py`.

- [ ] **Step 5: Confirm the exact-date lookup is really exact**

In `api/snapshots.py`, replace the `fetchrow` in `snapshot_for_date` with a `latest_snapshot(conn, account_id, parsed)` call.

Run: `cd /root/projects/Deadband && uv run pytest tests/api/test_snapshots.py::test_get_snapshot_does_not_fall_back_to_an_earlier_one -v`
Expected: **FAIL.** That test exists solely to catch this substitution.

Restore with `git checkout api/snapshots.py`.

- [ ] **Step 6: Run the whole thing**

**Every pytest command uses `--env-file .env`.** `TEST_PG_DSN` is not in the
shell environment and there is no `.envrc`; a bare `uv run pytest` skips the
entire DB and API lane and reports green. That is this repo's documented worst
failure mode, and CI carries a dedicated step asserting `skipped == 0`.

The API lane fits a foreground run comfortably. Call it directly and let it
block; a 600000 ms tool timeout covers it. Piped output buffers, so an empty
output file mid-run is expected and is **not** evidence of a hang.

```bash
uv run --env-file .env pytest tests/api/ -q
```

Expected: PASS, **0 skipped**. Roughly 110s at this size.

**The DB lane does NOT fit.** Measured at 616.76s on this box against the Bash
tool's 600000 ms ceiling — the harness auto-backgrounds it and no timeout
prevents that. Run it **per file**, in the foreground; each file is minutes,
not ten. Take the file list first:

```bash
cd /root/projects/.wt/marks-snapshot-entry && ls tests/db/
```

Then run each file as its own foreground command, e.g.:

```bash
cd /root/projects/.wt/marks-snapshot-entry && uv run --env-file .env pytest tests/db/test_cli.py -q
```

Do not background any of them, and do not substitute one whole-lane run. If a
file's run appears to hang, that is buffered output — wait for it.

The pure lane is fast and fits easily:

```bash
cd /root/projects/.wt/marks-snapshot-entry && uv run --env-file .env pytest tests/ --ignore=tests/db --ignore=tests/api -q
```

Expected: 444 passed, 0 skipped. If it reports 443 passed / 1 skipped, the
owner-local `imports/` directory is missing from this worktree — that skip is
environmental, not a regression, but say so in the report rather than
accepting it silently.

- [ ] **Step 7: Record the residue in `docs/known-gaps.md`**

The file is organised as dated sections, each introduced by a short paragraph
and followed by a `| # | Gap | Why it matters |` table -- **not** as `####`
headings. Confirm the last number used before writing:

```bash
cd /root/projects/Deadband && grep -nE "^\| [0-9]+ \|" docs/known-gaps.md | tail -3
```

Expected: the highest is `70`. If it is not, renumber the rows below to
continue from whatever it actually is.

Append a new section in the existing shape:

```markdown
---

## Found while adding marks and snapshot entry (2026-08-28)

Plan 3 of the Entry & Import milestone put the two remaining CLI writes
(`marks set`, `snapshot add`) behind forms. Three limits are worth recording
because each is a deliberate choice that reads like an oversight from the
screen itself.

| # | Gap | Why it matters |
|---|---|---|
| 71 | **The snapshot form's ledger comparison is as-of-now, not as-of-statement.** | `db/cash.py`'s `account_cash` and `db/positions.py`'s `open_positions` take no `as_of` — both compute from everything in the ledger at the moment they are called. The Snapshot screen's "ledger, as of now" column is therefore today's figure beside a statement that may be months old, and the screen says so rather than printing a verdict: a tick appears only when the statement date IS today. This is not new to the form — `cli.py`'s `reconcile` has exactly the same property, where `--as-of` selects WHICH snapshot to compare against while the ledger side is always the present. Closing it means historical position and cash reconstruction, which is a feature, not a fix. The comparison still earns its place: a transposed cash/equity pair shows as two large offsetting differences regardless of date, which is the failure `add_snapshot`'s keyword-only signature exists to prevent and the one a human is most likely to commit at the keyboard. |
| 72 | **The marks table lists holdings, so an instrument sold to zero cannot be re-marked from the UI.** | `GET /api/marks` is built from `open_positions`, so it lists only what the ledger currently holds. Correcting a mark on a closed position — which still affects the historical unrealized figure on the Trade detail screen — needs `marks set` from the CLI. Deliberate: the table exists to light up the Dashboard, and listing every instrument ever traded would bury the handful that matter behind a list that only grows. |
| 73 | **The frontend has no test runner, so `toInstant` is protected by reuse rather than by assertion.** | `web/` has no vitest and no test files; `package.json` exposes only `dev`/`build`/`lint`/`preview`. The `datetime-local` → `toISOString()` conversion is the one frontend behaviour with a known silent-corruption mode — stamping `Z` shifts every value by the browser's offset, and grouping orders fills by `executed_at`, so it can reorder trades. **This box runs UTC, so the bug is invisible to any test run here anyway**, which is why a green local run has never been evidence about it. Mitigated structurally instead: one exported `toInstant` in `web/src/datetime.ts`, imported by every caller, so there is no second copy to get it wrong. Adding a frontend test runner is its own decision and should not ride in on a feature branch. |
```

- [ ] **Step 8: Commit**

```bash
cd /root/projects/Deadband && git add docs/known-gaps.md && git commit -m "docs: record the gaps marks and snapshot entry leave open"
```

- [ ] **Step 9: Confirm the tree is clean before handing off**

Mutation testing edits source files and restores them. Verify nothing is still mutated -- trust the tree, not your recollection of having restored it:

```bash
cd /root/projects/Deadband && git status --short && git diff --stat
```

Expected: empty output. **A non-empty diff here means a mutation is still applied.** That has happened before on this project: a subagent left a mutation on a tracked source file after a stalled run. Restore with `git checkout <file>` and re-run Step 6's two lanes before continuing.

---

## Self-Review

**Spec coverage.** Section 4 asks for "two small forms over `marks set` and `snapshot add`" that light up "equity, unrealized P&L, drift, and the Accounts screen's rules panel". Tasks 2-3 cover marks; Tasks 4-5 cover snapshots; Tasks 7-8 are the forms. E3 (scope), E6 (CLI-first, one write path — satisfied already, since `db/marks.py` and `db/snapshots.py` predate this plan and the API calls those same functions) and section 6 (identity on every write) are covered by Tasks 3, 5 and 9. Section 5's "money arrives as strings, parsed straight to Decimal" is a Global Constraint and is asserted in every API test via `assert_no_json_floats`.

**Not covered, deliberately:** the Accounts screen's headroom/rules panel is not modified. It reads from `account_snapshot` already (`api/accounts.py`), so it starts working once a snapshot exists — no code change is needed, and the executor should verify that claim in Task 8 Step 6 rather than assume it.

**Placeholders:** none. Every code step carries the actual code; every command names its expected output.

**Type consistency:** `MarkRow`/`MarkIn`/`SetMarksResult`/`StoredSnapshot`/`SnapshotIn`/`CreatedSnapshot` are defined in Task 6 and used with the same field names in Tasks 7 and 8. `parse_decimal`/`parse_instant`/`parse_as_of`/`refuse_future` are defined in Task 1 and used with the same signatures in Tasks 3, 4 and 5. `MARK_FUTURE_TOLERANCE` is defined once in `db/marks.py` (Task 1) and imported everywhere else. `toInstant` gains an optional second parameter in Task 6 and both existing `Entry.tsx` callers keep the default.

**Checked against the tree while writing, not assumed:** `AccountTile.cash` and
`.equity` really are `string | null` (`web/src/api.ts:17-18`); `db/accounts.py`
exports `create_account` and `get_account`; `db/fills.py` exports
`insert_fills`; `web/src/api.ts` already has a `send<T>` helper, so Task 6 uses
it rather than adding a second; and `docs/known-gaps.md` is dated sections with
markdown tables rather than `####` headings, so Task 9 Step 7 matches that
shape. Task 8 Step 3 re-checks `AccountTile` at execution time anyway, since
the file may move underneath this plan.

**One claim left for the executor to verify rather than trust:** that the
Accounts screen's rules/headroom panel starts working with no code change once
a snapshot exists. `api/accounts.py` reads `account_snapshot` already, but this
plan does not modify that screen and the claim is untested — Task 8 Step 6 is
where to confirm it.
