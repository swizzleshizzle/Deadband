"""Group fills into trades. Pure — no I/O, no clock.

A trade opens when position moves from flat to non-flat and closes when it returns
to flat. A fill that crosses zero is split by quantity across two trades, which is
why association is an allocation rather than a foreign key.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID

from ledger.types import Direction, Fill, Side, TradeStatus


@dataclass(frozen=True, slots=True)
class FillAllocation:
    fill_id: UUID
    quantity: Decimal  # always positive; the portion of the fill in this trade


@dataclass(frozen=True, slots=True)
class TradeGroup:
    account_id: UUID
    instrument_ids: tuple[UUID, ...]
    allocations: tuple[FillAllocation, ...]
    direction: Direction
    status: TradeStatus
    opened_at: datetime
    closed_at: datetime | None


def _sort_key(f: Fill) -> tuple[datetime, str]:
    # Ties broken by id so grouping is deterministic for simultaneous fills.
    return (f.executed_at, str(f.id))


def group_fills(fills: list[Fill]) -> list[TradeGroup]:
    """Group fills into trades by walking signed position per (account, instrument)."""
    missing = [f for f in fills if f.id is None]
    if missing:
        raise ValueError(f"group_fills requires persisted fills; {len(missing)} have id=None")

    buckets: dict[tuple[UUID, UUID], list[Fill]] = defaultdict(list)
    for f in fills:
        buckets[(f.account_id, f.instrument_id)].append(f)

    groups: list[TradeGroup] = []

    for account_id, instrument_id in sorted(buckets, key=lambda k: (str(k[0]), str(k[1]))):
        bucket = sorted(buckets[(account_id, instrument_id)], key=_sort_key)

        with localcontext() as ctx:
            ctx.prec = 200

            position = Decimal(0)
            allocations: list[FillAllocation] = []
            opened_at: datetime | None = None
            direction: Direction | None = None

            def flush(closed_at: datetime | None) -> None:
                nonlocal allocations, opened_at, direction
                if not allocations:
                    return
                groups.append(
                    TradeGroup(
                        account_id=account_id,  # noqa: B023
                        instrument_ids=(instrument_id,),  # noqa: B023
                        allocations=tuple(allocations),
                        direction=direction,  # type: ignore[arg-type]
                        status=TradeStatus.CLOSED if closed_at else TradeStatus.OPEN,
                        opened_at=opened_at,  # type: ignore[arg-type]
                        closed_at=closed_at,
                    )
                )
                allocations = []
                opened_at = None
                direction = None

            for f in bucket:
                remaining = f.quantity  # positive magnitude left to allocate
                delta_sign = Decimal(1) if f.side is Side.BUY else Decimal(-1)

                while remaining > 0:
                    if position == 0:
                        # Opening a new trade with whatever is left of this fill.
                        opened_at = f.executed_at
                        direction = Direction.LONG if delta_sign > 0 else Direction.SHORT
                        allocations.append(FillAllocation(f.id, remaining))
                        position = delta_sign * remaining
                        remaining = Decimal(0)

                    elif (position > 0) == (delta_sign > 0):
                        # Same direction — scaling in.
                        allocations.append(FillAllocation(f.id, remaining))
                        position += delta_sign * remaining
                        remaining = Decimal(0)

                    else:
                        # Opposite direction — reducing, possibly through zero.
                        reducible = min(remaining, abs(position))
                        allocations.append(FillAllocation(f.id, reducible))
                        if reducible == abs(position):
                            position = Decimal(0)
                        else:
                            position += delta_sign * reducible
                        if reducible == remaining:
                            remaining = Decimal(0)
                        else:
                            remaining -= reducible
                        if position == 0:
                            flush(closed_at=f.executed_at)
                        # Any leftover re-enters the loop and opens an opposite trade.

            flush(closed_at=None)

    return groups
