# Deadband

A single-user trading dashboard and source-of-truth ledger — every fill, position, trade,
and thesis across crypto, equities, options, and futures, from inception to conclusion.

> *Deadband* (control theory): the range of input over which a system produces no output
> response. Don't act on noise; act on moves that clear the threshold.

## Status

Design phase complete. Implementation not started.

## Subsystems

| | | Spec | Plan |
|---|---|---|---|
| **A** | Trade & position ledger | [spec](docs/superpowers/specs/2026-08-04-trade-position-ledger-design.md) | [A-1 ledger core](docs/superpowers/plans/2026-08-04-ledger-core.md) |
| **B** | Thesis lifecycle | [spec](docs/superpowers/specs/2026-08-05-thesis-lifecycle-design.md) | — |
| **C** | Metrics & analytics | [spec](docs/superpowers/specs/2026-08-05-metrics-analytics-design.md) | — |
| **D** | Market data, screeners, pre-trade gate | [spec](docs/superpowers/specs/2026-08-05-market-data-screeners-design.md) | — |
| **E** | Strategy lab (dispatches to QuantConnect) | [spec](docs/superpowers/specs/2026-08-05-strategy-lab-design.md) | — |

Parked ideas live in [`docs/ideas.md`](docs/ideas.md).

## Principles

These recur across every spec and are the ones worth knowing before reading any of them.

- **Fills are ground truth.** Trades, positions, and every metric are derived. Corporate
  actions adjust through a computed layer; raw fills are never mutated.
- **Missing data is labelled, never zeroed.** An unstopped position is reported as unknown
  risk, not as riskless. A cost-basis valuation is flagged stale. A return computed without
  sub-period valuations says it is approximate.
- **Verdict and P&L are separate.** A thesis can be right while the trade loses. Recording
  only P&L reinforces luck and punishes good reads.
- **No point estimate without a confidence interval.** A 62% win rate over 13 trades is a
  coin flip, and displaying it bare gets it acted on.
- **Dependencies are isolated to interfaces.** Prices, benchmarks, assertion evaluation, and
  backtesting each sit behind a protocol with a trivial implementation shipped first.
- **Fixed layouts, no configurability.** One user. Layouts get designed, not deferred behind
  a drag-and-drop grid — the machinery that killed the predecessor project.
- **Read-only toward the outside world, permanently.** No order placement, ever.

## Stack

FastAPI + Postgres + React/Vite. Self-hosted as a container on a private network, never
exposed publicly. See §10 of the subsystem A spec for the deployment contract.

## Related, separate

- **QuantConnect** — autonomous strategy research and backtesting. Deadband's subsystem E
  will call it over HTTP. Separate codebase, separate database.
- **Ubiqui-Trade** — a dead Electron predecessor. Reference code only.
