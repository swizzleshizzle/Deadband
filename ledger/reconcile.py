"""Compare the computed ledger against a broker statement. Pure — no I/O, no clock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID


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
    cash_difference: Decimal
    unmarked_instruments: tuple[UUID, ...]
    is_within_tolerance: bool


def reconcile(
    snapshot: Snapshot,
    positions: Sequence[Position],
    marks: Mapping[UUID, Decimal],
    computed_cash: Decimal,
    tolerance: Decimal = Decimal("0.01"),
) -> Drift:
    """Value positions at their marks, add cash, and compare to the statement."""
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
        cash_difference = snapshot.cash_balance - computed_cash

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
            is_within_tolerance=abs(equity_difference) <= tolerance
            and abs(cash_difference) <= tolerance,
        )
