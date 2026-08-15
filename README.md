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

uv run python cli.py sync coinbase --account <uuid> --commit                            # pull fills via API

uv run python cli.py trades --account <uuid>
```

`import` without `--commit` never opens a database connection — it only parses the file
and reports what it would do. `--commit` refuses to run if `--account`'s venue doesn't
match the importer's (e.g. committing a Coinbase export to a Fidelity account), and
wraps the fill insert and trade regroup in one transaction, so a crash between the two
can never leave fills without their trades.

### Coinbase fills

Coinbase fills come from the Advanced Trade API, not CSV. `sync coinbase [--account
<uuid>] [--start …] [--end …] [--commit]` requires `COINBASE_API_KEY` and
`COINBASE_API_SECRET` in the environment — a CDP API key scoped to **`view` only**; it
never needs `trade` or `transfer`. If either variable is absent, or Coinbase rejects the
key, `sync` **raises**, naming the missing variable, rather than returning zero fills —
a sync that reports success while fetching nothing is worse than one that fails loudly.

A read-only key still discloses your complete position and balance history. It belongs
only in the deployment environment and must never be committed to this repository or
placed in `.env.example`.

The Coinbase CSV importer no longer produces fills — it reports each trade row and
points you at `sync coinbase` instead. CSV import remains the only path for Coinbase's
non-trade cash movements (deposits, withdrawals, transfers, rewards and staking income,
interest); the Advanced Trade API has no endpoint for any of those.

### Fidelity option expiry

An `EXPIRED` row in a Fidelity export closes the option position at price zero, dated to
**the option's own expiry date, not the broker's `Run Date`** — Fidelity books a Friday
expiry on the following Monday, so the two differ by three days, and the expiry is the
date the position actually ceased to exist. The expiry date comes from the option symbol
itself (it is part of what identifies the instrument), never from parsed row text, so it
cannot disagree with the instrument the fill is posted against. Side is derived from the
sign of the row's `Quantity` — negative closes a short with a buy, positive closes a long
with a sell — since an `EXPIRED` row describes the position being removed, not a trade
direction there is a verb for.

This does not change any drift `reconcile` reports today: `open_positions` takes no
`as_of` and applies no date filter at all — it filters on `status = 'open'` and,
optionally, the account — so `--as-of` picks which *statement* to compare against, never
which positions — a close dated either day leaves the same closed trade. Recording the
true event date is what keeps the ledger correct once position reconstruction becomes
as-of aware (gap #29 in [`docs/known-gaps.md`](docs/known-gaps.md)
tracks that `--as-of` does not filter the ledger side); only then would a `Run Date`-dated
close leave a phantom open position across a statement date falling inside the three-day
window.

`ASSIGNED` and `EXERCISED` rows are recognised and deliberately **refused** rather than
guessed at: no assignment or exercise appears anywhere in five years of real exports
across three accounts, and modelling the resulting stock leg from documentation instead
of from a row actually received is how an earlier version of this importer's test fixture
got its money columns wrong. Either verb blocks the entire commit — nothing is written,
for any account in the batch — and names itself in the refusal. The one carve-out: a
blocking row belonging to an account registered `ignore_on_import` refuses nothing, since
it was never going to be part of the import. See
[`docs/known-gaps.md`](docs/known-gaps.md) (gaps #30–32) for what this leaves open: an
expiry whose opening fill hasn't been imported yet, corporate actions (`MERGER`,
`REVERSE SPLIT`, and others), and backdated `as of` correction rows.

### Reconciliation

`snapshot add` records a broker statement's figures by hand; `reconcile` compares the
ledger against the most recent one on or before a date.

```bash
uv run python cli.py snapshot add --account <uuid> --as-of 2026-08-01 \
    --equity 52340.00 --cash 1250.75 --note "August statement"

uv run python cli.py reconcile --account <uuid>                          # as of now, tolerance 0.01
uv run python cli.py reconcile --account <uuid> --as-of 2026-08-01 --tolerance 0.05
```

`snapshot add` takes `--account --as-of --equity --cash`, plus an optional `--note`.
Re-adding the same `--account`/`--as-of` pair overwrites what was stored — correcting a
mistyped figure is the point, and the table keeps no history. `reconcile` takes
`--account`, plus optional `--as-of` (defaults to now) and `--tolerance` (defaults to
`0.01`).

Cash is derived from the account's cash movements *and* its fills — a buy spends cash as
a fill, not a movement, so a balance built from movements alone would omit every trade —
and an account whose cash movements, instruments, or nonzero fill fees span more than one
currency is refused rather than summed incorrectly. A position `positions` cannot value
(see `docs/known-gaps.md` gap #12's note) is excluded from computed equity and named in
the output rather than silently dropped or silently priced.

**`reconcile` exits 0 only when the verdict is `OK`.** `DRIFT` (numbers disagree beyond
tolerance) and `UNRELIABLE` (something could not be valued, so the comparison cannot be
trusted either way) both exit 1. Exit 2 is a refusal — nothing was compared — and the
complete list of them is:

- `--account` is not a well-formed UUID (checked before the command runs at all);
- `--as-of` is neither a valid date nor a valid timestamp;
- `--as-of` is a timestamp carrying no UTC offset (a bare date is fine — it means midnight
  UTC);
- `--tolerance` is not a valid number, or is `NaN`/`Infinity`;
- `--tolerance` is negative (it would make every comparison read as drift);
- no account with that id;
- no snapshot on or before the effective `--as-of`;
- the account is mixed-currency (see the paragraph above);
- the database could not be reached — any `OSError` escaping the run is reported as a
  one-line `error: …` rather than a traceback. Not a judgement about your data like the
  rest of the list, but it exits 2 and compares nothing, so it belongs here.

That list is meant to be exhaustive, because a script branching on the exit code gets no
other contract. One deliberate exclusion: **argparse usage errors also exit 2** — a missing
`--account`, an unknown subcommand — but those are Python CLI convention rather than this
command's contract, and they fail before `reconcile` starts. A script that only checks the
exit code cannot tell `DRIFT` from `UNRELIABLE` apart, nor one refusal from another; read
stderr and the printed verdict for that.

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
