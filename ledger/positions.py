"""Open trades → positions per instrument. Pure — no I/O, no clock."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from uuid import UUID

from ledger.types import Direction


@dataclass(frozen=True, slots=True)
class TradeRow:
    """One open trade, as the database hands it over."""

    account_id: UUID
    account_name: str
    instrument_id: UUID
    symbol: str
    multiplier: Decimal
    direction: Direction
    # NULL on a protected (orphaned) trade -- see db/trades.py's protect path
    # and tests/db/test_trades.py:652. NOT zero.
    open_quantity: Decimal | None
    open_cost_basis: Decimal | None
    is_estimated: bool


@dataclass(frozen=True, slots=True)
class OpenPosition:
    account_id: UUID
    account_name: str
    instrument_id: UUID
    symbol: str
    quantity: Decimal
    cost_basis: Decimal
    multiplier: Decimal
    # None when the contributing trades do not agree on one direction.
    # `unvaluable_reason` -- never this field -- is what decides whether the
    # position can be valued: a group can agree on a single direction (this
    # is set) while still being unvaluable for another reason (e.g. a NULL
    # quantity on one contributor), so a non-None direction here is NOT a
    # signal that pricing is safe. Check `unvaluable_reason` for that.
    direction: Direction | None
    is_estimated: bool
    # None means "this position can be valued against a mark". Any other
    # value is a human-readable reason it cannot be, and the caller must
    # show the row anyway -- a position omitted from a position listing is
    # the silent-loss shape this codebase keeps rediscovering.
    unvaluable_reason: str | None
    trade_count: int


def aggregate_positions(rows: Sequence[TradeRow]) -> tuple[OpenPosition, ...]:
    # Keyed by (account_id, instrument_id), not instrument_id alone: cost
    # basis is not fungible across accounts (a retirement account's basis
    # carries no tax consequence; a taxable account's drives the whole tax
    # position), and blending them across accounts also manufactures a
    # "mixed direction" position that exists nowhere in reality -- long in
    # one account and short in another is two ordinary, individually
    # valuable positions, not one unvaluable one. Mixed direction WITHIN one
    # account is still real and still falls through to the direction check
    # below exactly as before.
    grouped: dict[tuple[UUID, UUID], list[TradeRow]] = {}
    for r in rows:
        grouped.setdefault((r.account_id, r.instrument_id), []).append(r)

    out: list[OpenPosition] = []
    for (account_id, instrument_id), group in grouped.items():
        first = group[0]
        reasons: list[str] = []

        # NULL is unknown, never zero. Checked BEFORE any arithmetic so a
        # missing quantity can never be summed away as if it were nothing.
        if any(r.open_quantity is None or r.open_cost_basis is None for r in group):
            reasons.append("open quantity unknown on at least one trade")

        directions = {r.direction for r in group}
        if Direction.SPREAD in directions:
            reasons.append("spread")
        elif len(directions) > 1:
            reasons.append("mixed direction")

        priced = [
            r for r in group if r.open_quantity is not None and r.open_cost_basis is not None
        ]
        with localcontext() as ctx:
            # Same pin as ledger/pnl.py and ledger/reconcile.py: an ambient
            # low precision would silently round the weighting.
            ctx.prec = 50
            quantity = sum((r.open_quantity for r in priced), Decimal(0))
            if quantity != 0:
                weighted = sum(
                    (r.open_quantity * r.open_cost_basis for r in priced), Decimal(0)
                )
                cost_basis = weighted / quantity
            else:
                cost_basis = Decimal(0)

        out.append(
            OpenPosition(
                account_id=account_id,
                account_name=first.account_name,
                instrument_id=instrument_id,
                symbol=first.symbol,
                quantity=quantity,
                cost_basis=cost_basis,
                multiplier=first.multiplier,
                direction=next(iter(directions))
                if len(directions) == 1 and Direction.SPREAD not in directions
                else None,
                is_estimated=any(r.is_estimated for r in group),
                unvaluable_reason="; ".join(reasons) if reasons else None,
                trade_count=len(group),
            )
        )

    # Symbols are NOT unique (only instrument.natural_key is) and account
    # names are NOT unique either (nothing stops two accounts sharing a
    # name) -- so this sorts primarily for a human (symbol, then account
    # name) but the trailing (account_id, instrument_id) pair is what makes
    # the order a TOTAL one with no ties: it is exactly the tuple used as
    # this function's own grouping key above, so any two distinct output
    # rows necessarily differ in it, however their symbol and account name
    # compare.
    return tuple(
        sorted(
            out,
            key=lambda p: (p.symbol, p.account_name, str(p.account_id), str(p.instrument_id)),
        )
    )
