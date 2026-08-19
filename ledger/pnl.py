"""Realized and unrealized P&L using average-cost basis. Pure — no I/O, no clock.

Average cost per trade, not FIFO tax lots. Deadband is a performance journal,
not a tax tool (spec D6).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from uuid import UUID

from ledger.grouping import FillAllocation, TransferAllocation
from ledger.types import Direction, Fill, Side

_QUANT = Decimal("1E-18")


def _q(value: Decimal) -> Decimal:
    """Bound the scale so returned values are storable and callers can
    reproduce arithmetic at ordinary precision.

    The quantized values guarantee the identity realized = gross - fees at
    the producing precision (50 digits). Callers at default precision (28)
    have ~10 integer digits of headroom; the identity holds for |realized| < 1e10,
    which covers all realistic P&L. At extreme magnitudes (1e13+), identity
    may diverge at caller precision, but such cases are degenerate anyway."""
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
    fees_realized: Decimal
    realized_pnl: Decimal  # net of fees
    open_quantity: Decimal
    open_cost_basis: Decimal  # per unit, running average after closes (excluding multiplier)
    # Quantity that left via outbound transfer (branch B): closed the position
    # at average cost, realising nothing. Not part of qty_closed -- a transfer
    # is not an exit decision and never enters avg_exit or fee recognition.
    qty_transferred: Decimal = Decimal(0)


def compute_pnl(
    allocations: Sequence[FillAllocation],
    fills_by_id: Mapping[UUID, Fill],
    multipliers: Mapping[UUID, Decimal],
    direction: Direction,
    transfers: Sequence[TransferAllocation] = (),
) -> TradePnL:
    """Walk allocations chronologically, maintaining a running average cost."""
    if direction is Direction.SPREAD:
        raise NotImplementedError("multi-leg SPREAD trades need their own P&L path")

    # Wrap in a precision context: 50 digits is sufficient for displayed/stored
    # values (unlike grouping's 200, which prevents rounding during computation).
    with localcontext() as ctx:
        ctx.prec = 50

        # Fills sort before transfers at a tied timestamp, mirroring grouping's
        # rule: a broker's same-day executions precede its ACAT snapshot.
        events: list[tuple] = [
            (fills_by_id[a.fill_id].executed_at, 0, str(a.fill_id), a) for a in allocations
        ] + [(t.occurred_at, 1, str(t.transfer_id), t) for t in transfers]
        events.sort(key=lambda e: (e[0], e[1], e[2]))
        ordered = [e[3] for e in events]
        opening_side = Side.SELL if direction is Direction.SHORT else Side.BUY

        position = Decimal(0)  # units of open position
        basis_total = Decimal(0)  # cost of the open position, per-unit terms
        qty_opened = Decimal(0)
        qty_closed = Decimal(0)
        entry_notional = Decimal(0)
        exit_notional = Decimal(0)
        gross = Decimal(0)
        fees = Decimal(0)
        fees_entry = Decimal(0)
        fees_exit = Decimal(0)
        entry_mult = Decimal(0)

        qty_transferred = Decimal(0)

        for alloc in ordered:
            if isinstance(alloc, TransferAllocation):
                # Reduce-only, at running average cost: the removed slice takes
                # its exact share of basis with it and contributes nothing to
                # gross, exits, or fee recognition -- zero realised P&L is a
                # consequence of this shape, not an adjustment made elsewhere.
                # Grouping (ledger/grouping.py) has already validated the
                # transfer against the walk; a violation here means the caller
                # passed transfers grouping never saw.
                if position <= 0 or alloc.quantity > position:
                    raise ValueError(
                        f"transfer of {alloc.quantity} cannot apply to a position of {position}"
                    )
                if alloc.quantity == position:
                    basis_total = Decimal(0)  # exact: no division at all
                    position = Decimal(0)
                else:
                    basis_total -= basis_total * (alloc.quantity / position)
                    position -= alloc.quantity
                qty_transferred += alloc.quantity
                continue

            f = fills_by_id[alloc.fill_id]
            qty = alloc.quantity
            try:
                mult = multipliers[f.instrument_id]
            except KeyError as e:
                raise KeyError(
                    f"no contract multiplier supplied for instrument {f.instrument_id}"
                ) from e

            # Split by side: an entry fee is part of the basis of the units
            # acquired and is recognised as those units are sold; an exit fee
            # is recognised in full at the close. Pro-rating by the allocation's
            # share of the FILL (the old behaviour) expensed an entry fee
            # entirely on a fill wholly inside a barely-closed trade.
            fee_share = (f.fee * qty / f.quantity) if f.quantity else Decimal(0)
            fees += fee_share
            if f.side is opening_side:
                fees_entry += fee_share
            else:
                fees_exit += fee_share

            if f.side is opening_side:
                basis_total += qty * f.price
                position += qty
                qty_opened += qty
                entry_notional += qty * f.price
                entry_mult = mult  # opening leg's multiplier, for fee capitalization
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

        # Entry fees attributable to closed quantity, plus every exit fee.
        #
        # Deliberately computed once at end-of-trade from final totals
        # (fees_entry * qty_closed / qty_opened), not as a running average
        # applied at each individual close. This is prescribed by the spec: a
        # single end-of-trade ratio is order-insensitive -- allocating a
        # multi-lot entry's fee across several partial closes in any order
        # yields the same fees_realized -- which is what lets
        # test_allocations_sorted_chronologically pass. Do not "fix" this into
        # a running average; that would make the result order-dependent.
        entry_fee_recognised = (
            fees_entry * (qty_closed / qty_opened) if qty_opened else Decimal(0)
        )
        fees_realized_val = _q(fees_exit + entry_fee_recognised)

        # The remainder rides with the open units. open_cost_basis is per-unit and
        # excludes the multiplier, so convert the currency fee into price terms.
        # For a LONG, basis_total is purchase cost, so the fee adds to it. For a
        # SHORT, basis_total is sale proceeds, so the fee (which reduces net
        # proceeds) must be subtracted -- otherwise unrealized_pnl's
        # (open_cost_basis - mark_price) for SHORT doubles the sign error.
        entry_fee_per_unit = Decimal(0)
        if qty_opened and position and entry_mult:
            sign = Decimal(-1) if direction is Direction.SHORT else Decimal(1)
            entry_fee_per_unit = sign * (fees_entry / qty_opened) / entry_mult
        open_cost_basis_val = _q(
            ((basis_total / position) + entry_fee_per_unit) if position else Decimal(0)
        )

        return TradePnL(
            qty_opened=qty_opened,
            qty_closed=qty_closed,
            avg_entry=avg_entry_val,
            avg_exit=avg_exit_val,
            gross_realized_pnl=gross_val,
            fees_total=fees_val,
            fees_realized=fees_realized_val,
            realized_pnl=gross_val - fees_realized_val,
            open_quantity=position,
            open_cost_basis=open_cost_basis_val,
            qty_transferred=qty_transferred,
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
