"""Realized and unrealized P&L using average-cost basis. Pure — no I/O, no clock.

Average cost per trade, not FIFO tax lots. Deadband is a performance journal,
not a tax tool (spec D6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID

from ledger.grouping import FillAllocation
from ledger.types import Direction, Fill, Side

_QUANT = Decimal("1E-18")


def _q(value: Decimal) -> Decimal:
    """Bound the scale so returned values are storable and callers can
    reproduce arithmetic at ordinary precision."""
    try:
        return value.quantize(_QUANT)
    except InvalidOperation:  # magnitude too large to quantize; leave as-is
        return value


@dataclass(frozen=True, slots=True)
class TradePnL:
    qty_opened: Decimal
    qty_closed: Decimal
    avg_entry: Decimal  # Average price of all opening fills
    avg_exit: Decimal | None  # Average price of closing fills (or None if position never closed)
    gross_realized_pnl: Decimal
    fees_total: Decimal
    realized_pnl: Decimal  # net of fees
    open_quantity: Decimal
    open_cost_basis: Decimal  # per unit, running average after closes


def compute_pnl(
    allocations: Sequence[FillAllocation],
    fills_by_id: Mapping[UUID, Fill],
    multipliers: Mapping[UUID, Decimal],
    direction: Direction,
) -> TradePnL:
    """Walk allocations chronologically, maintaining a running average cost."""
    if direction is Direction.SPREAD:
        raise NotImplementedError("multi-leg SPREAD trades need their own P&L path")

    # Wrap in a precision context: 50 digits is sufficient for displayed/stored
    # values (unlike grouping's 200, which prevents rounding during computation).
    with localcontext() as ctx:
        ctx.prec = 50

        ordered = sorted(
            allocations,
            key=lambda a: (fills_by_id[a.fill_id].executed_at, str(a.fill_id)),
        )
        opening_side = Side.SELL if direction is Direction.SHORT else Side.BUY

        position = Decimal(0)  # units of open position
        basis_total = Decimal(0)  # cost of the open position, per-unit terms
        qty_opened = Decimal(0)
        qty_closed = Decimal(0)
        entry_notional = Decimal(0)
        exit_notional = Decimal(0)
        gross = Decimal(0)
        fees = Decimal(0)

        for alloc in ordered:
            f = fills_by_id[alloc.fill_id]
            qty = alloc.quantity
            try:
                mult = multipliers[f.instrument_id]
            except KeyError as e:
                raise KeyError(
                    f"no contract multiplier supplied for instrument {f.instrument_id}"
                ) from e

            # Pro-rate the fee by this allocation's share of the fill.
            fees += (f.fee * qty / f.quantity) if f.quantity else Decimal(0)

            if f.side is opening_side:
                basis_total += qty * f.price
                position += qty
                qty_opened += qty
                entry_notional += qty * f.price
            else:
                # Remove a proportional slice of the EXACT basis rather than reconstructing
                # from a rounded per-unit average. Identical in intent, but exact when the
                # position closes fully, which is the common case.
                if qty == position:
                    removed = basis_total  # exact: no division at all
                else:
                    removed = basis_total * (qty / position)
                proceeds = qty * f.price
                if direction is Direction.SHORT:
                    gross += (removed - proceeds) * mult
                else:
                    gross += (proceeds - removed) * mult
                basis_total -= removed
                position -= qty
                qty_closed += qty
                exit_notional += qty * f.price

        # Quantize individual fields so the identity realized = gross - fees holds for callers.
        avg_entry_val = _q((entry_notional / qty_opened) if qty_opened else Decimal(0))
        avg_exit_val = (
            _q((exit_notional / qty_closed) if qty_closed else Decimal(0)) if qty_closed else None
        )
        gross_val = _q(gross)
        fees_val = _q(fees)
        open_cost_basis_val = _q((basis_total / position) if position else Decimal(0))

        return TradePnL(
            qty_opened=qty_opened,
            qty_closed=qty_closed,
            avg_entry=avg_entry_val,
            avg_exit=avg_exit_val,
            gross_realized_pnl=gross_val,
            fees_total=fees_val,
            realized_pnl=gross_val - fees_val,
            open_quantity=position,
            open_cost_basis=open_cost_basis_val,
        )


def unrealized_pnl(
    open_quantity: Decimal,
    open_cost_basis: Decimal,
    mark_price: Decimal,
    multiplier: Decimal,
    direction: Direction,
) -> Decimal:
    if direction is Direction.SPREAD:
        raise NotImplementedError("multi-leg SPREAD trades need their own P&L path")

    if open_quantity == 0:
        return Decimal(0)
    per_unit = (
        (open_cost_basis - mark_price)
        if direction is Direction.SHORT
        else (mark_price - open_cost_basis)
    )
    return per_unit * open_quantity * multiplier


def r_multiple(realized_pnl: Decimal, planned_risk: Decimal | None) -> Decimal | None:
    """R-multiple, or None when risk was never recorded. Never guess it."""
    if planned_risk is None or planned_risk == 0:
        return None
    return realized_pnl / planned_risk
