"""Quotes, and the one conversion every provider must go through.

Spec D7: every mark carries its source, so a delayed third-party price is
never mistaken for a broker-confirmed one. `Quote` is the shape that carries
that provenance out of a provider and into the proposal the user reviews.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

# Four decimal places. Two is wrong: DEMCF quoted 0.0795 when this was
# written, and rounding that to 0.08 is a 0.6% error across the whole
# position. More than four is provider noise rather than precision -- Yahoo's
# own display rounds well before that.
_SCALE = Decimal("0.0001")


def quantize_price(value: float | str | Decimal) -> Decimal:
    """A provider's price as a Decimal the ledger can store.

    Goes through str() deliberately. `Decimal(0.53)` is
    0.52999999999999999822364316059974953532218933105468750 -- the binary
    float's exact value -- while `Decimal(str(0.53))` is `Decimal("0.53")`.
    Yahoo returns precisely this shape: 0.5299999713897705 for a 53c option.

    Refuses negative and non-finite values rather than passing them on.
    `mark_price_chk` is `price >= 0 AND price < 'Infinity'`, so both would be
    refused by the database anyway -- as a 500 from a route, several layers
    from the provider that produced them. Migration 002 exists because NaN and
    Infinity reached NUMERIC columns once already.
    """
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError(f"refusing a non-finite quote: {value!r}")
    if d < 0:
        raise ValueError(f"refusing a negative quote: {value!r}")
    return d.quantize(_SCALE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    currency: str
    # None when the provider does not say. Never defaulted to now(): "I do not
    # know how old this is" and "this is current" are different claims, and
    # the screen shows the difference so a stale quote can be rejected.
    quoted_at: datetime | None
    source: str
