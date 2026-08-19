"""Group fills into trades. Pure — no I/O, no clock.

A trade opens when position moves from flat to non-flat and closes when it returns
to flat. A fill that crosses zero is split by quantity across two trades, which is
why association is an allocation rather than a foreign key.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from uuid import UUID

from ledger.types import AssetTransfer, Direction, Fill, Side, TradeStatus


class TransferError(ValueError):
    """An outbound transfer that cannot be applied: nothing held, a short
    position, or more shares than the position holds at that moment. Always
    raised, never clamped -- a transfer the ledger cannot honour is a data
    problem to surface, not a quantity to adjust (spec section 4)."""


@dataclass(frozen=True, slots=True)
class FillAllocation:
    fill_id: UUID
    quantity: Decimal  # always positive; the portion of the fill in this trade


@dataclass(frozen=True, slots=True)
class TransferAllocation:
    transfer_id: UUID
    quantity: Decimal  # always positive; adjusted units (see adjust_transfers)
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class TradeGroup:
    account_id: UUID
    instrument_ids: tuple[UUID, ...]
    allocations: tuple[FillAllocation, ...]
    # Outbound transfers that reduced or closed this trade (branch B). Not
    # FillAllocations: no fill row exists and trade_fill cannot reference one.
    transfers: tuple[TransferAllocation, ...]
    direction: Direction
    status: TradeStatus
    opened_at: datetime
    closed_at: datetime | None


def _sort_key(f: Fill) -> tuple[datetime, str]:
    # Ties broken by id so grouping is deterministic for simultaneous fills.
    return (f.executed_at, str(f.id))


def group_fills(
    fills: list[Fill], transfers: Sequence[AssetTransfer] = ()
) -> list[TradeGroup]:
    """Group fills into trades by walking signed position per (account, instrument).

    Transfers (branch B) enter the same walk as reduce-only closing events: an
    outbound transfer reduces a LONG position at its timestamp, closing the
    trade if it reaches zero. It never opens, never flips, and never clamps --
    an impossible transfer raises TransferError. At a tied timestamp fills
    process first: a broker's same-day executions precede its ACAT snapshot.
    """
    missing = [f for f in fills if f.id is None]
    if missing:
        raise ValueError(f"group_fills requires persisted fills; {len(missing)} have id=None")
    missing_t = [t for t in transfers if t.id is None]
    if missing_t:
        raise ValueError(
            f"group_fills requires persisted transfers; {len(missing_t)} have id=None"
        )

    buckets: dict[tuple[UUID, UUID], list[Fill]] = defaultdict(list)
    for f in fills:
        buckets[(f.account_id, f.instrument_id)].append(f)
    xfer_buckets: dict[tuple[UUID, UUID], list[AssetTransfer]] = defaultdict(list)
    for t in transfers:
        xfer_buckets[(t.account_id, t.instrument_id)].append(t)

    groups: list[TradeGroup] = []

    all_keys = set(buckets) | set(xfer_buckets)
    for account_id, instrument_id in sorted(all_keys, key=lambda k: (str(k[0]), str(k[1]))):
        bucket = sorted(buckets[(account_id, instrument_id)], key=_sort_key)
        xfers = sorted(
            xfer_buckets[(account_id, instrument_id)],
            key=lambda t: (t.occurred_at, str(t.id)),
        )
        # Fills sort before transfers at a tied timestamp (rank 0 vs 1).
        events: list[tuple[datetime, int, str, object]] = [
            (f.executed_at, 0, str(f.id), f) for f in bucket
        ] + [(t.occurred_at, 1, str(t.id), t) for t in xfers]
        events.sort(key=lambda e: (e[0], e[1], e[2]))

        with localcontext() as ctx:
            ctx.prec = 200

            position = Decimal(0)
            allocations: list[FillAllocation] = []
            transfer_allocs: list[TransferAllocation] = []
            opened_at: datetime | None = None
            direction: Direction | None = None

            def flush(closed_at: datetime | None) -> None:
                nonlocal allocations, transfer_allocs, opened_at, direction
                if not allocations:
                    return
                groups.append(
                    TradeGroup(
                        account_id=account_id,  # noqa: B023
                        instrument_ids=(instrument_id,),  # noqa: B023
                        allocations=tuple(allocations),
                        transfers=tuple(transfer_allocs),
                        direction=direction,  # type: ignore[arg-type]
                        status=TradeStatus.CLOSED if closed_at else TradeStatus.OPEN,
                        opened_at=opened_at,  # type: ignore[arg-type]
                        closed_at=closed_at,
                    )
                )
                allocations = []
                transfer_allocs = []
                opened_at = None
                direction = None

            for _at, _rank, _tie, ev in events:
                if isinstance(ev, AssetTransfer):
                    t = ev
                    if position == 0:
                        raise TransferError(
                            f"transfer of {t.quantity} on instrument {instrument_id} "
                            "with no open position"
                        )
                    if position < 0:
                        raise TransferError(
                            f"transfer of {t.quantity} on instrument {instrument_id} "
                            "against a short position: delivering shares out "
                            "requires a long holding"
                        )
                    if t.quantity > position:
                        raise TransferError(
                            f"transfer of {t.quantity} on instrument {instrument_id} "
                            f"exceeds the {position} held at {t.occurred_at.isoformat()}"
                        )
                    transfer_allocs.append(
                        TransferAllocation(t.id, t.quantity, t.occurred_at)
                    )
                    if t.quantity == position:
                        position = Decimal(0)
                        flush(closed_at=t.occurred_at)
                    else:
                        position -= t.quantity
                    continue

                f = ev
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
                        # ctx.prec=200 prevents rounding; exact-zero assignment is belt-and-braces
                        # to ensure the exhausted side is exactly zero regardless.
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
