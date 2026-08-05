"""Domain types. Pure — no I/O, no clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class AssetClass(StrEnum):
    CRYPTO_SPOT = "crypto_spot"
    CRYPTO_PERP = "crypto_perp"
    EQUITY = "equity"
    OPTION = "option"
    FUTURE = "future"


class FillSource(StrEnum):
    MANUAL = "manual"
    CSV = "csv"
    API = "api"
    OPENING_BALANCE = "opening_balance"


class TradeStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class TradeIntent(StrEnum):
    TRADE = "trade"
    INVESTMENT = "investment"
    UNASSIGNED = "unassigned"


class Direction(StrEnum):
    LONG = "long"
    SHORT = "short"
    SPREAD = "spread"


class GroupingMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Instrument:
    id: UUID | None
    asset_class: AssetClass
    symbol: str
    quote_currency: str
    underlying: str | None = None
    strike: Decimal | None = None
    expiry: date | None = None
    option_right: str | None = None  # "call" | "put"
    root: str | None = None
    contract_multiplier: Decimal = Decimal(1)
    chain: str | None = None
    contract_address: str | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    id: UUID | None
    account_id: UUID
    instrument_id: UUID
    executed_at: datetime
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    source: FillSource
    venue_fill_id: str | None
    is_estimated: bool
    venue_order_id: str | None = None
    content_hash: str | None = None

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError(f"fill quantity must be positive, got {self.quantity}")
        if self.price < 0:
            raise ValueError(f"fill price must not be negative, got {self.price}")
        if self.executed_at.tzinfo is None:
            raise ValueError("executed_at must be timezone-aware (UTC)")
        object.__setattr__(self, "executed_at", self.executed_at.astimezone(UTC))

    @property
    def signed_quantity(self) -> Decimal:
        """Position delta. Direction lives here, never in the stored quantity."""
        return self.quantity if self.side is Side.BUY else -self.quantity


def _escape(part: str) -> str:
    """Make a key component free of the ':' delimiter, injectively.
    '%' is escaped first so the mapping cannot be ambiguous."""
    return part.replace("%", "%25").replace(":", "%3A")


def _normalize_decimal(value: Decimal) -> str:
    """500 and 500.00 must produce the same key, or one contract becomes two."""
    normalized = value.normalize()
    # normalize() renders large integers in scientific notation; undo that.
    if normalized == normalized.to_integral_value():
        return str(normalized.quantize(Decimal(1)))
    return format(normalized, "f")


def instrument_natural_key(instrument: Instrument) -> str:
    """Stable identity for an instrument. The uniqueness constraint in the database
    is built on this, so two spellings of the same contract must collapse to one key."""
    cls = instrument.asset_class
    quote = _escape(instrument.quote_currency.upper())

    if cls is AssetClass.OPTION:
        if not (
            instrument.underlying
            and instrument.strike is not None
            and instrument.expiry
            and instrument.option_right
        ):
            raise ValueError("option instruments require underlying, strike, expiry, right")
        return ":".join(
            [
                cls.value,
                _escape(instrument.underlying.upper()),
                instrument.expiry.isoformat(),
                _escape(_normalize_decimal(instrument.strike)),
                _escape(instrument.option_right.lower()),
                quote,
            ]
        )

    if cls is AssetClass.FUTURE:
        if not (instrument.root and instrument.expiry):
            raise ValueError("future instruments require root and expiry")
        return ":".join(
            [
                cls.value,
                _escape(instrument.root.upper()),
                instrument.expiry.isoformat(),
                quote,
            ]
        )

    if instrument.contract_address:
        chain = _escape((instrument.chain or "").lower())
        return ":".join([cls.value, chain, _escape(instrument.contract_address.lower()), quote])

    return ":".join([cls.value, _escape(instrument.symbol.upper()), quote])
