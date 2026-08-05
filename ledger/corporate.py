"""Corporate action adjustments as a computed layer. Pure — no I/O, no clock.

Raw fills are never mutated (spec D10). These functions return adjusted copies,
so a wrong adjustment is fixable and ground truth stays intact.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from enum import StrEnum
from uuid import UUID, uuid5

from ledger.types import Fill, Side


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
        if self.action_type is ActionType.SPINOFF:
            if self.basis_allocation is None:
                raise ValueError("spinoff requires basis_allocation")
            if not (Decimal(0) <= self.basis_allocation <= Decimal(1)):
                raise ValueError(
                    f"basis_allocation must be between 0 and 1, got {self.basis_allocation}"
                )
        elif self.basis_allocation is not None:
            if not (Decimal(0) <= self.basis_allocation <= Decimal(1)):
                raise ValueError(
                    f"basis_allocation must be between 0 and 1, got {self.basis_allocation}"
                )


_ACTION_PRECEDENCE = {
    ActionType.SPLIT: 0,
    ActionType.REVERSE_SPLIT: 0,
    ActionType.SPINOFF: 1,
    ActionType.SYMBOL_CHANGE: 2,
    ActionType.MERGER: 3,
}


def _ordered_actions(actions: Sequence[CorporateAction]) -> list[CorporateAction]:
    """Within an ex_date, a remap must run before any action targeting the
    instrument it produces. Economic precedence breaks remaining ties."""
    by_date: dict[date, list[CorporateAction]] = defaultdict(list)
    for a in actions:
        by_date[a.ex_date].append(a)

    ordered: list[CorporateAction] = []
    for ex_date in sorted(by_date):
        group = sorted(
            by_date[ex_date],
            key=lambda a: (_ACTION_PRECEDENCE[a.action_type], str(a.instrument_id)),
        )
        produced: dict[UUID | None, list[int]] = defaultdict(list)
        for i, a in enumerate(group):
            if a.resulting_instrument_id is not None:
                produced[a.resulting_instrument_id].append(i)

        deps = {
            j: {i for i in produced.get(b.instrument_id, []) if i != j} for j, b in enumerate(group)
        }

        emitted: list[int] = []
        remaining = list(range(len(group)))
        while remaining:
            ready = [i for i in remaining if not (deps[i] - set(emitted))]
            if not ready:
                raise ValueError(f"circular corporate-action dependency on {ex_date.isoformat()}")
            nxt = ready[0]  # group is pre-sorted, so this applies the tie-break
            emitted.append(nxt)
            remaining.remove(nxt)
        ordered.extend(group[i] for i in emitted)
    return ordered


def adjust_fills(fills: Sequence[Fill], actions: Sequence[CorporateAction]) -> list[Fill]:
    """Return adjusted copies of `fills`, applying `actions` in ex-date order.

    Adjustments apply only to fills executed strictly BEFORE ex_date (UTC day
    boundary). This compares at UTC-day granularity, while ex_date is exchange-
    local, so sessions crossing midnight UTC can have adjacent fills treated
    differently — a known limitation pending position-aware calcs in a later task.

    For spinoffs, only BUY fills get basis reallocated. A fully-closed pre-
    ex_date position still has its BUY fills' basis reduced — correcting this
    needs position awareness (later task). SELL fills are untouched by spinoffs."""
    missing = [f for f in fills if f.id is None]
    if missing:
        raise ValueError(f"adjust_fills requires persisted fills; {len(missing)} have id=None")

    result = list(fills)

    with localcontext() as ctx:
        ctx.prec = 50

        for action in _ordered_actions(actions):
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
                        dataclasses.replace(
                            f,
                            quantity=f.quantity * action.ratio_numerator / action.ratio_denominator,
                            price=f.price * action.ratio_denominator / action.ratio_numerator,
                            venue_fill_id=None,
                            content_hash=None,
                        )
                    )

                elif action.action_type is ActionType.SYMBOL_CHANGE:
                    next_result.append(
                        dataclasses.replace(
                            f,
                            instrument_id=action.resulting_instrument_id,
                            venue_fill_id=None,
                            content_hash=None,
                        )
                    )

                elif action.action_type is ActionType.MERGER:
                    next_result.append(
                        dataclasses.replace(
                            f,
                            instrument_id=action.resulting_instrument_id,
                            quantity=f.quantity * action.ratio_numerator / action.ratio_denominator,
                            price=f.price * action.ratio_denominator / action.ratio_numerator,
                            venue_fill_id=None,
                            content_hash=None,
                        )
                    )

                elif action.action_type is ActionType.SPINOFF:
                    if f.side is not Side.BUY:
                        next_result.append(f)
                        continue

                    fraction = action.basis_allocation
                    spun_qty = f.quantity * action.ratio_numerator / action.ratio_denominator
                    total_basis = f.quantity * f.price
                    next_result.append(
                        dataclasses.replace(
                            f,
                            price=f.price * (Decimal(1) - fraction),
                            venue_fill_id=None,
                            content_hash=None,
                        )
                    )
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
