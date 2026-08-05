# Deadband — Trade & Position Ledger (Subsystem A)

**Date:** 2026-08-04
**Status:** Design approved, pending user review of this document
**Scope:** Subsystem A only. B–E are named here for boundary purposes and specced separately.

---

## 1. Context

Deadband is a single-user web application for tracking trading and investing activity
across every venue Michael uses — Coinbase, on-chain wallets, Fidelity, a crypto prop
account (Breakout), and an as-yet-unchosen futures prop firm. Its purpose is to be the
**source of truth** for the full trading and investment history: every fill, every
position, every thesis from inception to conclusion, and the long-term P&L that falls
out of them.

The name is a control-theory term: the input range over which a system produces no
output response. It points at the behavior the tool is meant to reinforce — do not act
on noise, only on moves that clear a threshold.

### Relationship to existing projects

**QuantConnect** (a sibling project) is a separate entity and stays that way.
It researches, builds, and backtests LEAN strategies autonomously. Deadband tracks what
Michael actually trades. Where the two overlap — subsystem E, strategy testing —
Deadband calls QuantConnect over HTTP rather than reimplementing it. They share a host
and a Postgres server; they do not share a database or a codebase.

**Ubiqui-Trade** (a dead sibling project) is an Electron application with
substantial overlapping surface (trade journal, market scanner, watchlist, alerts,
position calculator). It is **reference code only** — nothing is inherited wholesale.

Its failure mode is a design input here. Ubiqui-Trade did not die on data modelling; it
died on the drag-and-drop, resizable, per-tab configurable widget grid. That machinery
existed to postpone layout decisions, and for a single-user tool it buys nothing.
See §8.

### Subsystem decomposition

The full product idea decomposes into five subsystems, each getting its own
spec → plan → implementation cycle:

| | Subsystem | Owns |
|---|---|---|
| **A** | **Trade & position ledger** | *This spec.* Accounts, instruments, fills, trades, cash movements, marks. The canonical record. |
| B | Thesis lifecycle | Why entered, what invalidates, expected vs. actual, lessons. Links trades → thesis → outcome. |
| C | Metrics & analytics | Expectancy, R-multiples, drawdown, per-strategy/instrument/setup breakdowns, equity curves. |
| D | Market data & screeners | Quotes, charts, screeners, calendar — the pre-session "blink view". |
| E | Strategy lab | Plan and test strategies against real data. Consumes QuantConnect. |

A is first because B, C, and E all read from it, and a wrong schema here is the expensive
mistake. D is genuinely independent and can be built in any order.

---

## 2. Decisions

Decisions made during design, with the reasoning, so they can be revisited on evidence
rather than re-litigated from scratch.

| # | Decision | Why |
|---|---|---|
| D1 | New project, not an evolution of Ubiqui-Trade | Web app wanted; the Electron widget-grid premise is abandoned. |
| D2 | Fills are the unit of record; trades are a derived grouping | Survives scaling in/out, partial exits, and multi-leg options honestly. Lets API sync later insert fills without reshaping anything. |
| D3 | FastAPI + Postgres + React/Vite | Mirrors QuantConnect, so idioms transfer. `postgres:16` already runs on the target host. Reuses a dashboard stack already shipped once. |
| D4 | Self-hosted on a private machine, not a public web host | A Postgres instance and container stack already exist there, and a private-network VPN already provides remote access — so a public host would buy only uptime while exposing financial data and broker credentials. Reversible: the app is a container plus a Postgres URL. |
| D5 | Accounts tracked separately, aggregated at the view layer | Each venue has its own conventions and rules; merging at write time destroys that. Aggregation is a read concern. |
| D6 | Performance journal, not a tax tool | Average-cost basis per trade, not FIFO tax lots. Tax output, if ever needed, is a separate consumer of the same fills. |
| D7 | A does not fetch prices | `mark` table plus a `MarkSource` interface with a manual implementation. D plugs in a live source later without touching A. |
| D8 | Full history is in scope; schema day one, importers incremental | The model carries opening balances, corporate actions, intent, and snapshots from the start so nothing needs reshaping. Importers ship venue by venue. |
| D9 | Full corporate actions (splits, mergers, spinoffs, symbol changes) | Multi-year equity history silently corrupts without them. |
| D10 | Corporate actions are an adjustment layer, never a mutation | Raw fills stay exactly as the venue reported. A wrong adjustment is fixable; a corrupted ground truth is not. |
| D11 | No configurable layouts, ever | The machinery that sank Ubiqui-Trade. One user; layouts get designed, not deferred. |
| D12 | Name is `Deadband` | Ergonomics over semantics. `Hysteresis` is the better metaphor (path dependence) but is longer and gets misspelled in paths, service names, and database names. |

---

## 3. Scope

### In scope

- Accounts (multiple per venue), instruments, fills, trades, cash movements, price marks
- Funded-account rules and their current state
- Manual entry, including a multi-leg options builder
- CSV import with preview and idempotent commit
- Corporate action adjustments
- Reconciliation against broker statement snapshots
- Position views per account and aggregated
- Realized P&L; unrealized P&L where a mark exists

### Out of scope

- Thesis lifecycle (B), analytics beyond basic P&L (C), quote/screener feeds (D),
  strategy testing (E)
- Order placement or any write path to a broker. Deadband is read-only with respect
  to the outside world, permanently.
- Tax lot accounting (see D6)
- Multi-user, auth, sharing. Single user. If this ever changes it is a different product.

### Two boundaries worth restating

**Unrealized P&L needs prices, and A does not fetch prices.** The `mark` table plus a
`MarkSource` interface with exactly one implementation (manual) keeps an entire
market-data dependency out of the foundation.

**Deadband is a performance journal.** Cost basis is average-cost per trade. It will not
produce a Schedule D.

---

## 4. Data model

Postgres. `schema.sql` plus numbered migrations, following QuantConnect's convention.
All timestamps `timestamptz`, stored UTC. All money and quantity columns `numeric`,
never float.

### `account`

One row per account, not per venue — three Fidelity accounts are three rows.

| Column | Notes |
|---|---|
| `id` | pk |
| `name` | display name |
| `venue` | `coinbase` / `onchain` / `fidelity` / `breakout` / `futures_prop` / `manual` |
| `external_ref` | the venue's own account number; lets importers route rows automatically |
| `account_type` | `cash` / `margin` / `funded` / `wallet` |
| `default_intent` | `trade` / `investment` / `mixed` — the default for trades in this account |
| `base_currency` | reporting currency, default USD |
| `is_active`, `opened_at`, `closed_at` | |
| `metadata` | jsonb, venue-specific odds and ends |

### `funded_account_rule`

Per-account constraint set and current state, so a funded account can display
"3.1% from breach" rather than a bare P&L number.

`account_id`, `max_drawdown`, `drawdown_type` (static/trailing), `daily_loss_limit`,
`profit_target`, `payout_split`, `consistency_rule`, `current_state` (jsonb),
`evaluated_at`.

### `instrument`

One row per tradeable thing.

| Column | Notes |
|---|---|
| `id` | pk |
| `asset_class` | `crypto_spot` / `crypto_perp` / `equity` / `option` / `future` |
| `symbol` | display symbol |
| `quote_currency` | |
| `underlying`, `strike`, `expiry`, `option_right` | options (`call`/`put`) |
| `root`, `contract_expiry`, `contract_multiplier` | futures |
| `chain`, `contract_address` | on-chain tokens |
| `active_from`, `active_to` | supports symbol changes (see §6) |

Natural-key unique constraint per asset class, so the same `SPY 2026-09-19 500C` can
never be inserted twice.

### `fill`

**The ground truth.** Everything else in the system is derived from this table.

| Column | Notes |
|---|---|
| `id` | pk |
| `account_id`, `instrument_id` | |
| `executed_at` | |
| `side` | `buy` / `sell` |
| `quantity`, `price` | numeric |
| `fee`, `fee_currency` | |
| `source` | `manual` / `csv` / `api` / `opening_balance` |
| `venue_order_id`, `venue_fill_id` | nullable; `(account_id, venue_fill_id)` unique where not null |
| `content_hash` | dedupe fallback when a venue export carries no fill id |
| — | *no `trade_id`.* Fill-to-trade association lives in `trade_fill` (below), because one fill can belong to two trades. |
| `is_estimated` | true for reconstructed or opening-balance rows |
| `created_at`, `updated_at` | |

Fills with `source = 'api'` are not editable through the UI — they are what the venue
reported. Manual and CSV fills are editable and deletable.

**Opening balances.** For positions predating any importable history, an
`source = 'opening_balance'` fill records quantity and cost basis as known, with
`is_estimated = true`. Any trade containing one is excluded from R-multiple and win-rate
statistics while still counting toward P&L and current position. The system is explicit
about what it does not know rather than averaging in a guess.

### `trade`

A derived grouping over fills. **Deliberately has no `instrument_id`** — a four-leg
spread is one trade across four instruments.

| Column | Notes |
|---|---|
| `id` | pk |
| `account_id` | |
| `primary_underlying` | text, for display and grouping (`SPY`, `BTC`) |
| `direction` | `long` / `short` / `spread` — derived from the sign of net position at open for single-instrument trades; multi-leg structures whose net direction is not meaningful record `spread` |
| `status` | `open` / `closed` |
| `intent` | `trade` / `investment` — defaults from `account.default_intent`, overridable per trade. Where the account is `mixed` there is no default: entry and import require an explicit choice, and imported rows land as `unassigned` for triage rather than being guessed. |
| `grouping_mode` | `auto` / `manual` |
| `opening_fill_id` | Stable identity for an auto trade, unique per account. Regroup **upserts** on this key rather than deleting and rebuilding, so user-authored fields survive re-imports. See "Regroup must never destroy judgment" below. |
| `opened_at`, `closed_at` | |
| `qty_opened`, `qty_closed`, `avg_entry`, `avg_exit` | derived |
| `realized_pnl`, `fees_total` | derived |
| `planned_risk` | nullable; dollar risk at entry |
| `r_multiple` | derived: `realized_pnl / planned_risk` when both present |
| `strategy_tag` | free text for now; B and C will formalize |
| `rolled_from_id` | nullable self-fk, for option rolls |
| `notes` | |

`intent` exists because a five-year SPY holding and a three-day options play both group
correctly under the position rule but must never share a metrics denominator. Every
metric filters on it. Mixed accounts are exactly why it lives on the trade rather than
only on the account.

### `trade_fill`

`trade_id`, `fill_id`, `quantity`. The association between fills and trades, as an
allocation rather than a foreign key on `fill`.

A single fill can belong to **two** trades. Holding 2 long and selling 3 closes the long
with 2 units and opens a short with 1 — one fill, two trades, split by quantity. A
`fill.trade_id` column cannot express that without either losing information or splitting
the fill row, and splitting the row would mean altering ground truth to satisfy a
derived concept.

Invariant: for every fill, the sum of its allocations equals its quantity. Fees are
pro-rated across allocations by quantity share.

### `cash_movement`

| Column | Notes |
|---|---|
| `id`, `account_id`, `occurred_at` | |
| `kind` | `deposit` / `withdrawal` / `fee` / `funding` / `interest` / `dividend` / `payout` / `rebate` |
| `amount`, `currency` | |
| `instrument_id` | nullable — attributes dividends to a holding, making yield trackable |
| `note` | |

**Prop payouts are cash movements, not P&L.** Conflating them poisons every performance
metric downstream.

### `mark`

`instrument_id`, `as_of`, `price`, `source`. A latest-mark view drives unrealized P&L.

### `corporate_action`

`instrument_id`, `action_type` (`split` / `reverse_split` / `merger` / `spinoff` /
`symbol_change`), `ex_date`, `ratio_numerator`, `ratio_denominator`,
`resulting_instrument_id` (nullable), `cash_component` (nullable), `note`.

### `account_snapshot`

`account_id`, `as_of`, `cash_balance`, `total_equity`, `source` (statement/manual),
`note`.

Entered from a real broker statement. The reconciler compares computed position and cash
against it and surfaces drift. Without this a source-of-truth ledger diverges from
reality silently and the discovery comes a year late; with it, "Fidelity Roth is $312 off
as of 2026-07-31" arrives while the cause is still recoverable.

---

## 5. Grouping fills into trades

The auto-grouper walks fills for a given `(account, instrument)` in execution order,
tracking signed running position:

- Position moves **flat → non-flat**: a trade opens.
- Position returns to **flat**: that trade closes.
- All fills in between belong to that trade.
- A fill that **flips through zero** (long 2, sell 3) splits into a close of the existing
  trade plus a new opposite trade.

This covers spot, equities, futures, and single-leg options — the large majority of
volume. It cannot handle multi-leg options, where several instruments are one idea.

**So grouping is overridable.** `trade.grouping_mode` is `auto` or `manual`. The auto
pass only ever touches `auto` trades; a manual grouping is permanent. The options entry
form creates a multi-leg trade explicitly in one action, writing N fills bound to one
manually-grouped trade. A roll is "close trade, open new trade, linked by
`rolled_from_id`".

### Regroup must never destroy judgment

Regrouping runs after every import, so it runs often. It must be **non-destructive**.

`trade` columns divide into two kinds:

- **Derived** — status, opened/closed timestamps, avg entry/exit, quantities, realized P&L,
  fees, allocations. Regroup owns these and overwrites them freely.
- **User-authored** — `intent` (where the account is `mixed`), `planned_risk`,
  `strategy_tag` / setup, `notes`, and, once subsystem B exists, the thesis link.
  Regroup must never write these.

The naive implementation — delete all `auto` trades and rebuild — silently destroys every
user-authored field on the next CSV import. The failure is quiet and the loss is
unrecoverable, which is the worst combination.

So an auto trade carries a stable identity: `opening_fill_id`, unique per account.
Regroup upserts on it, updating derived columns only. A backdated fill that changes which
fill opens a trade does change its identity — that is correct, because the trade genuinely
changed.

The grouper is a **pure function** — fills in, groupings out, no I/O. This is the piece
where a subtle error silently corrupts every metric that will ever be computed, so it is
also the most heavily tested piece in the system (§9).

Cost basis is average-cost within a trade (D6).

---

## 6. Corporate actions

Applied as a computed adjustment layer over raw fills. Raw fills are never mutated (D10).

- **Split / reverse split** — quantity and price of fills before `ex_date` are adjusted
  by the ratio in adjusted views.
- **Symbol change** — handled through `instrument.active_from` / `active_to` plus a
  `symbol_change` action linking old to new; both resolve to one continuous position.
- **Merger** — position in the old instrument closes into `resulting_instrument_id`
  and/or `cash_component`.
- **Spinoff** — creates a position in the resulting instrument with cost basis allocated
  by the recorded ratio.

Entered manually. There is no corporate-action data feed in scope for A.

---

## 7. Import pipeline

**Three phases, never two: parse → preview → commit.**

Each importer exposes a pure mapping function taking a venue file and returning canonical
fills, canonical cash movements, and warnings. Importers never touch the database.

Deduplication is on `(account_id, venue_fill_id)` where the export provides an id, and on
`content_hash` where it does not. This is load-bearing rather than defensive: overlapping
date ranges get re-imported constantly during a backfill, and a non-idempotent importer
turns one trade into three.

The preview phase shows what would be inserted, what is a duplicate and will be skipped,
and what could not be mapped, before anything is written.

**Day-one importers: Fidelity and Coinbase.** On-chain, Breakout, and the futures prop
firm follow as their formats are obtained. Nothing blocks on a venue whose export is
awkward (D8).

---

## 8. UI

Five screens, fixed layouts:

1. **Dashboard** — aggregate equity, per-account tiles, open positions, recent activity,
   reconciliation drift warnings.
2. **Accounts** — list and detail; funded-account rules and headroom, snapshot history,
   drift.
3. **Trades** — the filterable log. Account, intent, instrument, status, date, tag. Rows
   expand to their fills.
4. **Trade detail** — fills, timeline, P&L breakdown, R-multiple, notes. This is where B
   attaches thesis.
5. **Entry & Import** — keyboard-first manual entry, multi-leg options builder, CSV
   wizard with preview and dedupe.

**No drag-and-drop, no resizable panes, no configurable layouts, ever (D11).**

---

## 9. Testing

- **Pure unit tests** on `ledger/` — grouping, P&L, corporate actions, reconciliation.
  No database, no network, no clock.
- **Property-based tests on the grouper.** Invariants: the sum of per-trade realized P&L
  equals total realized P&L computed from fills; a closed trade's fills always net to
  flat; every fill belongs to exactly one trade; replaying the grouper is idempotent.
- **Importer fixture tests** — real anonymized exports per venue mapped to expected
  canonical fills, including a re-import that must produce zero new rows.
- **DB-gated integration tests** behind `TEST_PG_DSN`, matching QuantConnect's pattern.
- **Playwright** on entry and import only — the two flows where a bug means bad data.

---

## 10. Deployment

Deadband self-hosts. It is a container plus a Postgres connection string, which is what
makes D4 reversible.

Requirements, stated as properties rather than as a description of any particular
machine:

- **Runs as a container** alongside an existing Postgres instance, in its own database
  with its own role.
- **Never bound to a public interface.** Loopback plus a private VPN network only.
  Deadband has no authentication and is not designed to gain any (§3).
- **No secrets in this repository, ever.** Credentials live only on the deployment host.
  This repository is public; see the public-hygiene skill in `.claude/skills/`.
- **Read-only toward the outside world, permanently.** No order placement, no write path
  to any broker.

### Backup is part of this design, not an afterthought

A source-of-truth ledger with no tested backup is not a source of truth. Whatever backup
regime already covers the deployment host, verify it actually covers *this database* —
host-level backup schemes routinely run in one direction only, and a database can sit
outside the very scheme its operator believes protects it.

The requirement: a **nightly `pg_dump` to durable storage with retention, plus a restore
that has actually been performed**. A backup that has never been restored is not a
backup.

### Environment-specific detail

Concrete hostnames, paths, ports, compose project layout, and operational traps for this
particular deployment live in `docs/ops/deployment.md`, which is **deliberately excluded
from version control**. That file is the operational source of truth; this section is the
contract it must satisfy.

---

## 11. Deferred

- Broker and exchange API sync (D8: manual and CSV first, API once the schema has proven
  itself against real trades)
- Live price marks — arrives with D
- Formalized strategy taxonomy — `strategy_tag` is free text until B and C define it
- Corporate-action data feed — manual entry only in A
