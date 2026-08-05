# Deadband — Strategy Lab (Subsystem E)

**Date:** 2026-08-05
**Status:** Design approved, pending user review of this document
**Depends on:** A (ledger), B (setups & theses), C (metrics), D (bars); QuantConnect for backtesting
**Scope:** Subsystem E only.

---

## 1. Purpose

E asks whether the things you trade actually work, and measures the distance between the
system you designed and the system you actually run.

It does **not** reimplement backtesting. QuantConnect already researches, builds, and
backtests LEAN strategies against regime-tagged historical data. E defines, dispatches,
tracks, and displays; QuantConnect computes.

### `setup` versus `strategy`

| | Definition | Lives in |
|---|---|---|
| `setup` | A pattern you recognize. May stay discretionary forever. | B |
| `strategy` | A rule set with defined entries, exits, and sizing that **can be tested**. | E |

A strategy may formalize a setup. A setup needs no strategy.

**Strategies are versioned.** Rules change, and a result is only meaningful against the
exact ruleset that produced it. Every backtest, forward test, and signal ties to a
`strategy_version`, never to a `strategy`. Without this you end up comparing two different
rulesets that happen to share a name and concluding something false.

---

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| E1 | Four capabilities: validate, backtest, forward-test, execution gap | Each answers a different question; only "backtest" needs QuantConnect. |
| E2 | **No point estimate is ever shown without a confidence interval** | A 62% win rate over 13 trades is indistinguishable from a coin flip. Displaying "62%" is false in a technically true way, and it gets acted on. |
| E3 | Strategies are versioned; results bind to a version | Otherwise results from different rulesets get compared as though they were the same strategy. |
| E4 | QuantConnect over HTTP, behind a `BacktestEngine` interface | Separate databases and lifecycles. QuantConnect down degrades E and breaks nothing else. |
| E5 | v1 stores LEAN Python source directly; **no rules DSL** | Compiling a structured rule definition into a LEAN algorithm is a compiler — its own multi-month project, and possibly never worth it. |
| E6 | Execution gap is E's, not C's | C measures behavior generally; E measures deviation from a *specific defined ruleset*. |

---

## 3. Statistical honesty

The core of E, and where most trading journals fail.

Every statistic over a set of trades reports:

- the **point estimate**
- a **95% confidence interval**
- an explicit **sufficiency verdict**
- where insufficient, the **additional sample size** needed to distinguish the observed
  effect from zero

```
ORB — 13 trades since 2026-03-01
  win rate      62%    (95% CI: 35% – 85%)
  expectancy    0.31R  (95% CI: -0.42R – 1.04R)

  ⚠ INSUFFICIENT SAMPLE
    Consistent with having no edge at all.
    ~58 more trades needed to distinguish 0.31R from zero.
```

Implemented in `strategy/significance.py` — pure, tested against hand-computed statistics.

**This rule has teeth:** the UI must have no code path that renders a win rate,
expectancy, or profit factor without its interval. A number displayed bare will be
believed and sized on.

Caveats the module must also surface:

- Trades containing an `opening_balance` fill are excluded (A's rule) and the exclusion is
  reported.
- Overlapping positions violate independence; where a setup's trades overlap in time, the
  interval is widened and flagged rather than quietly assuming independence.
- Survivorship: a setup retired after a losing streak and re-added later is one setup with
  a gap, not two, and is reported as such.

---

## 4. The four capabilities

### Validate — does this setup have an edge in my own history?

Reads A, B, and C. Segments by setup, computes expectancy and win rate with intervals,
reports sufficiency. Needs no QuantConnect and no market data — it is C's data asked a
harder question.

### Backtest — does this ruleset work on historical data?

Deadband holds a `strategy_version` with LEAN Python source and backtest parameters,
posts it to QuantConnect, polls for completion, stores the result. Deadband never runs
LEAN itself.

### Forward-test — track signals without capital

A registered strategy version emits or records signals going forward. Each signal is
evaluated against D's bars once its horizon passes, producing a hypothetical outcome. Then
three things get compared: **what the strategy said**, **what the market did**, and **what
you actually did**.

### Execution gap — the system you designed versus the one you run

For trades linked to a strategy or a thesis with a plan, compare planned entry, stop,
target, and size against actual fills from A. Aggregate the deviations: exited early,
moved the stop, sized under plan, chased entry.

This closes the loop across the whole product, and it is usually where the money goes.

---

## 5. Data model

| Table | Holds |
|---|---|
| `strategy` | Name, description, `status` (draft / testing / live / retired), optional `setup_id`, instrument scope, timeframe. |
| `strategy_version` | `strategy_id`, version number, rules prose, **LEAN Python source**, sizing rule, planned entry/exit/stop definitions, `created_at`. Immutable once a result binds to it. |
| `backtest_run` | `strategy_version_id`, engine, parameters (period, universe, resolution), status, `submitted_at`, `completed_at`, `engine_job_ref`, results (jsonb), error. |
| `signal` | `strategy_version_id`, `generated_at`, instrument, direction, entry, stop, target, horizon, `status` (pending / triggered / expired), hypothetical outcome, linked `trade_id` if taken. |

Execution gap is **computed, not stored** — a view over A's fills and B's planned fields.
Storing it would create a second source of truth that drifts.

---

## 6. QuantConnect bridge

```
BacktestEngine
  .submit(strategy_version, params) -> job_ref
  .poll(job_ref) -> BacktestStatus
  .results(job_ref) -> BacktestResult
```

One implementation: `QuantConnectEngine`, posting to QuantConnect's FastAPI service.

- **Separate databases.** Deadband never reads QuantConnect's Postgres. A QuantConnect
  migration must not be able to break Deadband silently.
- **Failure isolation.** QuantConnect unreachable puts backtest runs in `error` and leaves
  everything else working.
- **Never blocking.** Submission is fire-and-poll. No request waits on a backtest.
- QuantConnect's orchestrator containers are load-bearing on the deployment host and are
  shared with its own pipeline. E must respect its rate of work rather than flooding it.

---

## 7. UI

1. **Strategy list** — status, version count, latest result
2. **Strategy detail** — versions, rules, LEAN source, backtest history, forward-test signals
3. **Setup validation** — the statistics view with intervals and sufficiency verdicts
4. **Execution gap** — planned versus actual, aggregated and per trade

Fixed layouts. No configurability.

---

## 8. Testing

- `significance.py` against hand-computed statistics, including a case where the point
  estimate looks good and the interval spans zero.
- A test that **no rendering path emits a bare point estimate** (E2), asserted at the
  serialization layer so the UI cannot bypass it.
- Overlapping-trade detection: a set of time-overlapping trades must widen the interval
  and set the flag.
- `BacktestEngine` against a fake engine — submit, poll, complete, and the failure path.
- Version immutability: binding a result to a `strategy_version` and then editing its rules
  must be rejected.
- Execution-gap computation against hand-built plan/actual pairs.

---

## 9. Deferred

- A rules DSL compiling to LEAN (E5) — deferred, possibly permanently
- Automatic signal generation from rules; v1 signals are recorded, not generated
- Walk-forward and regime-segmented analysis — QuantConnect already does this; surface its
  results rather than reimplementing
- Multi-strategy portfolio allocation
