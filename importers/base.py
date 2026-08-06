"""Canonical import types and the dedupe hash. Pure — no I/O, no clock.

Importers map venue rows to these types and never touch the database. Import is
three-phase — parse, preview, commit — so nothing is written before it is seen.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Protocol
from uuid import UUID

from ledger.types import Instrument, Side

# Canonical sign convention for CanonicalCash.amount: amount is ALWAYS positive;
# direction is carried entirely by `kind`, never by the sign of `amount`. A
# negative deposit or a negative withdrawal is not representable — a negative
# `amount` is always a bug, in an importer or in a consumer, not a legitimate
# outflow. Every importer must normalize with abs() at the point `amount` is
# built (see importers/coinbase.py and importers/fidelity.py), rather than
# leaving each venue's raw export sign to leak through. `OUTFLOW_KINDS` is
# defined ONCE here so a consumer that needs to net cash movements (e.g. sum
# deposits minus withdrawals/fees) has a single shared source for "which kinds
# subtract" instead of every caller inventing its own sign map.
OUTFLOW_KINDS = frozenset({"withdrawal", "fee"})


def _escape(part: str) -> str:
    """Make a component free of the '|' delimiter, injectively.

    '%' is escaped first so the mapping cannot be ambiguous.
    """
    return part.replace("%", "%25").replace("|", "%7C")


def _canon(value: Decimal) -> str:
    """Render a Decimal so 10, 10.0 and 10.00 hash identically.

    Decimal precision is pinned to 50 digits to prevent silent data loss:
    if ambient precision were low, normalize() could round away detail,
    making genuinely different quantities hash identically and causing
    one to be silently dropped as a duplicate on import.
    """
    with localcontext() as ctx:
        ctx.prec = 50
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            return str(normalized.quantize(Decimal(1)))
        return str(normalized)


def content_hash(
    account_id: UUID,
    executed_at: datetime,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    occurrence: int = 0,
) -> str:
    """Dedupe key for exports that carry no venue fill id.

    Account-scoped, so the same trade in two accounts is two fills.
    Requires timezone-aware executed_at so the same instant always hashes
    identically regardless of which timezone offset it arrived in.

    `occurrence` distinguishes genuinely repeated rows that otherwise hash
    identically — e.g. two identical Fidelity trades on the same day, where the
    export carries no time component at all. The caller assigns 0, 1, 2, ... to
    successive rows with the same (executed_at, symbol, side, quantity, price)
    shape within a batch, in the order they appear. That is stable across
    re-imports of the same file (the same rows always get the same indices in
    the same order), so re-importing still dedupes to zero, while two distinct
    same-day repeats no longer collide onto the same hash. Default 0 keeps
    every caller that never has same-shape repeats unaffected.
    """
    if executed_at.tzinfo is None:
        raise ValueError("content_hash requires a timezone-aware executed_at")

    payload = "|".join(
        [
            str(account_id),
            executed_at.astimezone(UTC).isoformat(),
            _escape(symbol.upper()),
            _escape(side.lower()),
            _canon(quantity),
            _canon(price),
            str(occurrence),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class CanonicalFill:
    instrument: Instrument
    executed_at: datetime
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    venue_fill_id: str | None = None
    venue_order_id: str | None = None
    external_ref: str | None = None  # venue's account number, for routing
    # 'external' = the user's own capital. 'reinvestment' = bought with a
    # distribution the position itself produced. Both carry real cost basis;
    # the distinction exists so contributed_capital can exclude reinvestment
    # while cost_basis stays tax-correct. Constrained by fill_funding_source_chk.
    funding_source: str = "external"


@dataclass(frozen=True, slots=True)
class CanonicalCash:
    occurred_at: datetime
    kind: str
    amount: Decimal  # always positive — see OUTFLOW_KINDS docstring above
    currency: str
    symbol: str | None = None
    venue_ref: str | None = None
    external_ref: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ImportBatch:
    fills: tuple[CanonicalFill, ...] = ()
    cash: tuple[CanonicalCash, ...] = ()
    warnings: tuple[str, ...] = ()
    unmapped_rows: tuple[str, ...] = ()


class Importer(Protocol):
    venue: str

    def parse(self, text: str) -> ImportBatch:
        """Map a venue export to canonical rows. Never writes anything."""
        ...
