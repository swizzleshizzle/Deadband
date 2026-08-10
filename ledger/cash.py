# ledger/cash.py
"""Cash balance from movements and fills. Pure — no I/O, no clock."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from importers.base import OUTFLOW_KINDS
from ledger.types import Side


@dataclass(frozen=True, slots=True)
class CashMovementRow:
    kind: str
    # Always positive -- direction lives in `kind`. See OUTFLOW_KINDS'
    # docstring in importers/base.py: a negative amount is a bug, not an outflow.
    amount: Decimal


@dataclass(frozen=True, slots=True)
class CashFillRow:
    side: Side
    quantity: Decimal
    price: Decimal
    multiplier: Decimal
    fee: Decimal


def net_cash(
    movements: Sequence[CashMovementRow], fills: Sequence[CashFillRow]
) -> Decimal:
    """The account's cash balance implied by everything the ledger holds.

    Cash CANNOT come from cash_movement alone: a buy spends cash as a FILL, not
    as a movement, so a balance built only from movements omits every trade.

    A DRIP needs no special case. The dividend is a movement in, the
    reinvestment is a fill out, and the two cancel to the residual that really
    stayed in cash. Adding a reinvestment special case here would double-count.

    Sweep rows are already absent: importers/fidelity.py classifies a
    sweep-fund reinvestment as INTERNAL so it is never counted twice (A2-9).
    """
    with localcontext() as ctx:
        # Same pin as ledger/pnl.py and ledger/reconcile.py.
        ctx.prec = 50
        total = Decimal(0)
        for m in movements:
            total += -m.amount if m.kind in OUTFLOW_KINDS else m.amount
        for f in fills:
            # The multiplier is load-bearing: 2 contracts at 3.50 with x100 is
            # 700, not 7.
            notional = f.quantity * f.price * f.multiplier
            total += (notional - f.fee) if f.side is Side.SELL else -(notional + f.fee)
        return total
