# Deadband — Metrics & Analytics (Subsystem C)

**Date:** 2026-08-05
**Status:** Design approved, pending user review of this document
**Depends on:** Subsystem A (ledger), Subsystem B (thesis lifecycle)
**Scope:** Subsystem C only.

---

## 1. Purpose

C answers two kinds of question with one engine.

**Daily, before a session:** where do I stand, how exposed am I, how close is the prop
account to a breach, and am I on tilt.

**Periodically, in review:** what is actually working — which setups, which instruments,
which of my reads are right, and which mistakes do I keep repeating.

The dashboard is the scoreboard; every tile drills into the review analysis behind it.

**C owns no data.** It reads A and B and computes. Nothing in C is a source of truth.

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| C1 | Scoreboard that opens into review | One engine, two depths. The extra cost is UI, not modelling. |
| C2 | Both time-weighted and money-weighted returns, labelled | They answer different questions and diverge sharply around deposit timing. Showing one unlabelled is the most common way traders mislead themselves. |
| C3 | Computed on demand; no precomputed rollups initially | Personal-scale data on an 8-core host. Caching buys unneeded speed and costs staleness bugs. |
| C4 | Materialized views only when measured over ~200 ms | A threshold, not a guess. |
| C5 | Missing data is labelled, never silently zeroed | A confident number computed from absent data is worse than an admittedly rough one. |
| C6 | `intent` separation is enforced, not offered | A five-year hold and a day trade in one denominator makes both numbers meaningless. |
| C7 | Per-account benchmarks | Comparing a crypto account to SPY is noise. |
| C8 | `analytics/` is pure, like `ledger/` | Financial math errors are invisible until they have driven a year of bad decisions. Testable against hand-computed fixtures is the only defence. |

---

## 3. Scope

### In scope

- Daily equity series per account, with per-point data-quality labelling
- Time-weighted return, money-weighted return (XIRR), Modified Dietz fallback
- Trade performance: win rate, average win/loss, expectancy, profit factor,
  R-multiple distribution, max drawdown, holding time
- Segmentation across account, venue, asset class, setup, thesis verdict, lesson tag,
  period, day and time of day
- Behavioral metrics: streaks, no-thesis trades, plan overrides, thesis/outcome 2×2,
  lesson-tag frequency
- Funded-account headroom
- Open risk, with explicit unknown-risk reporting
- Benchmark comparison interface

### Out of scope

- The ledger itself (A), thesis capture (B)
- Market and benchmark price data (D)
- Strategy testing (E)
- MAE/MFE — needs intra-trade price history, deferred to D

---

## 4. The honesty principle

C reports on incomplete data. There will be trades with no recorded stop, positions with
no mark, and periods with no statement. Every metric therefore reports its own coverage.

| Situation | What C does | What C must never do |
|---|---|---|
| Open trade with no stop recorded | "$1,240 at risk across 4 positions; **2 positions have no stop recorded**" | Sum only the known stops and present it as total risk |
| Position with no current mark | Value at cost basis, flagged **stale** with the age of the last mark | Present a cost-basis valuation as a current one |
| No valuation at a cashflow date | Fall back to Modified Dietz, label the return **approximate** | Emit a precise-looking TWR derived from absent valuations |
| Trade containing an `opening_balance` fill | Counts toward P&L; **excluded** from R-multiple and win rate | Treat a reconstructed entry price as a real one |

This is the principle A established with `unmarked_instruments` in reconciliation, applied
across every metric.

---

## 5. Metric families

### Returns

Both methodologies, always labelled:

- **Time-weighted (TWR)** — strips the effect of deposit timing. Judges decision quality
  and is what a benchmark comparison is valid against.
- **Money-weighted (MWR)** — XIRR over external cashflows plus ending value. What the
  money actually earned.
- **Modified Dietz** — the fallback where sub-period valuations are missing. Always
  labelled approximate.

All three need a **daily equity series per account**, derived from fills, cash movements,
and marks. Each point carries a quality label: `statement` (from an `account_snapshot`),
`marked` (positions have current marks), or `estimated` (cost-basis fallback).

### Trade performance

Win rate, average win and average loss, expectancy, profit factor, R-multiple
distribution, maximum drawdown on the closed-trade equity curve, holding time
distribution.

R-multiple metrics cover only trades with a recorded `planned_risk`; the coverage
percentage is reported alongside.

### Segmentation

Account · venue · asset class · setup · thesis verdict · lesson tag · period ·
day of week · time of day.

**`intent` is enforced rather than offered.** Trading metrics exclude
`intent = 'investment'` by default. Investment performance gets its own view, measured in
returns rather than win rates, which is the right instrument for it. `unassigned` trades
in mixed accounts are surfaced as needing triage rather than silently included.

### Behavioral

- Win and loss streaks
- Trades taken with no thesis attached
- **Plan overrides** — exits that ignored the recorded stop or target from B
- The **thesis/outcome 2×2**
- Lesson-tag frequency, and tag frequency conditioned on verdict

### The 2×2

| | Trade won | Trade lost |
|---|---|---|
| **Thesis correct** | Repeatable | **Execution problem** — the most actionable quadrant |
| **Thesis wrong** | Luck; dangerous because it feels like skill | Working as intended |

Only possible because B records verdict and P&L separately. Crossed with `setup`, it
answers: which setups do I read correctly but execute badly, and which am I simply wrong
about?

### Funded-account headroom

From A's `funded_account_rule` against current equity: distance to drawdown breach,
distance to daily loss limit, progress to profit target, both absolute and as a fraction.
For a prop account this matters more than P&L, because breaching ends the account.

---

## 6. Scoreboard

Four tiles, fixed layout, each drilling into its review view:

1. **P&L across periods** — today, week, month, YTD; realized and unrealized; per account
   and aggregated.
2. **Open risk now** — capital at risk across open positions, as a fraction of equity,
   with unknown-risk positions counted separately.
3. **Funded-account headroom** — distance to breach, as number and bar.
4. **Discipline** — current streak, no-thesis trade count, recent plan overrides.

---

## 7. Structure

```
analytics/                 PURE — data in, numbers out, no I/O
  equity.py                daily equity series with quality labels
  returns.py               TWR, XIRR, Modified Dietz
  expectancy.py            win rate, expectancy, profit factor, R distribution
  drawdown.py              max drawdown, underwater curve
  behavioral.py            streaks, overrides, 2×2, tag frequency
  funded.py                headroom against account rules
  segments.py              segmentation helpers
```

Queries live in `db/analytics.py`; the pure modules never touch Postgres.

Benchmarks: `account.benchmark_symbol` (nullable) plus a `BenchmarkSource` interface.
Ships with no implementation; D provides one. Absent a benchmark, C reports absolute
returns and says so.

---

## 8. Testing

- **Hand-computed fixtures** for every returns function. XIRR, TWR, and Dietz get worked
  examples with known answers, including the case where TWR and MWR diverge sharply.
- Expectancy and drawdown against hand-built trade sequences.
- Property tests: expectancy of a symmetric sequence is zero; drawdown is never positive;
  segment totals sum to the unsegmented total.
- A test that investment-intent trades are excluded from win rate (C6), and one that
  `opening_balance` trades are excluded from R-multiple statistics.
- Coverage-reporting tests: a position with no stop must produce an unknown-risk count,
  never a smaller total.

---

## 9. Deferred

- MAE/MFE — needs intra-trade price history (D)
- Benchmark comparison data — needs D
- Materialized views — only if the 200 ms threshold is crossed (C4)
- Tax-lot reporting — explicitly not C's job; see A's D6
