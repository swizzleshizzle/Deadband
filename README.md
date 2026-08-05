# Deadband

A single-user trading dashboard and source-of-truth ledger — every fill, position, trade,
and thesis across crypto, equities, options, and futures, from inception to conclusion.

> *Deadband* (control theory): the range of input over which a system produces no output
> response. Don't act on noise; act on moves that clear the threshold.

## Status

Subsystem **A-1 (trade & position ledger core)** is implemented: pure domain logic for
fill grouping, P&L, and corporate actions; a Postgres schema; CSV importers for Fidelity
and Coinbase; idempotent import (parse → preview → commit); and a CLI to drive all of it.
Subsystems B–E are still design phase, spec only.

### Requirements

- Python 3.11+, [uv](https://docs.astral.sh/uv/)
- **PostgreSQL 15 or later.** `db/schema.sql`'s `trade_opening_fill_fk` constraint uses
  the column-scoped `ON DELETE SET NULL (opening_fill_id)` form on a composite foreign
  key — that syntax does not exist before PG15. A plain `ON DELETE SET NULL` on an older
  Postgres would null every column in the constraint, including `account_id`, which
  violates `account_id`'s own `NOT NULL`.

### Quickstart

```bash
uv sync --extra dev

cp .env.example .env   # fill in PG_DSN (and TEST_PG_DSN if you'll run the DB test suite)
set -a && . ./.env && set +a

uv run python cli.py migrate                                                    # bootstrap the schema
uv run python cli.py accounts add --name "Fidelity Brokerage" --venue fidelity \
    --account-type cash                                                         # prints the account UUID
uv run python cli.py accounts                                                   # list accounts

uv run python cli.py import fidelity path/to/activity.csv --account <uuid>              # preview only
uv run python cli.py import fidelity path/to/activity.csv --account <uuid> --commit     # write + regroup

uv run python cli.py trades --account <uuid>
```

`import` without `--commit` never opens a database connection — it only parses the file
and reports what it would do. `--commit` refuses to run if `--account`'s venue doesn't
match the importer's (e.g. committing a Coinbase export to a Fidelity account), and
wraps the fill insert and trade regroup in one transaction, so a crash between the two
can never leave fills without their trades.

Run the test suite with `uv run pytest` (`TEST_PG_DSN` unset skips the database-backed
tests; set it to run them too).

## Subsystems

| | | Spec | Plan |
|---|---|---|---|
| **A** | Trade & position ledger | [spec](docs/superpowers/specs/2026-08-04-trade-position-ledger-design.md) | [A-1 ledger core](docs/superpowers/plans/2026-08-04-ledger-core.md) |
| **B** | Thesis lifecycle | [spec](docs/superpowers/specs/2026-08-05-thesis-lifecycle-design.md) | — |
| **C** | Metrics & analytics | [spec](docs/superpowers/specs/2026-08-05-metrics-analytics-design.md) | — |
| **D** | Market data, screeners, pre-trade gate | [spec](docs/superpowers/specs/2026-08-05-market-data-screeners-design.md) | — |
| **E** | Strategy lab (dispatches to QuantConnect) | [spec](docs/superpowers/specs/2026-08-05-strategy-lab-design.md) | — |

Parked ideas live in [`docs/ideas.md`](docs/ideas.md); gaps carried out of A-1 are in [`docs/known-gaps.md`](docs/known-gaps.md).

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
