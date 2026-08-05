"""Corporate action adjustments as a computed layer. Pure — no I/O, no clock.

Raw fills are never mutated (spec D10). These functions return adjusted copies,
so a wrong adjustment is fixable and ground truth stays intact.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum
from uuid import UUID, uuid5

from ledger.types import Fill


class ActionType(StrEnum):
    SPLIT = "split"
    REVERSE_SPLIT = "reverse_split"
    MERGER = "merger"
    SPINOFF = "spinoff"
    SYMBOL_CHANGE = "symbol_change"


_SPINOFF_NAMESPACE = UUID("6f2a0f4e-9c1b-4c7a-9a5c-2f1d3b7e8a90")


def _spinoff_fill_id(parent_fill_id: UUID, action: CorporateAction) -> UUID:
    """Stable synthetic id, so repeated adjustment yields identical output."""
    return uuid5(
        _SPINOFF_NAMESPACE,
        f"{parent_fill_id}:{action.resulting_instrument_id}:{action.ex_date.isoformat()}",
    )


@dataclass(frozen=True, slots=True)
class CorporateAction:
    instrument_id: UUID
    action_type: ActionType
    ex_date: date
    ratio_numerator: Decimal
    ratio_denominator: Decimal
    resulting_instrument_id: UUID | None = None
    cash_component: Decimal | None = None
    basis_allocation: Decimal | None = None  # spinoff: fraction of basis moved

    def __post_init__(self) -> None:
        if self.ratio_numerator <= 0 or self.ratio_denominator <= 0:
            raise ValueError("corporate action ratio components must be positive")
        if self.action_type in {ActionType.MERGER, ActionType.SPINOFF, ActionType.SYMBOL_CHANGE}:
            if self.resulting_instrument_id is None:
                raise ValueError(f"{self.action_type} requires resulting_instrument_id")


def adjust_fills(fills: Sequence[Fill], actions: Sequence[CorporateAction]) -> list[Fill]:
    """Return adjusted copies of `fills`, applying `actions` in ex-date order."""
    result = list(fills)

    with localcontext() as ctx:
        ctx.prec = 50

        for action in sorted(actions, key=lambda a: (a.ex_date, a.action_type.value)):
            ratio = action.ratio_numerator / action.ratio_denominator
            next_result: list[Fill] = []

            for f in result:
                if (
                    f.instrument_id != action.instrument_id
                    or f.executed_at.date() >= action.ex_date
                ):
                    next_result.append(f)
                    continue

                if action.action_type in {ActionType.SPLIT, ActionType.REVERSE_SPLIT}:
                    next_result.append(
                        dataclasses.replace(f, quantity=f.quantity * ratio, price=f.price / ratio)
                    )

                elif action.action_type is ActionType.SYMBOL_CHANGE:
                    next_result.append(
                        dataclasses.replace(f, instrument_id=action.resulting_instrument_id)
                    )

                elif action.action_type is ActionType.MERGER:
                    next_result.append(
                        dataclasses.replace(
                            f,
                            instrument_id=action.resulting_instrument_id,
                            quantity=f.quantity * ratio,
                            price=f.price / ratio,
                        )
                    )

                elif action.action_type is ActionType.SPINOFF:
                    fraction = action.basis_allocation or Decimal(0)
                    spun_qty = f.quantity * ratio
                    total_basis = f.quantity * f.price
                    next_result.append(
                        dataclasses.replace(f, price=f.price * (Decimal(1) - fraction))
                    )
                    if spun_qty > 0:
                        next_result.append(
                            dataclasses.replace(
                                f,
                                id=_spinoff_fill_id(f.id, action),
                                instrument_id=action.resulting_instrument_id,
                                quantity=spun_qty,
                                price=(total_basis * fraction) / spun_qty,
                                fee=Decimal(0),
                                is_estimated=True,
                                venue_fill_id=None,
                                content_hash=None,
                            )
                        )

            result = next_result

    return result
