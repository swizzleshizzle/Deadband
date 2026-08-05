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
) -> str:
    """Dedupe key for exports that carry no venue fill id.

    Account-scoped, so the same trade in two accounts is two fills.
    Requires timezone-aware executed_at so the same instant always hashes
    identically regardless of which timezone offset it arrived in.
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


@dataclass(frozen=True, slots=True)
class CanonicalCash:
    occurred_at: datetime
    kind: str
    amount: Decimal
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
