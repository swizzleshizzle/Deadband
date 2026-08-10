"""Compare the computed ledger against a broker statement. Pure — no I/O, no clock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from uuid import UUID


class ReconcileVerdict(StrEnum):
    OK = "ok"
    DRIFT = "drift"
    UNRELIABLE = "unreliable"


@dataclass(frozen=True, slots=True)
class UnvaluableRef:
    """A position the ledger holds but cannot value. `instrument_id` may be a
    grouping key rather than a real instrument id -- db/positions.py uses a
    trade's own id when its instrument is unreachable -- so never look one up."""

    instrument_id: UUID
    symbol: str
    reason: str


@dataclass(frozen=True, slots=True)
class Position:
    instrument_id: UUID
    quantity: Decimal
    cost_basis: Decimal  # per unit, excluding multiplier
    multiplier: Decimal


@dataclass(frozen=True, slots=True)
class Snapshot:
    account_id: UUID
    as_of: datetime
    cash_balance: Decimal
    total_equity: Decimal


@dataclass(frozen=True, slots=True)
class Drift:
    account_id: UUID
    as_of: datetime
    computed_equity: Decimal
    reported_equity: Decimal
    equity_difference: Decimal  # computed - reported
    computed_cash: Decimal
    reported_cash: Decimal
    cash_difference: Decimal  # computed - reported
    unmarked_instruments: tuple[UUID, ...]
    is_within_tolerance: bool
    unvaluable_positions: tuple[UnvaluableRef, ...]
    # THE field callers render. `is_within_tolerance` above answers only "do the
    # numbers agree" -- a component of this, never the answer. A caller reading
    # it alone would print a clean pass on an account with unvalued positions,
    # which is the misuse docs/known-gaps.md's gap #12 note already warns about
    # for `unvaluable_reason`. An enum cannot be half-read.
    verdict: ReconcileVerdict


def reconcile(
    snapshot: Snapshot,
    positions: Sequence[Position],
    marks: Mapping[UUID, Decimal],
    computed_cash: Decimal,
    unvaluable: Sequence[UnvaluableRef] = (),
    tolerance: Decimal = Decimal("0.01"),
) -> Drift:
    """Value positions at their marks, add cash, and compare to the statement.

    Both equity_difference and cash_difference follow the convention:
    positive means the ledger computed MORE than the statement reported.
    """
    with localcontext() as ctx:
        ctx.prec = 50

        market_value = Decimal(0)
        unmarked: list[UUID] = []

        for p in positions:
            price = marks.get(p.instrument_id)
            if price is None:
                # Falling back to cost basis is a knowingly stale valuation, not a zero.
                price = p.cost_basis
                unmarked.append(p.instrument_id)
            market_value += p.quantity * price * p.multiplier

        computed_equity = computed_cash + market_value
        equity_difference = computed_equity - snapshot.total_equity
        cash_difference = computed_cash - snapshot.cash_balance

        within = abs(equity_difference) <= tolerance and abs(cash_difference) <= tolerance
        if unvaluable:
            # UNRELIABLE outranks DRIFT: with something unvalued, a numeric gap
            # cannot be attributed. It may be entirely the missing position, or
            # may hide a real defect on top.
            verdict = ReconcileVerdict.UNRELIABLE
        elif within:
            verdict = ReconcileVerdict.OK
        else:
            verdict = ReconcileVerdict.DRIFT

        return Drift(
            account_id=snapshot.account_id,
            as_of=snapshot.as_of,
            computed_equity=computed_equity,
            reported_equity=snapshot.total_equity,
            equity_difference=equity_difference,
            computed_cash=computed_cash,
            reported_cash=snapshot.cash_balance,
            cash_difference=cash_difference,
            unmarked_instruments=tuple(unmarked),
            is_within_tolerance=within,
            unvaluable_positions=tuple(unvaluable),
            verdict=verdict,
        )
