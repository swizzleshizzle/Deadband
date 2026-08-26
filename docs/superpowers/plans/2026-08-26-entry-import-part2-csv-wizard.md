# Entry & Import part 2 — the CSV import wizard

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import a broker export from the browser — pick a file, read an honest preview of what it will and will not do, then commit — without the API growing a second, divergent copy of the import rules.

**Architecture:** `cli.py:_preview_or_commit` already is the one shared body behind both `import` and `sync`, but it takes an argparse namespace, prints, and returns an exit code. Task 1 extracts its *decisions* into `db/import_flow.py` as two functions returning dataclasses; `cli.py` keeps only rendering. The API then calls the same two functions, so CLI and HTTP cannot diverge by construction.

**Tech Stack:** Python 3.12, asyncpg, FastAPI, pytest (asyncio auto mode), React 19 + Vite + TypeScript.

**Spec:** `docs/superpowers/specs/2026-08-24-entry-import-design.md` — §3 (the two import endpoints) and §4 "Import" (the three-step screen).

**This is plan 2 of 3.** Plan 1 (write plumbing + manual entry) is merged. Plan 3 is the marks/snapshot forms and does not depend on this.

## Global Constraints

- **Preview must stay connection-free by default.** `tests/db/test_cli.py::test_preview_import_never_opens_a_database_connection` pins it, and `--check-duplicates` is the one explicit opt-in exception. Any extraction that makes preview always take a live connection breaks a deliberate guarantee.
- **CLI behaviour must not change.** The 137 tests in `tests/db/test_cli.py` are Task 1's gate and must pass **unchanged** — do not edit a CLI test to accommodate the refactor. A test that needs changing means the refactor changed behaviour, which is the one thing it must not do.
- **Money and quantities cross the wire as strings**, never through `float`. Handlers return `DeadbandJSONResponse`, never a bare dict.
- **Write endpoints declare `get_write_conn`**; read endpoints declare `get_conn`. `tests/api/test_write_pool.py` enforces this by walking routes — and note it reads the *effective* route context, because `original_route` is unprefixed and omits router-level dependencies.
- **`request.client.host` must never be used as an access control.** The deployment proxies every path, so the proxy is the client.
- **Write routes only exist when `DEADBAND_ENABLE_WRITES` is set.** Both new endpoints are POSTs; the preview one is read-only in *effect* but is still a POST (it takes an upload), so it lives behind the same gate. Say so in a comment — a reader will otherwise wonder why a read-only operation is gated.
- **DB tests run in the foreground**, summary line read: `set -a && . ./.env && set +a && uv run pytest <paths>`. Never pipe to `tail`. A non-zero skip count means the run proved nothing.
- **This repo is PUBLIC.** No host identities, IPs, or deployment topology in tracked files; a pre-commit hook enforces a deny-list.
- Test data must be invented, never real portfolio values.

## Scope decision: account routing

The spec's §4 says "any ref without a matching account gets a selector". This plan implements the **whole-file** selector (the `--account` equivalent, which the History dialect needs because it carries no account column) and **not** per-ref reassignment.

Per-ref reassignment would need either a write to `account.external_ref` — a new write surface on accounts — or a routing override that `route_batch` has no concept of, which the CLI could not reproduce. Both are the CLI/HTTP divergence this plan exists to avoid. Instead the preview *reports* unknown refs clearly and names the fix (`accounts add --external-ref …`), which is exactly what the CLI does today. Recorded as a gap in Task 6.

---

### Task 1: Extract the import decisions from cli.py

**Files:**
- Create: `db/import_flow.py`
- Modify: `cli.py` (`_preview_or_commit` becomes a renderer)
- Test: `tests/db/test_import_flow.py`

**Interfaces:**
- Consumes: `db/importing.py:route_batch`, `commit_batch`, `probe_duplicates`, `RoutingPlan`, `CommitResult`, `DuplicateReport`; `db/trades.py:regroup_account`; `importers/base.py:ImportBatch`
- Produces: `db/import_flow.py:preview(batch, *, conn=None, venue, account_id=None) -> PreviewReport` and `commit(conn, *, venue, batch, account_id, source) -> ImportCommitReport`, plus both dataclasses

- [ ] **Step 1: Write the failing tests**

**Signature decision, settled here so the tests are concrete:** `preview` is
**always `async`** and takes `conn: asyncpg.Connection | None = None`. The
connection-free guarantee is about never *opening* a connection, not about
being synchronous — an async function that simply never touches `conn` honours
it exactly, and one signature beats a sync/async pair that callers must choose
between.

```python
# tests/db/test_import_flow.py
"""The extracted import decision layer. These assert on RETURNED DATA, never on
printed output -- that separation is the whole point of the extraction, and it
is what lets the API reuse these decisions instead of restating them.

All values invented."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from db.accounts import create_account
from db.import_flow import ImportCommitReport, PreviewReport, UnroutableRowsError, commit, preview
from importers.base import CanonicalFill, ImportBatch
from ledger.types import AssetClass, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db


def _batch(*, ref: str | None = None, n: int = 1, refs_seen: tuple[str, ...] = ()) -> ImportBatch:
    """Modelled on tests/db/test_importing.py's batch_of, plus the external_ref
    that routing turns on. ZZI is invented, as is every number here."""
    return ImportBatch(
        fills=tuple(
            CanonicalFill(
                instrument=Instrument(
                    id=None, asset_class=AssetClass.EQUITY, symbol="ZZI", quote_currency="USD"
                ),
                executed_at=datetime(2026, 3, 2 + i, 14, 30, tzinfo=UTC),
                side=Side.BUY,
                quantity=Decimal("2"),
                price=Decimal("11"),
                fee=Decimal("0"),
                fee_currency="USD",
                external_ref=ref,
            )
            for i in range(n)
        ),
        refs_seen=refs_seen or ((ref,) if ref else ()),
    )


async def test_preview_opens_no_connection_when_duplicates_are_not_requested():
    """A deliberate guarantee, pinned for the CLI by
    test_preview_import_never_opens_a_database_connection. conn=None must be a
    supported call, not an accident that happens to work."""
    batch = ImportBatch(warnings=("w1",), unmapped_rows=("r1",), refs_seen=("A",))
    report = await preview(batch, venue="fidelity", conn=None)
    assert isinstance(report, PreviewReport)
    assert report.warnings == ("w1",)
    assert report.unmapped_row_count == 1
    assert report.duplicates is None


async def test_preview_reports_every_ref_seen_including_wholly_unmapped_accounts(conn):
    """refs_seen is a strict superset of the refs reachable from fills/cash. An
    account whose rows are ALL unmapped contributes nothing to either, and is
    exactly the account this report most needs to surface."""
    await create_account(
        conn, name="Known", venue="fidelity", account_type="cash", external_ref="ZREF1"
    )
    report = await preview(
        _batch(ref="ZREF1", refs_seen=("ZREF1", "ZGHOST")), venue="fidelity", conn=conn
    )
    assert "ZGHOST" in report.unknown_refs
    assert "ZREF1" not in report.unknown_refs


async def test_commit_writes_and_regroups_and_reports_both(conn):
    acc = await create_account(
        conn, name="Flow", venue="fidelity", account_type="cash", external_ref="ZREF2"
    )
    report = await commit(
        conn, venue="fidelity", batch=_batch(ref="ZREF2"), account_id=None, source="csv"
    )
    assert isinstance(report, ImportCommitReport)
    assert report.fills_inserted == 1
    assert report.trades_regrouped >= 1
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 1


async def test_commit_refuses_unrouted_rows_when_no_account_is_given(conn):
    """A venue with no per-row account ref (the History dialect) has nothing to
    route on. Committing it without an explicit account would silently drop
    every row, so it must refuse instead."""
    with pytest.raises(UnroutableRowsError):
        await commit(
            conn, venue="fidelity", batch=_batch(ref=None), account_id=None, source="csv"
        )


async def test_commit_routes_everything_to_the_given_account_when_one_is_supplied(conn):
    acc = await create_account(conn, name="Whole", venue="fidelity", account_type="cash")
    report = await commit(
        conn, venue="fidelity", batch=_batch(ref=None), account_id=acc, source="csv"
    )
    assert report.fills_inserted == 1
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 1
```

`UnroutableRowsError` is new and belongs in `db/import_flow.py` beside the two
dataclasses — `cli.py` currently expresses this refusal by printing and
returning exit code 2, which an HTTP caller cannot act on.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_import_flow.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'db.import_flow'`

- [ ] **Step 3: Extract, without changing behaviour**

Read `cli.py:_preview_or_commit` end to end before touching it. It is ~460 lines and its comments record several hard-won edge cases — `ignore_on_import` refs routing *successfully*, `unknown_refs` (money-scoped) being the only field allowed to drive refusal while `reported_unknown_refs` is report-only, and corporate-action proposals printing before other preview output. **Preserve every one of those decisions; move them, do not rewrite them.**

Create `db/import_flow.py` holding two dataclasses and two functions:

```python
@dataclass(frozen=True, slots=True)
class PreviewReport:
    fill_count: int
    cash_count: int
    transfer_count: int
    warnings: tuple[str, ...]
    unmapped_row_count: int
    refs_seen: tuple[str, ...]
    unknown_refs: tuple[str, ...]        # reported_unknown_refs: REPORT ONLY
    ignored_refs: tuple[str, ...]
    blocking: tuple[tuple[str | None, str], ...]
    corporate_proposals: tuple[str, ...]
    duplicates: DuplicateReport | None   # None unless probing was requested
    needs_account: bool                  # rows exist with no routable ref


@dataclass(frozen=True, slots=True)
class ImportCommitReport:
    fills_inserted: int
    fills_skipped: int
    cash_inserted: int
    transfers_inserted: int
    transfers_skipped: int
    trades_regrouped: int
    warnings: tuple[str, ...]
    ignored_refs: tuple[str, ...]
```

Then rewrite `cli.py:_preview_or_commit` to call them and do nothing but print and map to an exit code. Every `print` stays in `cli.py`; no `print` may appear in `db/import_flow.py`.

- [ ] **Step 4: Run the new tests**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_import_flow.py -v`
Expected: PASS

- [ ] **Step 5: Prove the CLI is unchanged — this is the real gate**

Run: `set -a && . ./.env && set +a && uv run pytest tests/db/test_cli.py`
Expected: **137 passed, 0 skipped.** Foreground; this takes about 4 minutes.

If any CLI test fails, the refactor changed behaviour. **Fix the refactor, never the test.** A CLI test edited to accommodate this task is a defect, not a passing gate.

- [ ] **Step 6: Commit**

```bash
git add db/import_flow.py cli.py tests/db/test_import_flow.py
git commit -m "refactor: extract import decisions from cli.py into db/import_flow

_preview_or_commit was the one shared body behind import and sync, but it
took an argparse namespace, printed, and returned an exit code -- so the
HTTP wizard could not reuse it without restating every routing and refusal
rule. Now it returns dataclasses and cli.py only renders them.

Behaviour is unchanged: the 137 CLI tests pass untouched."
```

---

### Task 2: `POST /api/imports/preview`

**Files:**
- Create: `api/imports.py`
- Modify: `api/app.py` (register inside the existing `if enable_writes:` block)
- Test: `tests/api/test_imports_preview.py`

**Interfaces:**
- Consumes: `db/import_flow.py:preview`, `importers/registry.py:get_importer`, `list_importers`, `api/deps.py:get_conn`
- Produces: `POST /api/imports/preview` — multipart upload (`file`, `venue`, optional `account_id`) → the `PreviewReport` as JSON

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_imports_preview.py
"""POST /api/imports/preview (spec section 3). All fixtures invented."""

from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db

_CSV = "Run Date,Action,Symbol,Quantity,Price ($),Amount ($)\n"


async def test_preview_returns_counts_and_never_writes(client, conn):
    before = await conn.fetchval("SELECT count(*) FROM fill")
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("export.csv", _sample_fidelity_csv(), "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 200
    body = r.json()
    assert_no_json_floats(body)
    assert body["fill_count"] >= 1
    assert await conn.fetchval("SELECT count(*) FROM fill") == before


async def test_preview_reports_unknown_refs_rather_than_failing(client, conn):
    """An export naming an account you have not registered is a normal, common
    situation -- the screen must be able to SAY so. Refusing the whole preview
    would hide every other thing the file contains."""
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("export.csv", _sample_fidelity_csv(ref="NOSUCH"), "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 200
    assert "NOSUCH" in r.json()["unknown_refs"]


async def test_preview_rejects_an_unknown_venue(client):
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("x.csv", _CSV, "text/csv")},
        data={"venue": "notabroker"},
    )
    assert r.status_code == 422


async def test_preview_rejects_a_file_that_is_not_utf8_text(client):
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("x.csv", b"\xff\xfe\x00binary", "application/octet-stream")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 422
```

Write `_sample_fidelity_csv()` yourself: read `tests/` for an existing Fidelity fixture and reuse it rather than inventing a new dialect sample. **Invent all values** — never copy real rows.

- [ ] **Step 2: Run to verify failure**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/test_imports_preview.py -v`
Expected: FAIL — 404/405, the route does not exist.

- [ ] **Step 3: Implement**

`api/imports.py` with a `POST /api/imports/preview` taking `UploadFile` plus form fields. It decodes as UTF-8 (422 on failure), resolves the importer (422 on an unknown venue), parses, and calls `db.import_flow.preview` with the request's connection so duplicates are always probed — the wizard has no reason to hide them, and this is the endpoint's read-only use of the read pool.

Register it in `api/app.py` **inside the existing `if enable_writes:` block**, with a comment: preview writes nothing, but it belongs to the same feature and is gated with it rather than being reachable on the published instance.

- [ ] **Step 4: Run to verify pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/test_imports_preview.py tests/api/test_write_pool.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add api/imports.py api/app.py tests/api/test_imports_preview.py
git commit -m "feat(api): POST /api/imports/preview

Parses an upload and returns the same PreviewReport the CLI renders, so the
wizard and the command line describe a file identically. Writes nothing."
```

---

### Task 3: `POST /api/imports/commit`

**Files:**
- Modify: `api/imports.py`
- Test: `tests/api/test_imports_commit.py`

**Interfaces:**
- Consumes: `db/import_flow.py:commit`, `api/deps.py:get_write_conn`
- Produces: `POST /api/imports/commit` → the `ImportCommitReport` as JSON

- [ ] **Step 1: Write the failing tests**

```python
# tests/api/test_imports_commit.py
"""POST /api/imports/commit (spec section 3). All fixtures invented."""

from db.accounts import create_account
from tests.conftest import requires_db

pytestmark = requires_db


async def test_commit_writes_fills_and_regroups(client, conn):
    await create_account(
        conn, name="Imp", venue="fidelity", account_type="cash", external_ref="F1"
    )
    r = await client.post(
        "/api/imports/commit",
        files={"file": ("export.csv", _sample_fidelity_csv(ref="F1"), "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["fills_inserted"] >= 1
    assert body["trades_regrouped"] >= 1


async def test_committing_the_same_file_twice_is_a_no_op(client, conn):
    """content_hash dedupe is what makes a repeat import safe, and it is the
    property the whole 'import the same 90-day export again' workflow rests
    on. Asserted here because the wizard makes repeating an import one click."""
    await create_account(
        conn, name="Imp2", venue="fidelity", account_type="cash", external_ref="F2"
    )
    args = dict(
        files={"file": ("e.csv", _sample_fidelity_csv(ref="F2"), "text/csv")},
        data={"venue": "fidelity"},
    )
    first = (await client.post("/api/imports/commit", **args)).json()
    second = (await client.post("/api/imports/commit", **args)).json()
    assert first["fills_inserted"] >= 1
    assert second["fills_inserted"] == 0
    assert second["fills_skipped"] == first["fills_inserted"]


async def test_commit_refuses_unroutable_rows_without_an_account(client, conn):
    """Rows with no account ref and no chosen account would be silently
    dropped. Refusing is the only honest option."""
    r = await client.post(
        "/api/imports/commit",
        files={"file": ("e.csv", _sample_history_csv(), "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 422
    assert "account" in r.text.lower()


async def test_commit_routes_to_an_explicitly_chosen_account(client, conn):
    acc = await create_account(conn, name="Whole", venue="fidelity", account_type="cash")
    r = await client.post(
        "/api/imports/commit",
        files={"file": ("e.csv", _sample_history_csv(), "text/csv")},
        data={"venue": "fidelity", "account_id": str(acc)},
    )
    assert r.status_code == 200
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) >= 1
```

- [ ] **Step 2: Run to verify failure**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api/test_imports_commit.py -v`
Expected: FAIL — route missing.

- [ ] **Step 3: Implement**

Same parse path as preview, then `db.import_flow.commit` under `get_write_conn`, with the write and every `regroup_account` inside **one** `async with conn.transaction():` — a partially-imported file is the outcome this must make impossible. Re-parse the upload rather than trusting a client-supplied batch, matching spec §3: no server-side session state, and `content_hash` makes a repeat idempotent.

Map the unroutable-rows refusal to `422` with a message naming what to do.

- [ ] **Step 4: Run to verify pass**

Run: `set -a && . ./.env && set +a && uv run pytest tests/api -q`
Expected: PASS, 0 skipped.

- [ ] **Step 5: Commit**

```bash
git add api/imports.py tests/api/test_imports_commit.py
git commit -m "feat(api): POST /api/imports/commit

Re-parses the upload and commits through the same db/import_flow the CLI
uses. One transaction covers every account's write and regroup, so a file
cannot land half-imported; content_hash makes a repeat a no-op."
```

---

### Task 4: The Import tab — pick and preview

**Files:**
- Modify: `web/src/api.ts`, `web/src/screens/Entry.tsx`, `web/src/styles.css`

**Interfaces:**
- Consumes: `POST /api/imports/preview`
- Produces: `api.ts:previewImport(file, venue, accountId?)`, an `Import` mode on the Entry screen's existing segmented control

- [ ] **Step 1: Add the client**

`previewImport` posts `FormData` — **do not set a `content-type` header by hand**; the browser must set the multipart boundary itself, and overriding it produces a request the server cannot parse. Add `PreviewReport` and `ImportCommitReport` TypeScript interfaces mirroring the dataclasses exactly.

- [ ] **Step 2: Build step 1 and step 2 of the screen**

Add `'import'` to the existing `mode` union and a third segmented-control button. Then:

- A file input and a venue select. Venues come from the API's importer list if one is exposed; otherwise hardcode the two the repo supports and add a gap noting the duplication.
- On preview, render `PreviewReport` honestly: counts for fills/cash/transfers, every warning, the unmapped-row count, the duplicate report, and **`blocking` grouped by account ref**, so the screen says *this* account's rows block while *that* account's are fine.
- `unknown_refs` render as a named problem with the fix spelled out (register the account with that external ref), **not** as a silent omission. Per the scope decision above there is no per-ref selector.
- When `needs_account` is true, show the whole-file account selector.

Reuse the existing error path — `send()` already extracts a readable message from `detail`. Keep every value a string; nothing here goes through `Number`.

- [ ] **Step 3: Build**

Run: `cd /root/projects/Deadband/web && pnpm run build`
Expected: `tsc -b && vite build` clean.

- [ ] **Step 4: Commit**

```bash
git add web/src/api.ts web/src/screens/Entry.tsx web/src/styles.css
git commit -m "feat(web): import wizard -- pick a file and read an honest preview

Renders PreviewReport as-is: warnings, unmapped rows, duplicates, and
blocking reasons grouped by account. An unregistered account ref is named
as a problem with its fix, never silently skipped."
```

---

### Task 5: The Import tab — commit and result

**Files:**
- Modify: `web/src/api.ts`, `web/src/screens/Entry.tsx`

- [ ] **Step 1: Add `commitImport` and the commit step**

The commit button is **disabled while any ref being imported still has a blocking reason**, and disabled while a request is in flight. After a successful commit, render the `ImportCommitReport`: rows written, rows skipped as duplicates, trades regrouped, and any ignored refs.

Show `fills_skipped` prominently rather than hiding it — on a re-import it will be the *whole* file, and a user who sees only "0 inserted" with no explanation will reasonably think the import failed.

- [ ] **Step 2: Build**

Run: `cd /root/projects/Deadband/web && pnpm run build`
Expected: clean.

- [ ] **Step 3: Commit**

```bash
git add web/src/api.ts web/src/screens/Entry.tsx
git commit -m "feat(web): import wizard -- commit step and result summary

Commit is blocked while any importing ref has a blocking reason. The result
reports skipped-as-duplicate counts prominently: on a re-import that is the
entire file, and silence there reads as failure."
```

---

### Task 6: Full verification and gaps

- [ ] **Step 1: Run every suite, foreground, reading each summary line**

```bash
uv run pytest tests/ --ignore=tests/db --ignore=tests/api -q
set -a && . ./.env && set +a && uv run pytest tests/api -q
set -a && . ./.env && set +a && uv run pytest tests/db -q          # ~10 min
cd web && pnpm run build
```

A non-zero `skipped` count on either DB suite means the environment did not load and the run proved nothing.

- [ ] **Step 2: Record the gaps**

Append to `docs/known-gaps.md`:
- **No per-ref account reassignment in the wizard.** An export naming an unregistered account is reported, not fixable in place; the fix is to register the account with that `external_ref` first. Doing it in the UI needs either a write to `account.external_ref` or a routing override `route_batch` has no concept of — the second would be a CLI/HTTP divergence.
- **The venue list may be duplicated** in the frontend if no endpoint exposes `list_importers()`. A venue added to the registry would then not appear in the wizard until someone remembers the second list.

- [ ] **Step 3: Commit**

```bash
git add docs/known-gaps.md
git commit -m "docs: record the gaps the import wizard creates"
```
