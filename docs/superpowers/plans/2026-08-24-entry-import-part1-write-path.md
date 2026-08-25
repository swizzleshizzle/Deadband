# Entry & Import part 1 — write plumbing and manual fill entry

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the UI its first write path — a keyboard-first form that records manual fills and can delete them — without spending milestone 1's read-only Postgres guarantee.

**Architecture:** A second, write-enabled asyncpg pool lives beside the read-only one; read handlers keep the read-only pool and stay structurally incapable of writing. Write logic lands in `db/`, CLI commands wrap it, API endpoints call the same functions. Write routes are registered only when `DEADBAND_ENABLE_WRITES` is set, so the published instance has no write surface at all.

**Tech Stack:** Python 3.12, asyncpg, FastAPI, pytest (asyncio auto mode), React 19 + Vite + TypeScript.

**Spec:** `docs/superpowers/specs/2026-08-24-entry-import-design.md`

**This is plan 1 of 3.** Plan 2 is the CSV import wizard (spec §4 "Import"); plan 3 is the marks/snapshot forms (spec §4 "Marks"). Both depend on the plumbing built here and neither depends on the other.

## Global Constraints

- **Money and quantities cross the wire as strings, both directions** (spec §5). Never `type="number"`, never `Number(value)` in TypeScript. Use `type="text"` with `inputMode="decimal"`.
- **Every write endpoint wraps `write + regroup_account` in one `async with conn.transaction():`** (spec §2), matching `cli.py:1101-1108`.
- **`request.client.host` must never be used as an access control** (spec §6). The deployment proxies every path to the local port, so the proxy is the client and the check passes for remote requests.
- **DB tests run in the foreground** and their summary line must be read: `set -a && . ./.env && set +a && uv run pytest <file>`. Never pipe to `tail` — it swallows the exit code.
- **This repo is public.** No host identities, tailnet names, or deployment topology in any tracked file. The pre-commit hook enforces this against a deny-list in gitignored `docs/ops/`.
- Decimals are rendered by `api/serialization.py:DeadbandJSONResponse`. Return that class directly from handlers, never a bare dict.

## Decision made during planning: manual fills are NOT content-hash deduped

`db/importing.py:_fill_dedupe_keys` hashes a fill without a `venue_fill_id` on `(executed_at, symbol, side, quantity, price)` plus an occurrence index that only disambiguates **within one import batch**.

Applied to manual entry that is actively wrong. Buying 100 of the same symbol at the same price twice in the same minute is a real thing a person does, and across two separate form submissions the occurrence index cannot tell them apart — the second fill would hash identically and be silently dropped by `ON CONFLICT DO NOTHING`. The user would type it, see success, and it would not exist.

**Silent loss is the failure mode this codebase treats as worst.** So manual fills are written with `venue_fill_id = NULL` and `content_hash = NULL`. Both unique indexes are partial (`WHERE ... IS NOT NULL`), so neither applies and every submitted fill lands. The trade is that a double-submitted form creates two fills — visible, and removable via `fills rm` and the delete control built in Task 6. A visible duplicate the user can delete beats an invisible drop they cannot detect.

---

### Task 1: Write-enabled pool, and a guard that keeps reads read-only

**Files:**
- Modify: `api/deps.py`
- Modify: `tests/api/conftest.py`
- Test: `tests/api/test_write_pool.py`

**Interfaces:**
- Consumes: `db/pool.py:create_pool(**kwargs)`, existing `api/deps.py:get_conn`
- Produces: `api/deps.py:get_write_conn(request) -> AsyncIterator[asyncpg.Connection]`, `api/deps.py:ensure_write_pool(app) -> asyncpg.Pool`

- [ ] **Step 1: Write the failing test**

```python
# tests/api/test_write_pool.py
"""The read pool must stay read-only, and every route must draw from the pool
its HTTP method implies (spec section 2)."""

from fastapi.routing import APIRoute

from api.app import create_app
from api.deps import get_conn, get_write_conn
from tests.conftest import requires_db

pytestmark = requires_db

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _dependency_names(route: APIRoute) -> set[str]:
    return {d.call.__name__ for d in route.dependant.dependencies if d.call is not None}


def test_every_route_uses_the_pool_its_method_implies():
    """A POST that forgets get_write_conn would silently run inside a read-only
    transaction; a GET that reaches for the write pool quietly gives up the
    guarantee D3 exists to provide. Both fail here rather than in review."""
    app = create_app(enable_writes=True)
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        deps = _dependency_names(route)
        if not deps & {get_conn.__name__, get_write_conn.__name__}:
            continue
        writes = bool(route.methods & _WRITE_METHODS)
        expected = get_write_conn.__name__ if writes else get_conn.__name__
        forbidden = get_conn.__name__ if writes else get_write_conn.__name__
        assert expected in deps, f"{sorted(route.methods)} {route.path} must use {expected}"
        assert forbidden not in deps, f"{sorted(route.methods)} {route.path} must not use {forbidden}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/test_write_pool.py -v`
Expected: FAIL — `ImportError: cannot import name 'get_write_conn' from 'api.deps'`

- [ ] **Step 3: Add the write pool**

```python
# api/deps.py — append after ensure_pool / get_conn

async def ensure_write_pool(app) -> asyncpg.Pool:
    """The write pool, created lazily and separately from the read pool.

    Deliberately NOT the same pool with the flag flipped per transaction:
    `default_transaction_read_only` is a server setting applied at connection
    time, so one pool cannot be both. Two pools make the read guarantee a
    property of which dependency a handler declares, which the test in
    tests/api/test_write_pool.py can then check mechanically.
    """
    if getattr(app.state, "write_pool", None) is None:
        app.state.write_pool = await create_pool()
    return app.state.write_pool


async def get_write_conn(request: Request) -> AsyncIterator[asyncpg.Connection]:
    pool = await ensure_write_pool(request.app)
    async with pool.acquire() as conn:
        yield conn
```

Also set `app.state.write_pool = None` alongside `app.state.pool = None` in `api/app.py:create_app`.

- [ ] **Step 4: Point the test fixtures at both pools**

```python
# tests/api/conftest.py — in the api_app fixture, after app.state.pool assignment
    app.state.write_pool = _FixturePool(conn)
```

The same rollback-per-test connection backs both, so a write endpoint's changes are visible to a read endpoint in the same test and vanish afterward.

- [ ] **Step 5: Run test to verify it passes**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/test_write_pool.py -v`
Expected: PASS (it passes vacuously — no write routes exist yet — and becomes load-bearing in Task 4)

- [ ] **Step 6: Confirm the existing read-only guarantee is untouched**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/test_readonly.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add api/deps.py api/app.py tests/api/conftest.py tests/api/test_write_pool.py
git commit -m "feat(api): add a write-enabled pool beside the read-only one

Reads keep default_transaction_read_only. A route/pool guard test asserts
every /api route draws from the pool its HTTP method implies, so a POST that
forgets get_write_conn fails CI rather than silently running read-only."
```

---

### Task 2: `db/fills.py` gains manual add and delete

**Files:**
- Modify: `db/fills.py`
- Test: `tests/db/test_fills_manual.py`

**Interfaces:**
- Consumes: `db/fills.py:insert_fills(conn, fills) -> InsertResult`, `ledger/types.py:Fill`, `FillSource`
- Produces: `add_manual_fills(conn, fills: list[Fill]) -> list[UUID]`, `delete_manual_fill(conn, fill_id: UUID) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/db/test_fills_manual.py
"""Manual fill add/delete (spec E5). All values invented."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from db.accounts import create_account
from db.fills import add_manual_fills, delete_manual_fill, insert_fills
from db.instruments import upsert_instrument
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db

_T = datetime(2026, 6, 1, 15, 30, tzinfo=UTC)


async def _account_and_instrument(conn):
    acc = await create_account(conn, name="ManualEntry", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZE", quote_currency="USD"),
    )
    return acc, inst


def _manual_fill(acc, inst, *, qty="10", price="5"):
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=_T,
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=None,
        is_estimated=False,
    )


async def test_two_identical_manual_fills_both_land(conn):
    """Buying the same thing twice at the same instant is a real event. The
    import dedupe hashes on (time, symbol, side, qty, price), so if manual
    fills carried a content_hash the second would be silently dropped -- the
    user would type it, see success, and it would not exist."""
    acc, inst = await _account_and_instrument(conn)
    ids = await add_manual_fills(conn, [_manual_fill(acc, inst), _manual_fill(acc, inst)])
    assert len(ids) == 2
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 2


async def test_manual_fills_carry_no_dedupe_keys(conn):
    acc, inst = await _account_and_instrument(conn)
    await add_manual_fills(conn, [_manual_fill(acc, inst)])
    row = await conn.fetchrow(
        "SELECT source, venue_fill_id, content_hash FROM fill WHERE account_id = $1", acc
    )
    assert row["source"] == "manual"
    assert row["venue_fill_id"] is None
    assert row["content_hash"] is None


async def test_add_manual_fills_refuses_a_non_manual_fill(conn):
    """The function names its contract; a csv-sourced fill routed through here
    would bypass the import path's dedupe entirely."""
    acc, inst = await _account_and_instrument(conn)
    from dataclasses import replace

    with pytest.raises(ValueError, match="manual"):
        await add_manual_fills(conn, [replace(_manual_fill(acc, inst), source=FillSource.CSV)])


async def test_delete_manual_fill_removes_it(conn):
    acc, inst = await _account_and_instrument(conn)
    (fill_id,) = await add_manual_fills(conn, [_manual_fill(acc, inst)])
    assert await delete_manual_fill(conn, fill_id) is True
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE id = $1", fill_id) == 0


async def test_delete_manual_fill_refuses_an_imported_fill(conn):
    """Imported fills are reproducible from their export; deleting one invites
    divergence from the source of truth (spec E5). Enforced in SQL, so a
    caller that forgets to check cannot bypass it."""
    acc, inst = await _account_and_instrument(conn)
    from dataclasses import replace

    imported = replace(_manual_fill(acc, inst), source=FillSource.CSV, venue_fill_id="v1")
    await insert_fills(conn, [imported])
    assert await delete_manual_fill(conn, imported.id) is False
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE id = $1", imported.id) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_fills_manual.py -v`
Expected: FAIL — `ImportError: cannot import name 'add_manual_fills' from 'db.fills'`

- [ ] **Step 3: Implement both functions**

```python
# db/fills.py — append

async def add_manual_fills(conn: asyncpg.Connection, fills: list[Fill]) -> list[UUID]:
    """Insert hand-entered fills and return their ids, in order.

    Manual fills deliberately carry NEITHER dedupe key. venue_fill_id is None
    because no venue issued them, and content_hash is None because the import
    hash -- (executed_at, symbol, side, quantity, price) plus a within-batch
    occurrence index -- cannot distinguish two genuinely identical manual
    entries made in separate submissions. Hashing them would silently drop the
    second, which is worse than a visible duplicate the user can delete: both
    partial unique indexes skip NULLs, so every fill here lands.
    """
    for f in fills:
        if f.source is not FillSource.MANUAL:
            raise ValueError(f"add_manual_fills got a {f.source.value} fill; expected manual")
        if f.content_hash is not None or f.venue_fill_id is not None:
            raise ValueError("manual fills must carry neither venue_fill_id nor content_hash")
    await insert_fills(conn, fills)
    return [f.id for f in fills]


async def delete_manual_fill(conn: asyncpg.Connection, fill_id: UUID) -> bool:
    """Delete a hand-entered fill. Returns False if no such fill exists OR it
    was imported -- the source check lives in the WHERE clause so it cannot be
    bypassed by a caller that forgets to make it."""
    result = await conn.execute(
        "DELETE FROM fill WHERE id = $1 AND source = 'manual'", fill_id
    )
    return result != "DELETE 0"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_fills_manual.py -v`
Expected: PASS, 5 passed

- [ ] **Step 5: Commit**

```bash
git add db/fills.py tests/db/test_fills_manual.py
git commit -m "feat(db): add and delete hand-entered fills

Manual fills carry neither dedupe key on purpose: the import content_hash
cannot tell two genuinely identical manual entries apart across submissions,
so hashing them would silently drop the second. delete refuses non-manual
fills in SQL rather than in the caller."
```

---

### Task 3: CLI commands `fills add` and `fills rm`

**Files:**
- Modify: `cli.py`
- Test: `tests/db/test_cli.py`

**Interfaces:**
- Consumes: `db/fills.py:add_manual_fills`, `delete_manual_fill`; `db/trades.py:regroup_account`; `db/instruments.py:upsert_instrument`
- Produces: `cli.py:cmd_fills_add(args) -> int`, `cli.py:cmd_fills_rm(args) -> int`

- [ ] **Step 1: Write the failing tests**

```python
# tests/db/test_cli.py — append. Follows this file's established pattern:
# call the cmd_ function directly with a SimpleNamespace of args, and
# monkeypatch cli.create_pool to hand back the test's own connection.

from types import SimpleNamespace


def _fills_add_args(*, account, symbol, side="buy", quantity="5", price="20",
                    fee="0", fee_currency="USD", executed_at="2026-06-01T15:30:00+00:00"):
    return SimpleNamespace(
        account=str(account), symbol=symbol, side=side, quantity=quantity,
        price=price, fee=fee, fee_currency=fee_currency, executed_at=executed_at,
    )


async def test_fills_add_creates_a_fill_and_regroups(conn, monkeypatch, capsys):
    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    acc = await create_account(conn, name="CliEntry", venue="manual", account_type="cash")

    rc = await cli.cmd_fills_add(_fills_add_args(account=acc, symbol="ZZC"))
    assert rc == 0, capsys.readouterr().err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 1
    assert await conn.fetchval("SELECT count(*) FROM trade WHERE account_id = $1", acc) == 1


async def test_fills_add_rejects_a_blank_symbol(conn, monkeypatch):
    """Issue #27: an instrument was minted with symbol='' and became an unnamed
    position that renders as a blank row. Manual entry must not be a second way
    to create one. Rejected before the pool opens -- whether the symbol is blank
    depends only on the argument."""
    async def fake_create_pool(*_a, **_kw):
        raise AssertionError("must not open a pool for an invalid symbol")

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    acc = await create_account(conn, name="CliBlank", venue="manual", account_type="cash")

    rc = await cli.cmd_fills_add(_fills_add_args(account=acc, symbol="   "))
    assert rc == 2
    assert await conn.fetchval("SELECT count(*) FROM instrument WHERE btrim(symbol) = ''") == 0


async def test_fills_rm_refuses_an_imported_fill(conn, monkeypatch, capsys):
    async def fake_create_pool(*_a, **_kw):
        return _FakePool(conn)

    monkeypatch.setattr(cli, "create_pool", fake_create_pool)
    acc = await create_account(conn, name="CliRm", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZD", quote_currency="USD"),
    )
    imported = Fill(
        id=uuid4(), account_id=acc, instrument_id=inst,
        executed_at=datetime(2026, 6, 1, 15, 30, tzinfo=UTC),
        side=Side.BUY, quantity=Decimal("1"), price=Decimal("1"), fee=Decimal("0"),
        fee_currency="USD", source=FillSource.CSV, venue_fill_id="v9", is_estimated=False,
    )
    await insert_fills(conn, [imported])

    rc = await cli.cmd_fills_rm(SimpleNamespace(id=str(imported.id)))
    assert rc == 1
    assert "immutable" in capsys.readouterr().err
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE id = $1", imported.id) == 1
```

Reuse the file's existing `_FakePool`, `cli` import, and fixtures rather than
redefining them.

- [ ] **Step 2: Run tests to verify they fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -k "fills_add or fills_rm" -v`
Expected: FAIL — argparse exits 2 with "invalid choice: 'fills'"

- [ ] **Step 3: Implement the commands**

```python
# cli.py — new command functions, following cmd_marks_set's structure

async def cmd_fills_add(args) -> int:
    symbol = (args.symbol or "").strip()
    if not symbol:
        print("error: --symbol must not be blank", file=sys.stderr)
        return 2
    try:
        quantity = Decimal(args.quantity)
        price = Decimal(args.price)
        fee = Decimal(args.fee)
    except InvalidOperation as exc:
        print(f"error: not a valid number: {exc}", file=sys.stderr)
        return 2
    if not all(v.is_finite() for v in (quantity, price, fee)):
        print("error: quantity, price and fee must be finite numbers", file=sys.stderr)
        return 2
    if quantity <= 0:
        print("error: --quantity must be positive", file=sys.stderr)
        return 2

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            account_id = UUID(args.account)
            if await get_account(conn, account_id) is None:
                print(f"error: no account {account_id}", file=sys.stderr)
                return 2
            async with conn.transaction():
                instrument_id = await upsert_instrument(
                    conn,
                    Instrument(
                        id=None,
                        asset_class=AssetClass.EQUITY,
                        symbol=symbol.upper(),
                        quote_currency="USD",
                    ),
                )
                fill = Fill(
                    id=uuid4(),
                    account_id=account_id,
                    instrument_id=instrument_id,
                    executed_at=datetime.fromisoformat(args.executed_at),
                    side=Side(args.side),
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    fee_currency=args.fee_currency,
                    source=FillSource.MANUAL,
                    venue_fill_id=None,
                    is_estimated=False,
                )
                (fill_id,) = await add_manual_fills(conn, [fill])
                await regroup_account(conn, account_id)
            print(fill_id)
            return 0
    finally:
        await pool.close()


async def cmd_fills_rm(args) -> int:
    try:
        fill_id = UUID(args.id)
    except ValueError:
        print(f"error: --id {args.id!r} is not a UUID", file=sys.stderr)
        return 2
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            account_id = await conn.fetchval("SELECT account_id FROM fill WHERE id = $1", fill_id)
            if account_id is None:
                print(f"error: no fill {fill_id}", file=sys.stderr)
                return 1
            async with conn.transaction():
                if not await delete_manual_fill(conn, fill_id):
                    print(
                        f"error: fill {fill_id} was imported, not hand-entered; "
                        "imported fills are immutable",
                        file=sys.stderr,
                    )
                    return 1
                await regroup_account(conn, account_id)
            return 0
    finally:
        await pool.close()
```

Register in `main()`'s subparsers, beside `p_marks`:

```python
    p_fills = sub.add_parser("fills", help="hand-entered fills")
    fills_sub = p_fills.add_subparsers(dest="fills_cmd", required=True)
    p_fills_add = fills_sub.add_parser("add", help="record a fill by hand")
    p_fills_add.add_argument("--account", required=True)
    p_fills_add.add_argument("--symbol", required=True)
    p_fills_add.add_argument("--side", required=True, choices=["buy", "sell"])
    p_fills_add.add_argument("--quantity", required=True)
    p_fills_add.add_argument("--price", required=True)
    p_fills_add.add_argument("--fee", default="0")
    p_fills_add.add_argument("--fee-currency", default="USD")
    p_fills_add.add_argument("--executed-at", required=True, help="ISO-8601 instant")
    p_fills_add.set_defaults(fn=cmd_fills_add)
    p_fills_rm = fills_sub.add_parser("rm", help="delete a hand-entered fill")
    p_fills_rm.add_argument("--id", required=True)
    p_fills_rm.set_defaults(fn=cmd_fills_rm)
```

Add `add_manual_fills, delete_manual_fill` to the existing `from db.fills import ...` line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py -k "fills_add or fills_rm" -v`
Expected: PASS, 3 passed

- [ ] **Step 5: Commit**

```bash
git add cli.py tests/db/test_cli.py
git commit -m "feat(cli): fills add and fills rm

Hand entry and its undo, at the command line first -- the API calls these same
db/ functions. A blank symbol is refused before any instrument is minted;
issue #27 is what that guard exists for."
```

---

### Task 4: `POST /api/fills` and `DELETE /api/fills/{id}`

**Files:**
- Create: `api/fills.py`
- Modify: `api/app.py`
- Test: `tests/api/test_fills_write.py`

**Interfaces:**
- Consumes: `api/deps.py:get_write_conn`, `db/fills.py:add_manual_fills`, `delete_manual_fill`, `db/trades.py:regroup_account`
- Produces: router mounted at `/api/fills`; request body `{"account_id": str, "fills": [{symbol, side, quantity, price, fee, fee_currency, executed_at}]}`; response `{"fill_ids": [str], "trades_regrouped": int}`

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_fills_write.py
"""POST /api/fills and DELETE /api/fills/{id} (spec section 3). All values invented."""

from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db


def _leg(symbol="ZZF", side="buy", qty="4", price="12.50"):
    return {
        "symbol": symbol, "side": side, "quantity": qty, "price": price,
        "fee": "0", "fee_currency": "USD", "executed_at": "2026-06-01T15:30:00Z",
    }


async def test_post_fills_creates_a_fill_and_regroups(client, conn):
    acc = await create_account(conn, name="ApiEntry", venue="manual", account_type="cash")
    r = await client.post("/api/fills", json={"account_id": str(acc), "fills": [_leg()]})
    assert r.status_code == 201
    body = r.json()
    assert_no_json_floats(body)
    assert len(body["fill_ids"]) == 1
    assert await conn.fetchval("SELECT count(*) FROM trade WHERE account_id = $1", acc) == 1


async def test_post_fills_writes_every_leg_in_one_transaction(client, conn):
    """Multi-leg is N fills in one request (spec E2/section 4). Four legs land
    together or not at all -- never two in and two rejected."""
    acc = await create_account(conn, name="ApiMultiLeg", venue="manual", account_type="cash")
    legs = [_leg(symbol=f"ZZL{i}") for i in range(4)]
    r = await client.post("/api/fills", json={"account_id": str(acc), "fills": legs})
    assert r.status_code == 201
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 4


async def test_post_fills_rolls_back_every_leg_when_one_is_invalid(client, conn):
    acc = await create_account(conn, name="ApiAtomic", venue="manual", account_type="cash")
    legs = [_leg(symbol="ZZG"), _leg(symbol="   ")]
    r = await client.post("/api/fills", json={"account_id": str(acc), "fills": legs})
    assert r.status_code == 422
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_post_fills_renders_quantities_as_strings(client, conn):
    acc = await create_account(conn, name="ApiStrings", venue="manual", account_type="cash")
    await client.post("/api/fills", json={"account_id": str(acc), "fills": [_leg(qty="0.00000001")]})
    got = await conn.fetchval("SELECT quantity FROM fill WHERE account_id = $1", acc)
    assert got == Decimal("0.00000001")


async def test_post_fills_404s_on_an_unknown_account(client):
    r = await client.post("/api/fills", json={"account_id": str(uuid4()), "fills": [_leg()]})
    assert r.status_code == 404


async def test_delete_fill_removes_a_manual_fill(client, conn):
    acc = await create_account(conn, name="ApiDelete", venue="manual", account_type="cash")
    posted = (await client.post("/api/fills", json={"account_id": str(acc), "fills": [_leg()]})).json()
    fill_id = posted["fill_ids"][0]
    assert (await client.delete(f"/api/fills/{fill_id}")).status_code == 204
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


async def test_delete_fill_409s_on_an_imported_fill(client, conn):
    from datetime import UTC, datetime
    from db.fills import insert_fills
    from db.instruments import upsert_instrument
    from ledger.types import AssetClass, Fill, FillSource, Instrument, Side

    acc = await create_account(conn, name="ApiImported", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZH", quote_currency="USD")
    )
    imported = Fill(
        id=uuid4(), account_id=acc, instrument_id=inst,
        executed_at=datetime(2026, 6, 1, tzinfo=UTC), side=Side.BUY,
        quantity=Decimal("1"), price=Decimal("1"), fee=Decimal("0"), fee_currency="USD",
        source=FillSource.CSV, venue_fill_id="v7", is_estimated=False,
    )
    await insert_fills(conn, [imported])
    assert (await client.delete(f"/api/fills/{imported.id}")).status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/test_fills_write.py -v`
Expected: FAIL — the SPA catch-all or a 405 answers `POST /api/fills`, not 201

- [ ] **Step 3: Implement the router**

```python
# api/fills.py
"""POST /api/fills and DELETE /api/fills/{id} (spec section 3).

Thin: every decision lives in db/fills.py and cli.py's commands call the same
functions. Money and quantities arrive as STRINGS and are parsed straight to
Decimal -- never through float (spec section 5).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_write_conn
from api.serialization import DeadbandJSONResponse
from db.accounts import get_account
from db.fills import add_manual_fills, delete_manual_fill
from db.instruments import upsert_instrument
from db.trades import regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side

router = APIRouter()


class LegIn(BaseModel):
    symbol: str
    side: str
    quantity: str
    price: str
    fee: str = "0"
    fee_currency: str = "USD"
    executed_at: str


class FillsIn(BaseModel):
    account_id: UUID
    fills: list[LegIn]


def _decimal(raw: str, field: str) -> Decimal:
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise HTTPException(422, f"{field}: {raw!r} is not a valid number") from None
    if not value.is_finite():
        raise HTTPException(422, f"{field}: {raw!r} must be finite")
    return value


@router.post("/api/fills", status_code=201)
async def create_fills(
    body: FillsIn, conn: asyncpg.Connection = Depends(get_write_conn)
) -> DeadbandJSONResponse:
    if not body.fills:
        raise HTTPException(422, "fills: at least one leg is required")
    if await get_account(conn, body.account_id) is None:
        raise HTTPException(404, "account not found")

    # Validate every leg BEFORE opening the transaction: a blank symbol on
    # leg 4 must not leave legs 1-3 written. The transaction below makes that
    # true even so, but failing early keeps the error clean.
    parsed = []
    for i, leg in enumerate(body.fills):
        symbol = leg.symbol.strip()
        if not symbol:
            raise HTTPException(422, f"fills[{i}].symbol: must not be blank")
        quantity = _decimal(leg.quantity, f"fills[{i}].quantity")
        if quantity <= 0:
            raise HTTPException(422, f"fills[{i}].quantity: must be positive")
        parsed.append(
            (
                symbol.upper(),
                Side(leg.side),
                quantity,
                _decimal(leg.price, f"fills[{i}].price"),
                _decimal(leg.fee, f"fills[{i}].fee"),
                leg.fee_currency,
                datetime.fromisoformat(leg.executed_at),
            )
        )

    async with conn.transaction():
        fills = []
        for symbol, side, quantity, price, fee, currency, executed_at in parsed:
            instrument_id = await upsert_instrument(
                conn,
                Instrument(
                    id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency=currency
                ),
            )
            fills.append(
                Fill(
                    id=uuid4(), account_id=body.account_id, instrument_id=instrument_id,
                    executed_at=executed_at, side=side, quantity=quantity, price=price,
                    fee=fee, fee_currency=currency, source=FillSource.MANUAL,
                    venue_fill_id=None, is_estimated=False,
                )
            )
        fill_ids = await add_manual_fills(conn, fills)
        regrouped = await regroup_account(conn, body.account_id)

    return DeadbandJSONResponse(
        {"fill_ids": fill_ids, "trades_regrouped": regrouped}, status_code=201
    )


@router.delete("/api/fills/{fill_id}", status_code=204)
async def remove_fill(fill_id: UUID, conn: asyncpg.Connection = Depends(get_write_conn)):
    account_id = await conn.fetchval("SELECT account_id FROM fill WHERE id = $1", fill_id)
    if account_id is None:
        raise HTTPException(404, "fill not found")
    async with conn.transaction():
        if not await delete_manual_fill(conn, fill_id):
            raise HTTPException(409, "imported fills are immutable")
        await regroup_account(conn, account_id)
    return DeadbandJSONResponse(None, status_code=204)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/test_fills_write.py tests/api/test_write_pool.py -v`
Expected: PASS, 8 passed. The route/pool guard from Task 1 is now load-bearing.

- [ ] **Step 5: Commit**

```bash
git add api/fills.py api/app.py tests/api/test_fills_write.py
git commit -m "feat(api): POST /api/fills and DELETE /api/fills/{id}

Multi-leg is N fills in one request, written in one transaction -- four legs
land together or not at all. Quantities parse from strings straight to
Decimal, never through float."
```

---

### Task 5: Gate write routes behind `DEADBAND_ENABLE_WRITES`

**Files:**
- Modify: `api/app.py`
- Test: `tests/api/test_write_gating.py`

**Interfaces:**
- Consumes: `api/fills.py:router`
- Produces: `api/app.py:create_app(enable_writes: bool | None = None) -> FastAPI`

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_write_gating.py
"""Spec section 6: the published instance has no write surface at all.

This is the ONLY control standing between a shared tailnet and unauthenticated
writes, so it is pinned here rather than left to the deployment. Note what is
deliberately NOT tested: a source-address check. The deployment proxies every
path to the local port, so the proxy is the client and request.client.host
reads 127.0.0.1 for remote callers -- such a check would pass for exactly the
requests it exists to stop.
"""

from fastapi.routing import APIRoute

from api.app import create_app


def _write_paths(app) -> set[str]:
    return {
        r.path
        for r in app.routes
        if isinstance(r, APIRoute) and r.methods & {"POST", "PUT", "PATCH", "DELETE"}
    }


def test_writes_are_absent_by_default(monkeypatch):
    monkeypatch.delenv("DEADBAND_ENABLE_WRITES", raising=False)
    assert _write_paths(create_app()) == set()


def test_writes_are_absent_when_the_flag_is_empty(monkeypatch):
    monkeypatch.setenv("DEADBAND_ENABLE_WRITES", "")
    assert _write_paths(create_app()) == set()


def test_writes_are_present_when_enabled(monkeypatch):
    monkeypatch.setenv("DEADBAND_ENABLE_WRITES", "1")
    assert "/api/fills" in _write_paths(create_app())


def test_explicit_argument_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("DEADBAND_ENABLE_WRITES", "1")
    assert _write_paths(create_app(enable_writes=False)) == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/api/test_write_gating.py -v`
Expected: FAIL — writes are registered unconditionally, so the default case finds `/api/fills`

- [ ] **Step 3: Gate the router**

```python
# api/app.py — in create_app's signature and body
import os


def create_app(enable_writes: bool | None = None) -> FastAPI:
    ...
    if enable_writes is None:
        enable_writes = bool(os.environ.get("DEADBAND_ENABLE_WRITES"))
    ...
    app.include_router(accounts_router)
    # Write routes exist ONLY when explicitly enabled. The published unit does
    # not set the flag, so these endpoints are absent there and return 404 to
    # every proxied request -- nothing is trusted, not a header and not a
    # source address (spec section 6). Registered before the SPA catch-all.
    if enable_writes:
        from api.fills import router as fills_router

        app.include_router(fills_router)
```

Note `app = create_app()` at module scope keeps reading the environment, which is what the systemd units select on.

- [ ] **Step 4: Enable writes in the API test fixture**

```python
# tests/api/conftest.py — in api_app
    app = create_app(enable_writes=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/ -v`
Expected: PASS, all API tests green

- [ ] **Step 6: Commit**

```bash
git add api/app.py tests/api/conftest.py tests/api/test_write_gating.py
git commit -m "feat(api): register write routes only when DEADBAND_ENABLE_WRITES is set

The published instance does not set it, so write endpoints do not exist there
and return 404 regardless of who asks. This is the only control between a
shared tailnet and unauthenticated writes, so it is pinned by tests. A
source-address check is deliberately NOT used: the deployment proxies every
path, so the proxy is the client and such a check would pass for remote
callers."
```

---

### Task 6: The Entry screen — Fill tab

**Files:**
- Create: `web/src/screens/Entry.tsx`
- Modify: `web/src/api.ts`, `web/src/App.tsx`, `web/src/styles.css`

**Interfaces:**
- Consumes: `POST /api/fills`, `DELETE /api/fills/{id}`, `fetchAccounts` from the Accounts work
- Produces: route `/entry`; `api.ts:createFills(body)`, `api.ts:deleteFill(id)`

- [ ] **Step 1: Add the API client**

```typescript
// web/src/api.ts — append. Money and quantities stay STRINGS end to end.
export interface FillLegIn {
  symbol: string
  side: 'buy' | 'sell'
  quantity: string
  price: string
  fee: string
  fee_currency: string
  executed_at: string
}

export interface CreatedFills {
  fill_ids: string[]
  trades_regrouped: number
}

async function send<T>(path: string, method: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method,
    headers: body === undefined ? {} : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (r.status === 404) throw new NotFound()
  if (!r.ok) throw new Error((await r.text()) || `${path}: ${r.status}`)
  return r.status === 204 ? (undefined as T) : ((await r.json()) as T)
}

export const createFills = (body: { account_id: string; fills: FillLegIn[] }) =>
  send<CreatedFills>('/api/fills', 'POST', body)
export const deleteFill = (id: string) => send<void>(`/api/fills/${id}`, 'DELETE')
```

- [ ] **Step 2: Build the Fill tab**

```tsx
// web/src/screens/Entry.tsx
import { useEffect, useRef, useState } from 'react'
import {
  createFills, deleteFill, fetchAccounts,
  type AccountSummary, type FillLegIn,
} from '../api'

const EMPTY = { symbol: '', side: 'buy' as const, quantity: '', price: '', fee: '0' }

export default function Entry() {
  const [accounts, setAccounts] = useState<AccountSummary[]>([])
  const [account, setAccount] = useState('')
  const [executedAt, setExecutedAt] = useState('')
  const [leg, setLeg] = useState({ ...EMPTY })
  const [added, setAdded] = useState<{ id: string; label: string }[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const symbolRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    fetchAccounts()
      .then((r) => { setAccounts(r.accounts); setAccount((a) => a || r.accounts[0]?.id || '') })
      .catch(() => setAccounts([]))
  }, [])

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    if (busy) return              // a double-click must not create two fills
    setBusy(true)
    setError(null)
    const body: FillLegIn = {
      symbol: leg.symbol.trim(), side: leg.side,
      quantity: leg.quantity, price: leg.price, fee: leg.fee || '0',
      fee_currency: 'USD', executed_at: executedAt,
    }
    try {
      const r = await createFills({ account_id: account, fills: [body] })
      // account and date are RETAINED: entering N fills is N passes.
      setAdded((prev) => [
        { id: r.fill_ids[0], label: `${body.side} ${body.quantity} ${body.symbol} @ ${body.price}` },
        ...prev,
      ])
      setLeg({ ...EMPTY })
      symbolRef.current?.focus()
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    } finally {
      setBusy(false)
    }
  }

  async function remove(id: string) {
    try {
      await deleteFill(id)
      setAdded((prev) => prev.filter((f) => f.id !== id))
    } catch (err) {
      setError(String(err instanceof Error ? err.message : err))
    }
  }

  return (
    <>
      <p className="eyebrow">by hand</p>
      <h1>Entry</h1>
      {error && <div className="error">{error}</div>}

      <form className="entry" onSubmit={submit}>
        <select value={account} onChange={(e) => setAccount(e.target.value)} aria-label="Account">
          {accounts.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <input
          ref={symbolRef} value={leg.symbol} aria-label="Symbol" placeholder="symbol" size={8}
          onChange={(e) => setLeg({ ...leg, symbol: e.target.value })}
        />
        <select
          value={leg.side} aria-label="Side"
          onChange={(e) => setLeg({ ...leg, side: e.target.value as 'buy' | 'sell' })}
        >
          <option value="buy">buy</option>
          <option value="sell">sell</option>
        </select>
        {/* type="text" + inputMode, NEVER type="number": a number input
            round-trips through a float and silently destroys a
            small-magnitude quantity. Same reason format.ts exists. */}
        <input
          type="text" inputMode="decimal" value={leg.quantity} aria-label="Quantity"
          placeholder="qty" size={7}
          onChange={(e) => setLeg({ ...leg, quantity: e.target.value })}
        />
        <input
          type="text" inputMode="decimal" value={leg.price} aria-label="Price"
          placeholder="price" size={9}
          onChange={(e) => setLeg({ ...leg, price: e.target.value })}
        />
        <input
          type="text" inputMode="decimal" value={leg.fee} aria-label="Fee"
          placeholder="fee" size={6}
          onChange={(e) => setLeg({ ...leg, fee: e.target.value })}
        />
        <input
          type="datetime-local" value={executedAt} aria-label="Executed at"
          onChange={(e) => setExecutedAt(e.target.value)}
        />
        <button type="submit" disabled={busy}>{busy ? 'saving…' : 'add fill'}</button>
      </form>

      {added.length > 0 && (
        <section className="section">
          <p className="eyebrow">added this session</p>
          <table>
            <tbody>
              {added.map((f) => (
                <tr key={f.id}>
                  <td className="num">{f.label}</td>
                  <td className="right">
                    <button onClick={() => remove(f.id)}>delete</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </>
  )
}
```

Add to `web/src/styles.css`:

```css
.entry { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 16px; }
```

Note `executedAt` comes from a `datetime-local` input, which yields
`2026-06-01T15:30` with no zone. The server calls `datetime.fromisoformat`,
which accepts that and produces a naive datetime. Send
`` `${executedAt}:00Z` `` if the field has no seconds, so the instant is
unambiguous — the ledger stores `TIMESTAMPTZ` and a naive value would be
interpreted against the server's zone.

- [ ] **Step 3: Wire the route and nav**

```typescript
// web/src/App.tsx
import Entry from './screens/Entry'
// in the rail, after the Trades NavLink:
        <NavLink to="/entry">Entry</NavLink>
// in Routes:
          <Route path="/entry" element={<Entry />} />
```

- [ ] **Step 4: Typecheck and build**

Run: `cd web && pnpm run build`
Expected: `tsc -b` clean, `vite build` succeeds

- [ ] **Step 5: Verify against the running app**

```bash
DEADBAND_ENABLE_WRITES=1 uv run uvicorn api.app:app --host 127.0.0.1 --port 8477
curl -s -X POST http://127.0.0.1:8477/api/fills \
  -H 'content-type: application/json' \
  -d '{"account_id":"<a real account id>","fills":[{"symbol":"ZZZ","side":"buy","quantity":"1","price":"1","fee":"0","fee_currency":"USD","executed_at":"2026-06-01T15:30:00Z"}]}'
```

Expected: `201` with a `fill_ids` array. Then delete it again so the real ledger is left unchanged — this is Michael's live data, not a fixture.

- [ ] **Step 6: Commit**

```bash
git add web/src/api.ts web/src/screens/Entry.tsx web/src/App.tsx web/src/styles.css
git commit -m "feat(web): Entry screen -- keyboard-first manual fill entry

Enter submits and returns focus to the symbol field with account and date
retained, so N fills are N passes with no mouse. Numeric inputs are text with
inputMode=decimal: type=number would round-trip through a float and silently
destroy a small-magnitude quantity."
```

---

### Task 7: The Entry screen — Multi-leg tab

**Files:**
- Modify: `web/src/screens/Entry.tsx`, `web/src/styles.css`

**Interfaces:**
- Consumes: `api.ts:createFills` (already takes a list — the server side needs no change)

- [ ] **Step 1: Add the segmented control and leg editor**

Add a `mode` state of `'fill' | 'multileg'` rendering a fixed two-button segmented control (D11: no rearrangeable panes). In multi-leg mode:

- `account` and `executedAt` are hoisted into shared header fields.
- A `legs` array of `{symbol, side, quantity, price, fee}` with "add leg" and per-row remove.
- Submit sends every leg in **one** `createFills` call, so the server writes them in one transaction — a four-leg position lands together or not at all.
- A `422` naming `fills[2].symbol` highlights the third leg's symbol input specifically.

- [ ] **Step 2: Typecheck and build**

Run: `cd web && pnpm run build`
Expected: clean

- [ ] **Step 3: Verify atomicity end to end**

With the write-enabled server running, post two legs where the second has a blank symbol. Expect `422` and **zero** fills written for that account.

- [ ] **Step 4: Commit**

```bash
git add web/src/screens/Entry.tsx web/src/styles.css
git commit -m "feat(web): multi-leg entry -- N legs, one request, one transaction

Creates fills and lets the grouper decide; it never produces Direction.SPREAD
(grouping.py only assigns LONG or SHORT), so pnl.py's SPREAD
NotImplementedError stays dormant rather than becoming reachable."
```

---

### Task 8: Full-suite verification and the deployment note

**Files:**
- Modify: `docs/known-gaps.md`

- [ ] **Step 1: Run every suite, foreground, reading each summary line**

```bash
uv run pytest tests/ --ignore=tests/db --ignore=tests/api -q          # expect 437+ passed
set -a && . ./.env && set +a && uv run pytest tests/api -q            # expect all passed
set -a && . ./.env && set +a && uv run pytest tests/db -q             # ~10 min, expect all passed
cd web && pnpm run build
```

Read the summary line of each. A `skipped` count above zero on the DB suites means `TEST_PG_DSN` is missing and the run proved nothing.

- [ ] **Step 2: Record the gaps this plan creates**

Append to `docs/known-gaps.md` the three gaps from spec §9: the write instance has no health check; `DEADBAND_ENABLE_WRITES` is a footgun if ever set on the published unit; and manual fills carry no dedupe key, so a double submission creates a visible duplicate.

- [ ] **Step 3: Note the ops work for Michael**

The second systemd unit and the tunnel step belong in gitignored `docs/ops/` — **not** in any tracked file. Do not commit them. Flag to Michael that the write instance needs its unit installed before the Entry screen is reachable anywhere but a dev machine.

- [ ] **Step 4: Commit**

```bash
git add docs/known-gaps.md
git commit -m "docs: record the gaps the write path creates"
```
