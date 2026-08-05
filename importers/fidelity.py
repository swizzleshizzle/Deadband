"""Fidelity account-activity CSV → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import csv
import io
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from importers.base import CanonicalCash, CanonicalFill, ImportBatch
from ledger.types import AssetClass, Instrument, Side

# -SPY260919C500  →  underlying SPY, 2026-09-19, call, strike 500
_OPTION_RE = re.compile(
    r"^-(?P<underlying>[A-Z]+)(?P<yy>\d{2})(?P<mm>\d{2})(?P<dd>\d{2})"
    r"(?P<right>[CP])(?P<strike>\d+(?:\.\d+)?)$"
)

_CASH_ACTIONS = {
    "DIVIDEND RECEIVED": "dividend",
    "ELECTRONIC FUNDS TRANSFER RECEIVED": "deposit",
    "ELECTRONIC FUNDS TRANSFER PAID": "withdrawal",
    "INTEREST EARNED": "interest",
}


def parse_option_symbol(symbol: str) -> Instrument | None:
    """Parse Fidelity's option symbol. Returns None for anything that isn't one
    (including a syntactically matching symbol with an impossible calendar date —
    a parse failure must fall back to being treated as an equity, not crash)."""
    match = _OPTION_RE.match((symbol or "").strip().upper())
    if not match:
        return None
    g = match.groupdict()
    try:
        expiry = datetime(2000 + int(g["yy"]), int(g["mm"]), int(g["dd"]), tzinfo=UTC).date()
    except ValueError:
        return None
    return Instrument(
        id=None,
        asset_class=AssetClass.OPTION,
        symbol=symbol.strip().upper(),
        quote_currency="USD",
        underlying=g["underlying"],
        strike=Decimal(g["strike"]),
        expiry=expiry,
        option_right="call" if g["right"] == "C" else "put",
        contract_multiplier=Decimal("100"),
    )


def _decimal(raw: str | None) -> Decimal:
    cleaned = (raw or "").replace("$", "").replace(",", "").strip()
    return Decimal(cleaned) if cleaned else Decimal("0")


def _locate_header(text: str) -> tuple[list[str], int]:
    """Find the header row and split off any preamble before it.

    Real Fidelity exports commonly carry a few preamble lines (report title,
    generation date, blank lines) before the actual "Run Date,Account,..."
    header. Assuming line 1 is the header would fail such an export wholesale,
    so scan for the first line that names "Run Date" instead.

    Returns (lines_from_header_onward, preamble_line_count) so callers can
    report warnings against the real file line number rather than an offset
    one.
    """
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if "run date" in line.lower():
            return lines[idx:], idx
    return lines, 0


class FidelityImporter:
    venue = "fidelity"

    def parse(self, text: str) -> ImportBatch:
        fills: list[CanonicalFill] = []
        cash: list[CanonicalCash] = []
        warnings: list[str] = []
        unmapped: list[str] = []

        # Strip UTF-8 BOM if present — Fidelity exports carry them too, and a
        # BOM makes csv.DictReader name the first field "﻿Run Date"
        # instead of "Run Date", so every row would fail to parse.
        text = text.lstrip("﻿")

        if not text.strip():
            return ImportBatch()

        # Real exports carry preamble lines before the header row (and a
        # disclaimer block after the data, which falls out naturally below as
        # unmapped rows with warnings rather than being silently dropped).
        data_lines, preamble_offset = _locate_header(text)
        if not data_lines:
            return ImportBatch()

        reader = csv.DictReader(io.StringIO("\n".join(data_lines)))
        for line_no, row in enumerate(reader, start=preamble_offset + 2):
            action = (row.get("Action") or "").strip().upper()
            symbol = (row.get("Symbol") or "").strip()
            account = (row.get("Account") or "").strip() or None

            try:
                when = datetime.strptime((row.get("Run Date") or "").strip(), "%m/%d/%Y").replace(
                    tzinfo=UTC
                )
            except ValueError as exc:
                warnings.append(f"line {line_no}: bad date ({exc})")
                unmapped.append(str(row))
                continue

            cash_kind = next((v for k, v in _CASH_ACTIONS.items() if action.startswith(k)), None)
            if cash_kind:
                cash.append(
                    CanonicalCash(
                        occurred_at=when,
                        kind=cash_kind,
                        amount=_decimal(row.get("Amount")),
                        currency="USD",
                        symbol=symbol or None,
                        external_ref=account,
                        note=(row.get("Description") or "").strip() or None,
                    )
                )
                continue

            if "BOUGHT" not in action and "SOLD" not in action:
                warnings.append(f"line {line_no}: unhandled action {action!r}")
                unmapped.append(str(row))
                continue

            try:
                raw_qty = _decimal(row.get("Quantity"))
                price = _decimal(row.get("Price"))
                fee = _decimal(row.get("Commission")) + _decimal(row.get("Fees"))
            except InvalidOperation as exc:
                warnings.append(f"line {line_no}: bad number ({exc})")
                unmapped.append(str(row))
                continue

            if raw_qty == 0:
                warnings.append(f"line {line_no}: zero quantity, skipped")
                unmapped.append(str(row))
                continue

            instrument = parse_option_symbol(symbol) or Instrument(
                id=None,
                asset_class=AssetClass.EQUITY,
                symbol=symbol.upper(),
                quote_currency="USD",
            )

            fills.append(
                CanonicalFill(
                    instrument=instrument,
                    executed_at=when,
                    # Direction comes from the action, not the sign — "SOLD" is
                    # authoritative and the sign is corroboration.
                    side=Side.SELL if "SOLD" in action else Side.BUY,
                    quantity=abs(raw_qty),
                    price=price,
                    fee=fee,
                    fee_currency="USD",
                    external_ref=account,
                )
            )

        return ImportBatch(
            fills=tuple(fills),
            cash=tuple(cash),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
        )
