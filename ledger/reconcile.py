"""Compare the computed ledger against a broker statement. Pure — no I/O, no clock."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from enum import StrEnum
from uuid import UUID

from ledger.types import Direction


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
    # REQUIRED, and deliberately given NO default. `quantity` above is an
    # unsigned MAGNITUDE for a short exactly as much as for a long --
    # ledger/pnl.py:105 adds every opening fill's quantity with `position +=
    # qty` regardless of side, which is why unrealized_pnl takes `direction`
    # as a separate argument and branches on it. So this field is the only
    # thing that tells a 10-lot short from a 10-lot long, and valuing a short
    # as an asset overstates equity by TWICE its market value while
    # net_cash's own (direction-aware) figure still agrees to the cent -- a
    # pure-looking equity discrepancy that sends the reader hunting a
    # phantom. A default would make that failure silent on every call site
    # that forgot the argument, which is the same trap docs/known-gaps.md
    # gap #15 records for `Instrument.contract_multiplier` defaulting to 1
    # ("silently 100x wrong for an option") and had to be guarded against
    # with a crash.
    #
    # Only LONG and SHORT ever reach here. A caller builds these from
    # `OpenPosition`s whose `unvaluable_reason is None`, and
    # ledger/positions.py appends a reason for BOTH of the cases that leave
    # `direction` unset or SPREAD (`:76-83` vs `:109-111`), so
    # "unvaluable_reason is None" implies "direction is exactly LONG or
    # SHORT". Anything else is a caller bug, not a case to model here.
    direction: Direction


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

    Verdict precedence: UNRELIABLE outranks DRIFT. With something unvalued, a
    numeric gap cannot be attributed -- it may be entirely the missing
    position, or may hide a real defect on top -- so reporting DRIFT would
    claim a precision the data does not support.
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
            # SIGNED by direction, never bare. A short position is a
            # liability: closing it costs its market value, so it SUBTRACTS
            # from equity. `p.quantity` is an unsigned magnitude for both
            # directions (see Position.direction's own comment), so without
            # this sign a short is valued as though it were an asset --
            # equity wrong by twice the position's market value, while the
            # cash line still agrees to the cent because net_cash
            # (ledger/cash.py) already credits a SELL. That combination
            # reads as a pure equity discrepancy and sends the reader
            # hunting a defect that is not there.
            signed = -1 if p.direction is Direction.SHORT else 1
            market_value += signed * p.quantity * price * p.multiplier

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
