"""Coinbase transaction CSV → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from importers.base import CanonicalCash, CanonicalFill, ImportBatch
from ledger.types import AssetClass, Instrument, Side

_FILL_TYPES = {
    "buy": Side.BUY,
    "advanced trade buy": Side.BUY,
    "sell": Side.SELL,
    "advanced trade sell": Side.SELL,
}

_CASH_TYPES = {
    "deposit": "deposit",
    "withdrawal": "withdrawal",
    "rewards income": "interest",
    "staking income": "interest",
    "inflation reward": "interest",
    "interest": "interest",
}


def _decimal(raw: str) -> Decimal:
    return Decimal((raw or "0").replace("$", "").replace(",", "").strip() or "0")


class CoinbaseImporter:
    venue = "coinbase"

    def parse(self, text: str) -> ImportBatch:
        fills: list[CanonicalFill] = []
        cash: list[CanonicalCash] = []
        warnings: list[str] = []
        unmapped: list[str] = []

        # Strip UTF-8 BOM if present (common in Coinbase exports)
        text = text.lstrip("﻿")

        if not text.strip():
            return ImportBatch()

        reader = csv.DictReader(io.StringIO(text))
        for line_no, row in enumerate(reader, start=2):
            kind = (row.get("Transaction Type") or "").strip().lower()
            asset = (row.get("Asset") or "").strip().upper()
            currency = (row.get("Spot Price Currency") or "USD").strip().upper()

            try:
                when = datetime.fromisoformat(
                    (row.get("Timestamp") or "").replace("Z", "+00:00")
                ).astimezone(UTC)
                quantity = _decimal(row.get("Quantity Transacted", ""))
                price = _decimal(row.get("Spot Price at Transaction", ""))
                fee = _decimal(row.get("Fees and/or Spread", ""))
            except (ValueError, InvalidOperation) as exc:
                warnings.append(f"line {line_no}: could not parse row ({exc})")
                unmapped.append(str(row))
                continue

            if kind in _FILL_TYPES:
                # A blank/zero Quantity Transacted parses fine (as Decimal("0")) and
                # would otherwise become a CanonicalFill that survives preview, then
                # raises inside Fill.__post_init__ during commit — taking the whole
                # batch down with it, not just this row. Same guard as Fidelity's
                # twin (importers/fidelity.py).
                if quantity == 0:
                    warnings.append(f"line {line_no}: zero quantity, skipped")
                    unmapped.append(str(row))
                    continue

                # Decimal("NaN")/Decimal("Infinity") are valid constructions, so they
                # are not caught by the `except InvalidOperation` above. Left
                # unchecked, Infinity survives Fill.__post_init__'s `quantity > 0`
                # check and the DB's `quantity > 0` CHECK, becoming a live allocation.
                if not quantity.is_finite() or not price.is_finite():
                    warnings.append(f"line {line_no}: non-finite number, skipped")
                    unmapped.append(str(row))
                    continue

                fills.append(
                    CanonicalFill(
                        instrument=Instrument(
                            id=None,
                            asset_class=AssetClass.CRYPTO_SPOT,
                            symbol=asset,
                            quote_currency=currency,
                        ),
                        executed_at=when,
                        side=_FILL_TYPES[kind],
                        quantity=quantity,
                        price=price,
                        fee=fee,
                        fee_currency=currency,
                    )
                )
            elif kind in _CASH_TYPES:
                # For non-fiat cash (e.g., rewards in ETH), warn if price is missing
                if asset != currency and price == 0:
                    warnings.append(
                        f"line {line_no}: {row.get('Transaction Type')!r} in {asset} has no "
                        "spot price; amount recorded as 0 and needs manual valuation"
                    )
                cash.append(
                    CanonicalCash(
                        occurred_at=when,
                        kind=_CASH_TYPES[kind],
                        amount=quantity if asset == currency else quantity * price,
                        currency=currency,
                        symbol=None if asset == currency else asset,
                        note=(row.get("Notes") or "").strip() or None,
                    )
                )
            else:
                # Never drop a row silently — an unrecognized type is a reporting gap.
                warnings.append(
                    f"line {line_no}: unhandled transaction type {row.get('Transaction Type')!r}"
                )
                unmapped.append(str(row))

        return ImportBatch(
            fills=tuple(fills),
            cash=tuple(cash),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
        )
