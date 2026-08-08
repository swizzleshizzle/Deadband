# importers/coinbase_api.py
"""Coinbase Advanced Trade fills JSON → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from importers.base import CanonicalFill, ImportBatch, zero_price_warning
from ledger.types import AssetClass, Instrument, Side

_SIDES = {"BUY": Side.BUY, "SELL": Side.SELL}


def _decimal(raw: object) -> Decimal:
    if isinstance(raw, Decimal):
        return raw
    return Decimal(str(raw).strip() or "0")


def _when(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)


class CoinbaseAPIImporter:
    venue = "coinbase-api"

    def parse(self, text: str) -> ImportBatch:
        fills: list[CanonicalFill] = []
        warnings: list[str] = []
        unmapped: list[str] = []
        blocking: list[tuple[str | None, str]] = []

        def reject(raw: dict, idx: int, reason: str) -> None:
            """ONE path for every dropped fill, same discipline as
            importers/fidelity.py's reject(). An API row always carries
            money -- it is a trade execution -- so unlike the CSV importers
            there is no 'no financial content' branch that only warns.

            Every message embeds BOTH `idx` (the fill's position in the
            document's `fills` array -- the direct JSON analogue of the CSV
            importers' `line N`) and `trade_id` (the venue's identifier for
            the row, when present). idx locates the row in the document;
            trade_id identifies it at the venue -- a garbled or missing
            trade_id is exactly the case idx exists to disambiguate. Before
            this, `idx` was accepted by every call site and never used, so
            two different malformed rows lacking a trade_id produced
            byte-identical blocking messages and were indistinguishable in
            a batch with more than one bad row.
            """
            message = f"fill {idx} (trade_id={raw.get('trade_id')!r}): {reason}"
            warnings.append(message)
            unmapped.append(str(raw))
            blocking.append((None, message))

        if not text.strip():
            return ImportBatch()

        # parse_float=Decimal: an unquoted JSON number would otherwise arrive
        # as a float and silently lose precision on the way to NUMERIC.
        document = json.loads(text, parse_float=Decimal)

        for idx, raw in enumerate(document.get("fills") or []):
            # size_in_quote flips the MEANING of `size` from base units to
            # quote currency. There is no conversion available from the fill
            # alone, and guessing produces a position wrong by the price --
            # so refuse, loudly, rather than record something plausible.
            if raw.get("size_in_quote"):
                reject(
                    raw,
                    idx,
                    "size_in_quote is set, so `size` is denominated in the quote "
                    "currency, not the base asset -- refusing to record it as a "
                    "quantity",
                )
                continue

            side = _SIDES.get(str(raw.get("side", "")).strip().upper())
            if side is None:
                reject(raw, idx, f"unknown side {raw.get('side')!r}")
                continue

            # .strip(): product_id is otherwise the one field read without
            # normalizing incidental whitespace, while `side` two lines above
            # is -- an invariant applied correctly in one place and not its
            # twin, the exact defect shape docs/known-gaps.md names. Leaving
            # it unstripped would let " BTC-USD" or "BTC-USD " (or a
            # trailing/leading space that survives the split) through to
            # base/quote unnoticed, polluting the instrument's symbol or
            # quote_currency rather than rejecting the row.
            product = str(raw.get("product_id") or "").strip()
            base, _, quote = product.partition("-")
            if not base or not quote:
                reject(raw, idx, f"unparseable product_id {product!r}")
                continue

            try:
                quantity = _decimal(raw.get("size"))
                price = _decimal(raw.get("price"))
                fee = _decimal(raw.get("commission"))
                when = _when(str(raw.get("trade_time")))
            except (InvalidOperation, ValueError) as exc:
                reject(raw, idx, f"unparseable ({exc})")
                continue

            if not all(v.is_finite() for v in (quantity, price, fee)):
                reject(raw, idx, "non-finite number")
                continue
            if quantity <= 0:
                reject(raw, idx, "non-positive quantity")
                continue

            warn = zero_price_warning(idx, base, quantity, price)
            if warn is not None:
                warnings.append(warn)

            fills.append(
                CanonicalFill(
                    instrument=Instrument(
                        id=None,
                        asset_class=AssetClass.CRYPTO_SPOT,
                        symbol=base.upper(),
                        quote_currency=quote.upper(),
                    ),
                    executed_at=when,
                    side=side,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    fee_currency=quote.upper(),
                    venue_fill_id=str(raw.get("trade_id")),
                    venue_order_id=str(raw.get("order_id")) if raw.get("order_id") else None,
                )
            )

        return ImportBatch(
            fills=tuple(fills),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
            blocking=tuple(blocking),
        )
