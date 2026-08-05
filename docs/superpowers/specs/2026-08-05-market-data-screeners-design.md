# Deadband — Market Data, Screeners & the Pre-Trade Gate (Subsystem D)

**Date:** 2026-08-05
**Status:** Design approved, pending user review of this document
**Depends on:** A (ledger) for marks; B (thesis) for the gate; C (metrics) for risk and headroom inputs
**Scope:** Subsystem D only.

---

## 1. Purpose

> *"I want the all-in-one dashboard with every checklist item I need before I open a
> position. The whole idea of this app is to streamline my trading while keeping
> disciplined."*

That sentence is the product thesis, and it makes D more than a data feed. D supplies the
market half of a **readiness gate**: everything that should be true before a position is
opened, in one place, fast enough to actually be used.

### Three consumers, and the third sets the requirements

| Consumer | Needs |
|---|---|
| The pre-session dashboard | Quotes, context, calendar, news, screeners |
| Subsystem A | `mark` prices for unrealized P&L |
| **Subsystem B** | **Stored daily bar history**, for path-dependent assertions like "never closed above 524" |

A quote snapshot cannot answer a path-dependent question. That is why D stores bars rather
than merely proxying live quotes, and it is the single most consequential requirement in
this spec.

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| D1 | Delayed quotes plus stored daily bars | Covers all three consumers. No streaming infrastructure, free tiers suffice. |
| D2 | TradingView deep links instead of embedded charting | Charting is solved. `tradingview.com/chart/?symbol=NASDAQ:AAPL` from any tile beats rebuilding it — the exact trap Ubiqui-Trade fell into. |
| D3 | Provider abstraction with a fallback chain | Free tiers have gaps and rate limits. Swapping providers must be a config change, never a rewrite. |
| D4 | Start free, design for paid | Defer spend until gaps actually bite. |
| D5 | **The gate fires at thesis creation, not trade logging** | CSV import means trades arrive after they closed; a gate on logging would almost never fire. Thesis creation is the moment the user is actually in the app before trading. |
| D6 | Overrides are recorded, with reason | Converts discipline from a feeling into a dataset C can report on. |
| D7 | Every mark carries its source | A third-party delayed price must never be mistaken for a broker-confirmed one. |
| D8 | Fills that never match a thesis are flagged retrospectively | "Traded without a plan" is a discipline metric obtainable *only* from the ledger, not from a gate the user must cooperate with. |

---

## 3. The gate is a ritual, not an enforcement mechanism

**Deadband cannot stop a trade.** A is permanently read-only toward brokers — no order
placement, ever. The gate cannot block an order in Fidelity or Coinbase and must never be
described as though it can.

Its leverage is entirely in two places:

1. **Speed.** A ritual only survives if it is fast. The gate must resolve in one screen,
   with no navigation, or it will be skipped exactly when it matters most.
2. **The override record.** Every override is stored with a timestamp and reason so C can
   eventually report *"you overrode the red-folder warning 9 times; 7 of those lost
   money."* That sentence changes behavior. A disabled button would not — it would just be
   worked around.

D8 is the counterweight to the gate's voluntariness: whatever the user does or does not
acknowledge, imported fills that match no thesis are flagged. That measurement does not
depend on cooperation.

---

## 4. Scope

### In scope

- Provider abstraction and fallback chain
- Quote polling with per-provider rate limits and caching
- Daily bar storage and backfill
- Economic calendar with impact level (red-folder days)
- Earnings calendar for held and watched instruments
- News for watched symbols
- Watchlists and saved screens
- Market context (indices, sectors, macro, crypto)
- TradingView deep links
- Mark publication into A
- The pre-trade gate and its override record

### Out of scope

- Real-time streaming quotes (D1)
- Embedded charting (D2)
- Order placement — permanently out of scope for all of Deadband
- Intraday bars — deferred; needed for MAE/MFE in C, revisit if that becomes wanted
- Fundamentals and financial statements

---

## 5. Provider abstraction

Protocols, each with a fallback chain:

```
QuoteSource      .quote(symbols) -> dict[symbol, Quote]
BarSource        .daily_bars(symbol, start, end) -> list[Bar]
CalendarSource   .economic_events(start, end) -> list[EconomicEvent]
                 .earnings(symbols, start, end) -> list[EarningsEvent]
NewsSource       .news(symbols, since) -> list[NewsItem]
ScreenerSource   .run(screen_definition) -> list[ScreenResult]
```

Candidate implementations, free tier first: **Finnhub** (quotes, calendar, earnings, news
— already proven working in Ubiqui-Trade's `calendarService.js` and `newsService.js`,
which is debugged reference code), **Yahoo** (quotes, bars), the **TradingView MCP**
(screening, technicals, news, sentiment — see `docs/ideas.md` for its caveats), and
**exchange public endpoints** for crypto.

Rate limits, caching TTLs, and provider priority are configuration, not code.

### Marks published into A

D writes into A's existing `mark` table with `source` set to the provider name. A already
distinguishes marked from unmarked positions and labels stale valuations; D must never
write a mark that misrepresents its freshness or provenance (D7).

**Marks are never a reconciliation input.** Statement reconciliation compares against
broker statements only. A third-party delayed quote does not get a vote on whether the
ledger is correct.

---

## 6. Data model

| Table | Holds |
|---|---|
| `instrument_bar` | Daily OHLCV per instrument, with `source`. The backbone for B's assertions, C's benchmark comparisons, and A's marks. Unique on `(instrument_id, bar_date)`. |
| `watchlist`, `watchlist_item` | Named lists of symbols, ordered. |
| `screen` | Saved scan definitions: name, provider, criteria (jsonb), schedule. |
| `economic_event` | `occurs_at`, name, country, **`impact`** (`high`/`medium`/`low` — high is the red folder), actual, forecast, previous, source. |
| `earnings_event` | Instrument, date, timing (`before_open`/`after_close`), estimate, actual. |
| `news_item` | Symbol, headline, url, source, `published_at`. Deduped on url. |
| `checklist_rule` | User-configured gate rules: type, threshold, severity (`warn`/`fail`), active flag. |
| `checklist_run` | One row per gate evaluation: thesis id, run at, overall result, snapshot of every check's outcome (jsonb). |
| `checklist_override` | Run id, rule id, reason, timestamp. **C's discipline dataset.** |

### Retrieved content is untrusted

`news_item` bodies and any sentiment payload are attacker-writable text from public feeds.
If that content ever reaches an LLM context with tools attached, it is a prompt-injection
vector. Treat retrieved news and sentiment as data, never as instructions. This applies to
the TradingView MCP's Reddit and RSS surfaces in particular.

---

## 7. The gate

Fires when a thesis is created or activated. Each rule resolves to **pass**, **warn**, or
**fail**, and every result is stored.

Rule types, all user-configurable with thresholds:

| Rule | Source | Example |
|---|---|---|
| Red-folder proximity | D `economic_event` | high-impact event within 30 minutes |
| Earnings proximity | D `earnings_event` | earnings before the position's horizon ends |
| Open risk ceiling | C | total open risk above 3% of equity |
| Prop headroom floor | C | drawdown headroom below 2% |
| Position size cap | A + C | planned risk above 1% of account equity |
| Thesis completeness | B | no invalidation condition recorded |
| Recent discipline | C | three consecutive losses, or a recent override streak |

The user acknowledges warnings or overrides failures with a reason. Both paths proceed —
the gate never blocks (§3) — and both are recorded.

**The gate is implemented last**, after A, B, and C, because it consumes all three. It is
the most visible feature and the last one buildable, and planning should not pretend
otherwise.

---

## 8. Dashboard

Fixed layout, no configurability (the Ubiqui-Trade rule). Tiles:

1. **Market context** — indices, sectors, VIX, DXY, rates, BTC dominance
2. **Watchlist** — quotes, change, range, volume; each row deep-links to TradingView
3. **Today's calendar** — economic events with countdowns, red-folder items prominent;
   earnings for held and watched symbols
4. **Screener results** — saved scans, run on load
5. **News** — headlines for held and watched symbols
6. **Readiness** — the gate's standing checks: open risk, prop headroom, discipline flags

Tiles 1–5 are D's own. Tile 6 is the cross-subsystem synthesis.

---

## 9. Testing

- Provider implementations tested against **recorded fixtures**, never live endpoints —
  live tests are flaky and rate-limited.
- Fallback-chain tests: primary fails, secondary answers, and the result is labelled with
  the provider that actually served it.
- Rate-limit and cache-TTL tests with an injected clock (the pure-code rule: time is a
  parameter).
- Bar storage idempotency — re-fetching an overlapping range inserts nothing new.
- Gate evaluation is **pure**: rules plus a context object in, results out. Every rule type
  gets a pass, warn, and fail case.
- A test that an override is always recorded, including when the user overrides every
  check at once.

---

## 10. Deferred

- Intraday bars, and the MAE/MFE metrics in C that depend on them
- Real-time streaming
- Alerting on price or event conditions
- Automatic assertion evaluation for B — D provides the bars; wiring B's evaluator to them
  is B's remaining work
- Whether the TradingView MCP is adopted at all: decide by listing the scans actually
  wanted, then checking them against what it exposes (`docs/ideas.md`)
