# Deadband

A single-user trading dashboard and source-of-truth ledger — every fill, position, trade,
and thesis across crypto, equities, options, and futures, from inception to conclusion.

> *Deadband* (control theory): the range of input over which a system produces no output
> response. Don't act on noise; act on moves that clear the threshold.

## Status

Design phase. Nothing is implemented yet.

- Subsystem A design: [`docs/superpowers/specs/2026-08-04-trade-position-ledger-design.md`](docs/superpowers/specs/2026-08-04-trade-position-ledger-design.md)

## Subsystems

| | | Status |
|---|---|---|
| **A** | Trade & position ledger | design approved |
| B | Thesis lifecycle | not started |
| C | Metrics & analytics | not started |
| D | Market data & screeners | not started |
| E | Strategy lab (consumes QuantConnect) | not started |

## Stack

FastAPI + Postgres + React/Vite. Self-hosted as a container on a private network, never
exposed publicly. See §10 of the subsystem A spec for the deployment contract.

## Related, separate

- **QuantConnect** — autonomous strategy research and backtesting. Deadband's subsystem E
  will call it over HTTP. Separate codebase, separate database.
- **Ubiqui-Trade** — a dead Electron predecessor. Reference code only.
