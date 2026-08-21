# Read-only API surface: Dashboard, Trades, Trade detail

**Status:** design, approved 2026-08-19. Spec review pending.
**Scope:** UI milestone 1 — the read-only vertical slice. Step 3 of the approved
sequencing: it is *written* first because it is the long pole, but it is *built*
after the #15 fixture fix and branch B (ACAT transfer-out) both land.
**Prior art:** screens and layouts are fixed by the ledger design spec
(`2026-08-04-trade-position-ledger-design.md` §8); the stack (FastAPI + Postgres +
React/Vite) is decision D3 there. No API or UI code exists in the repo before this
spec — no endpoint, route, or request/response shape was described anywhere.

## 1. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | The API binds `127.0.0.1` only and contains **no auth code**. | Single-user app, real brokerage data, and this host sits on a tailnet shared with other people. Reached over SSH tunnel or a deliberate Tailscale port-forward. Zero auth code means zero auth code to get wrong. Chosen over a tailscale0 bind + bearer token, and over an auth-ready middleware seam. |
| D2 | Hybrid endpoint shape: resource endpoints where a screen is a resource view (`/api/trades`, `/api/trades/{id}`), one composite for the inherently cross-resource screen (`/api/dashboard`). | Chosen over one-endpoint-per-screen (screen-named endpoints get rebuilt in milestone 2) and over pure resource REST (the browser would stitch the dashboard from ~5 calls and aggregation logic would land in React). |
| D3 | Read-only is enforced by Postgres, not convention: the API pool sets `default_transaction_read_only = on`. | An accidentally-added write endpoint fails loudly instead of passing review quietly. |
| D4 | NUMERIC never becomes a JSON float. Every money/quantity/ratio field serializes as a **string**. | `db/schema.sql`'s first comment is "never FLOAT"; the boundary must not undo it. asyncpg yields `Decimal`; the serializer emits `str(Decimal)`. |
| D5 | The API never recomputes realized P&L. Trade P&L fields are served from the columns `regroup_account` persisted. | One source of truth; the CLI and the UI can never disagree about a realized number. Unrealized P&L and market values are computed at read time from marks, because they are time-dependent by nature. |
| D6 | Valuation is honest about staleness. Every computed valuation carries the mark timestamp it used, and instruments that cannot be valued are listed, not silently priced at zero or dropped. | Marks are manual (`deadband marks set`) and can be stale or absent. Follows `ledger/reconcile`'s existing `UnvaluableRef` concept. |

## 2. Architecture and layout

Two new top-level directories:

- **`api/`** — the FastAPI app. Imports `db/*` and `ledger/*` directly; no duplicate
  query layer, no ORM. Entry point `api/app.py` exposes `create_app()`;
  run with uvicorn on `127.0.0.1:8000`.
- **`web/`** — the React/Vite app (scaffolded in the build step; this spec fixes only
  the contract it consumes).

Serving model:

- **Dev:** Vite dev server proxies `/api/*` to `127.0.0.1:8000`.
- **Deployed use:** FastAPI additionally serves the built `web/dist/` as static files,
  so one process on one localhost port is the whole app. No nginx exposure, no CORS
  configuration in either mode (same origin in prod; Vite proxy makes dev same-origin
  too).

Connection handling: handlers acquire their connection through a FastAPI dependency
(`api/deps.py`), not a module-global pool. This is load-bearing for testing (§7):
tests override the dependency with the rollback-per-test connection so handlers see
seeded, uncommitted data. The real app's dependency draws from a pool created with
`default_transaction_read_only = on` (D3).

## 3. `GET /api/health`

Liveness plus schema currency:

```
{ "db": true,
  "migrations_current": true,
  "pending_migrations": [] }
```

`migrations_current` compares `schema_migrations` rows against the files in
`db/migrations/`; when false, `pending_migrations` names the missing ones. The UI
shows "backend schema behind" instead of rendering confusing data. `db: false`
(connection failure) returns 200 with the flag false — health itself is reachable;
what it reports is the problem.

## 4. `GET /api/dashboard`

One call returns everything the Dashboard renders. All `<num>` values are
numeric-strings (D4), all timestamps ISO-8601 UTC.

```
{ "generated_at": "<ts>",
  "equity": { "total": "<num>" | null, "basis": "marks" },
  "accounts": [
    { "id": "<uuid>", "name": "<str>", "venue": "<str>",
      "account_type": "cash|margin|funded|wallet", "base_currency": "<str>",
      "is_active": true,
      "cash": "<num>",
      "equity": "<num>" | null,
      "snapshot": { "as_of": "<ts>", "total_equity": "<num>",
                    "cash_balance": "<num>" } | null,
      "drift": { "verdict": "<ReconcileVerdict>", "amount": "<num>" | null } | null } ],
  "open_positions": [
    { "account_id": "<uuid>",
      "instrument": { "id": "<uuid>", "symbol": "<str>", "multiplier": "<num>" },
      "quantity": "<num>", "cost_basis": "<num>" | null,
      "mark": { "price": "<num>", "as_of": "<ts>" } | null,
      "market_value": "<num>" | null, "unrealized_pnl": "<num>" | null,
      "is_estimated": false } ],
  "recent_activity": [
    { "type": "fill" | "cash_movement", "at": "<ts>", "account_id": "<uuid>", ... } ],
  "unvaluable": [
    { "instrument": { ... }, "reason": "<str>" } ],
  "drift_warnings": [
    { "account_id": "<uuid>", "verdict": "<ReconcileVerdict>", "detail": "<str>" } ] }
```

Semantics:

- **Accounts** come from `db.accounts.list_accounts`; `cash` from
  `db.cash.account_cash`; `snapshot` from `db.snapshots.latest_snapshot`; `drift`
  from `ledger.reconcile.reconcile` against that snapshot (null when no snapshot
  exists). Verdict values are `ledger/reconcile.ReconcileVerdict`'s — the API does
  not invent its own taxonomy.
- **Open positions** come from `db.positions.open_positions`, which is already
  corporate-action-adjusted. The `instrument` object carries exactly what that
  read path exposes (id, symbol, multiplier) and no more. Per `UnvaluableRef`'s
  own docstring, the id may be a grouping key rather than a real instrument id
  (an orphaned trade's instrument is unreachable) — the UI must never use it for
  lookups, only as a row key. Positions in instruments with no usable mark appear
  with `mark`, `market_value` and `unrealized_pnl` null AND get a row in
  `unvaluable` — visible twice by design, once in place and once as a warning.
- **`equity.total`** is the sum over accounts of cash plus valued positions. If any
  held instrument is unvaluable, per-account `equity` and the aggregate are null,
  never a partial sum presented as a total. The tiles still show cash and the
  snapshot figure, which are always known.
- **Recent activity** is the newest 20 events across all accounts: `fill` and
  `cash_movement` rows merged by time, each carrying a `type` discriminator. The
  event list is deliberately typed and open-ended: branch B's transfer concept, once
  landed, appears here as its own `type` rather than forcing a payload redesign.
- **`drift_warnings`** repeats, flat, every account whose reconcile verdict is not
  the OK verdict — the screen's warning strip reads this without re-deriving it
  from tiles.

## 5. `GET /api/trades`

The filterable log. Query parameters mirror §8's filter list exactly:

| Param | Matches | Form |
|---|---|---|
| `account` | `trade.account_id` | UUID |
| `intent` | `trade.intent` | `trade\|investment\|unassigned` |
| `instrument` | `trade.primary_underlying` | case-insensitive exact symbol |
| `status` | `trade.status` | `open\|closed` |
| `from`, `to` | `trade.opened_at` | ISO dates, inclusive |
| `tag` | `trade.strategy_tag` | case-insensitive exact |
| `limit`, `offset` | paging | ints; `limit` default 50, max 500 |

Response:

```
{ "trades": [ { ...trade fields..., "instrument_symbol": "<str>" } ],
  "total": <int>, "limit": <int>, "offset": <int> }
```

- Ordered `opened_at DESC` (matching `list_trades`), offset paging. Keyset paging is
  rejected as YAGNI at single-user scale; `total` is a `count(*)` over the same
  predicate.
- Trade rows carry the persisted columns (status, direction, intent, quantities,
  averages, realized P&L, fees, `r_multiple`, `strategy_tag`, `is_estimated`,
  timestamps) plus a display symbol resolved from the trade's effective instrument
  when set, else its opening instrument.
- **Row expansion fetches `GET /api/trades/{id}` and uses its `fills`.** No separate
  fills endpoint and no fills embedded in list rows — the list payload stays flat
  and the expansion cost is paid only for rows actually expanded.
- Requires extending `db/trades.list_trades` with these filters, a count, and
  paging — additive, and the only backend change this milestone makes outside `api/`.

## 6. `GET /api/trades/{id}`

Everything Trade detail renders:

```
{ "trade": { ...all persisted trade fields... },
  "instrument": { ...opening instrument... },
  "effective_instrument": { ... } | null,
  "fills": [
    { "source": "fill" | "derived_fill", "id": "<uuid>", "executed_at": "<ts>",
      "side": "buy|sell", "quantity": "<num>", "price": "<num>", "fee": "<num>",
      "allocated_quantity": "<num>", "is_estimated": false } ],
  "timeline": [
    { "type": "opened" | "fill" | "derived_fill" | "corporate_action" | "closed",
      "at": "<ts>", ...per-type fields... } ],
  "pnl": { "realized": "<num>" | null, "gross_realized": "<num>" | null,
           "fees_total": "<num>" | null, "fees_realized": "<num>" | null,
           "unrealized": "<num>" | null,
           "mark": { "price": "<num>", "as_of": "<ts>" } | null },
  "r_multiple": "<num>" | null,
  "notes": "<str>" | null }
```

- `fills` unions the trade's `trade_fill` allocations over `fill` and `derived_fill`,
  each labelled by `source` and carrying its allocation's `quantity` as
  `allocated_quantity` (a zero-crossing fill can belong to this trade only in part).
- `timeline` is the same fills as events, plus `corporate_action` events for actions
  on the trade's instrument (or effective instrument) with an ex-date inside
  `[opened_at, closed_at ?? now]`, plus synthetic `opened`/`closed` endpoints.
  Typed and open-ended like recent activity — branch B's transfer appears as its
  own type when it lands.
- `pnl` realized figures are the persisted columns (D5); `unrealized` is computed
  from the latest mark for open trades, null when closed or unvaluable.
- Unknown or malformed id → 404 (§7's error body). A trade id is never guessable
  from this API's own responses being wrong — ids come from `/api/trades`.

## 7. Errors and testing

**Errors.** Three cases, no custom taxonomy:

- 404 with `{ "detail": "<str>" }` for unknown trade ids.
- 422 (FastAPI's validation default) for malformed filters/params.
- 503 with `{ "detail": "<str>" }` from data endpoints when the database is
  unreachable; `/api/health` itself stays 200 and reports `db: false`.

**Testing.** `tests/api/`, driven by httpx's `AsyncClient` over ASGI against
`create_app()` — no live server. The connection dependency is overridden with the
existing rollback-per-test fixture connection, so tests seed through the same
transaction the handler reads and nothing persists. This inherits the `pool`
fixture's migration behavior, which is exactly why the #15 fix lands first.

Contract-level assertions:

- shapes and discriminators for all four endpoints;
- every money field is a JSON string, asserted structurally (walk the payload; a
  bare JSON number in a money position fails the suite);
- filter behavior per parameter, including combinations, empty results, paging
  totals;
- null-propagation rules of §4 (an unvaluable holding nulls its account's equity
  and the aggregate);
- 404/422 paths.

DB-independent logic (payload assembly, activity merge, timeline construction) is
factored into pure functions and tested without a connection, consistent with the
repo's existing pure-core style (`tests/test_purity.py` guards it).

## 8. Out of scope, named

- All writes; Entry & Import (milestone 2). The `group_fills` quantity-aware
  exclusion gated milestone 2, not this slice — a read-only UI cannot create the
  partial manual trade the hazard needs. **Since closed** (`cefd27d`, 2026-08-06);
  `docs/known-gaps.md` carries it under "Closed: `group_fills` quantity-aware
  exclusion", and it gates nothing now.
- The Accounts screen (§8 screen 2) and any screen beyond the three named.
- Auth of any kind (D1); exposure beyond `127.0.0.1`.
- Live updates (websockets/SSE/polling); the UI refetches on navigation.
- Metrics/analytics endpoints; the other 2026-08-05 specs' screens.
- Any new derived-ratio computation — gap #53's `_long_holdings_as_of` caveat means
  the API serves stored and already-adjusted data only.

## 9. Gaps this design creates

| Gap | Why accepted |
|---|---|
| `recent_activity` is fixed at 20 events, unconfigurable. | A parameter would be speculative; the screen has one strip. Revisit if a screen needs more. |
| The dashboard is one query fan-out per request with no caching. | Single user on localhost; correctness over cleverness. Measure before caching. |
| `instrument` filter matches `primary_underlying` only — an option trade found by its underlying, not its OCC symbol. | Matches how the trades screen thinks. A symbol-level search is milestone-2 territory. |
| `total` runs a second count query. | Trivial at this scale; keyset + no-count rejected as YAGNI. |
| Timeline shows corporate actions by ex-date window overlap, which can include an action that did not materially affect the trade (e.g. position already flat mid-window). | Precise attribution needs per-fill adjustment provenance the read path does not expose; overlap is honest and errs toward showing. |

