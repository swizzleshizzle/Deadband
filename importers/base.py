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
OUTFLOW_KINDS = frozenset({"withdrawal", "fee", "tax"})


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
    # Every distinct account ref seen in the RAW rows, whether or not the row
    # went on to become a fill or cash movement. Deriving "which accounts are
    # in this file" from fills/cash alone is blind to an account whose rows
    # are entirely unmapped (e.g. every action is one the classifier doesn't
    # know) -- that account then contributes nothing to fills or cash and is
    # invisible to any report built from them. refs_seen exists so a caller
    # can report on accounts, not just on successfully classified rows.
    refs_seen: tuple[str, ...] = ()
    # Reasons the WHOLE batch must not commit. The venue's action vocabulary
    # is open-ended, so an unmapped row is guaranteed -- but blocking on every
    # unmapped row is unworkable (a real export's trailing legal disclaimer is
    # permanently unmapped by design, so nothing could ever commit) and
    # blocking on none is exactly how the defect that motivated this whole
    # effort looked like success. So only a row that both parsed a valid date
    # AND carries a non-zero quantity or amount, and that no rule matched,
    # belongs here -- a row with no financial content only warns. Empty means
    # "safe to commit," never "nothing was unmapped" (see unmapped_rows/
    # warnings for that).
    blocking: tuple[str, ...] = ()


def zero_price_warning(
    line_no: int, symbol: str, quantity: Decimal, price: Decimal
) -> str | None:
    """A fill-shaped row (real quantity) priced at zero is almost always a
    parse failure, not a free trade.

    This is the defect that started the whole effort: a real export names its
    money columns with a currency suffix, the importer read the bare names,
    missed every one, and `_decimal(None)` silently returned `Decimal("0")`
    for each -- no warning, dates/quantities/symbols all correct, the result
    plausible and financially meaningless. Downstream of `_decimal` a missing
    column and a genuine zero are indistinguishable, so the check must live
    HERE, at the point of parsing the row, while the distinction still
    exists -- not in any consumer of the already-built CanonicalFill.

    Shared by every importer building a fill (see importers/fidelity.py and
    importers/coinbase.py) so the guard can never drift between venues, and so
    a venue added later gets it by construction rather than by remembering to
    copy it.
    """
    if quantity != 0 and price == 0:
        return f"line {line_no}: {symbol} has quantity {quantity} at zero price"
    return None


class Importer(Protocol):
    venue: str

    def parse(self, text: str) -> ImportBatch:
        """Map a venue export to canonical rows. Never writes anything."""
        ...
