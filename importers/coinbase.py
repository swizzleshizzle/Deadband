"""Coinbase transaction CSV → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from importers.base import (
    CanonicalCash,
    CanonicalFill,
    ImportBatch,
    zero_amount_warning,
    zero_price_warning,
)
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
        # I4: the money-carrying-unmapped-row blocking policy (see
        # importers/fidelity.py and ImportBatch.blocking's docstring) is
        # venue-neutral in the spec -- the plan narrowed it to Fidelity only,
        # so this venue never populated `blocking` at all, and an
        # unrecognised transaction type carrying real money (the shipped
        # fixture's own "Convert" row, for instance) never refused a commit.
        # Each entry is (external_ref, message) for symmetry with Fidelity's
        # ImportBatch.blocking, even though this importer never populates a
        # row's external_ref at all (Coinbase's export carries no per-row
        # account number) -- every entry's ref is therefore always None.
        blocking: list[tuple[str | None, str]] = []

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
                # Decimal("NaN")/Decimal("Infinity") are valid constructions, so they
                # are not caught by the `except InvalidOperation` above. This check
                # MUST run before any ordering comparison (`<=`, `<`, `>`, `>=`)
                # touches quantity/price/fee: `Decimal("NaN") <= 0` itself raises
                # InvalidOperation (NaN is unordered, per IEEE 754), which would
                # escape this loop's own try/except (that one only wraps the parse
                # above) and abort the entire file instead of costing one row. Left
                # unchecked otherwise, Infinity survives Fill.__post_init__'s
                # `quantity > 0` check and the DB's `quantity > 0` CHECK, becoming a
                # live allocation. fee is included here too: Fill.__post_init__ never
                # validates fee at all, and Postgres NUMERIC (PG14+) happily stores
                # Infinity, so a non-finite fee has no other guard anywhere on its
                # way to the DB.
                if not quantity.is_finite() or not price.is_finite() or not fee.is_finite():
                    warnings.append(f"line {line_no}: non-finite number, skipped")
                    unmapped.append(str(row))
                    continue

                # A blank/zero Quantity Transacted parses fine (as Decimal("0")) and
                # would otherwise become a CanonicalFill that survives preview, then
                # raises inside Fill.__post_init__ during commit — taking the whole
                # batch down with it, not just this row. Fidelity's twin importer
                # guards this by taking abs(quantity) before the equality check, so
                # only exactly-zero rows reach it; Coinbase's Quantity Transacted is
                # used as-is (never abs()'d), so a negative value here reaches
                # Fill.__post_init__'s `quantity <= 0` check unguarded too — hence
                # `<= 0`, not `== 0`. This ordering comparison is now safe because
                # the finiteness check above already ran and skipped any NaN.
                if quantity <= 0:
                    warnings.append(f"line {line_no}: non-positive quantity, skipped")
                    unmapped.append(str(row))
                    continue

                # Same defect class as Fidelity's twin (see
                # importers.base.zero_price_warning's docstring): a real
                # quantity priced at zero is almost always a parse failure --
                # e.g. a currency-suffixed money column the importer's bare
                # header names missed -- not a free trade. Report it, but
                # still record the fill.
                warn = zero_price_warning(line_no, asset, quantity, price)
                if warn is not None:
                    warnings.append(warn)

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
                # Canonical convention (see importers.base.OUTFLOW_KINDS): amount is
                # always positive, direction lives in `kind` alone. Coinbase's raw
                # Quantity Transacted is already positive for every cash type this
                # importer maps, but abs() here pins that as an invariant rather
                # than an accident, so this importer can never drift from Fidelity's
                # twin (which must abs() a genuinely negative export amount).
                amount = abs(quantity if asset == currency else quantity * price)
                # Same non-finite hazard as the fill branch above: a poison
                # Quantity Transacted or Spot Price reaches `amount` (directly, or
                # via multiplication) with nothing downstream to catch it —
                # cash_movement.amount has no CHECK constraint at all.
                if not amount.is_finite():
                    warnings.append(f"line {line_no}: non-finite amount, skipped")
                    unmapped.append(str(row))
                    continue
                # For non-fiat cash (e.g., rewards in ETH), warn if price is missing.
                # This already explains a zero amount for that specific shape (no
                # spot price to convert quantity into a dollar figure), so it takes
                # priority over the generic zero_amount_warning below -- firing
                # both would double-warn the same row for the same underlying
                # cause.
                if asset != currency and price == 0:
                    warnings.append(
                        f"line {line_no}: {row.get('Transaction Type')!r} in {asset} has no "
                        "spot price; amount recorded as 0 and needs manual valuation"
                    )
                else:
                    # C2: cash rows had no equivalent of the fill branch's
                    # zero_price_warning -- a zero/blank Quantity Transacted on a
                    # cash-shaped row (e.g. a deposit) silently produced a $0.00
                    # cash movement with no warning at all. Warn, but still
                    # record it -- same reasoning as zero_price_warning on the
                    # fill side.
                    warn = zero_amount_warning(line_no, _CASH_TYPES[kind], amount)
                    if warn is not None:
                        warnings.append(warn)
                cash.append(
                    CanonicalCash(
                        occurred_at=when,
                        kind=_CASH_TYPES[kind],
                        amount=amount,
                        currency=currency,
                        symbol=None if asset == currency else asset,
                        note=(row.get("Notes") or "").strip() or None,
                    )
                )
            else:
                # Never drop a row silently — an unrecognized type is a reporting gap.
                msg = f"line {line_no}: unhandled transaction type {row.get('Transaction Type')!r}"
                warnings.append(msg)
                unmapped.append(str(row))
                # Same reasoning as Fidelity's twin guard: blocking on every
                # unrecognised type is unworkable (the venue's type
                # vocabulary is open-ended), and blocking on none of them is
                # exactly how the silent-loss defect this task exists to
                # close looked like success. Only a row that ALSO carries
                # money (a non-zero Quantity Transacted, already parsed
                # above for every row) refuses the commit.
                if quantity != 0:
                    blocking.append((None, msg))

        return ImportBatch(
            fills=tuple(fills),
            cash=tuple(cash),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
            blocking=tuple(blocking),
        )
