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

# "Price ($)" -> "price". Real Fidelity exports suffix every money column with a
# currency parenthetical; the fixtures did not, so every price, commission, fee
# and cash amount resolved to a missing key and _decimal(None)'s Decimal("0")
# silently replaced it — no warning, quantities and dates intact, the result
# plausible and financially meaningless. Strip the parenthetical structurally
# rather than aliasing the observed spellings: the export's own disclaimer text
# writes "Fees($)" without the space its header row uses, so an alias table
# would be one Fidelity inconsistency away from silently zeroing a column again.
_FIELD_QUALIFIER_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _normalize_field(name: str | None) -> str:
    return _FIELD_QUALIFIER_RE.sub("", (name or "").strip().lower()).strip()


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

    "Run Date" alone is not enough, though: a real preamble line like
    "Report run date: 08/04/2026" also contains the phrase, and would be
    accepted as the header if that were the only test — csv.DictReader would
    then take its field names from that prose, and every data row would fail
    to parse (a "bad date" warning per row, zero usable rows) instead of the
    file parsing normally. Require a second expected column name on the same
    line too, so a preamble sentence that merely mentions "run date" is never
    mistaken for the actual header row.

    Returns (lines_from_header_onward, preamble_line_count) so callers can
    report warnings against the real file line number rather than an offset
    one.
    """
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        lowered = line.lower()
        if "run date" in lowered and ("action" in lowered or "amount" in lowered):
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
        for line_no, raw_row in enumerate(reader, start=preamble_offset + 2):
            # Normalize header casing once — a real export's header is found
            # case-insensitively (above), so the fields must be read the same
            # way or a differently-cased header parses to zero usable rows.
            row = {_normalize_field(k): v for k, v in raw_row.items()}
            action = (row.get("action") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip()
            account = (row.get("account") or "").strip() or None

            try:
                when = datetime.strptime((row.get("run date") or "").strip(), "%m/%d/%Y").replace(
                    tzinfo=UTC
                )
            except ValueError as exc:
                warnings.append(f"line {line_no}: bad date ({exc})")
                unmapped.append(str(raw_row))
                continue

            cash_kind = next((v for k, v in _CASH_ACTIONS.items() if action.startswith(k)), None)
            if cash_kind:
                try:
                    amount = _decimal(row.get("amount"))
                except InvalidOperation as exc:
                    warnings.append(f"line {line_no}: bad amount ({exc})")
                    unmapped.append(str(raw_row))
                    continue
                # Decimal("Infinity")/Decimal("NaN") are valid constructions and slip
                # past the `except InvalidOperation` above (same hazard as quantity/
                # price below); cash_movement.amount has no CHECK constraint to catch
                # one downstream.
                if not amount.is_finite():
                    warnings.append(f"line {line_no}: non-finite amount, skipped")
                    unmapped.append(str(raw_row))
                    continue
                # Canonical convention (see importers.base.OUTFLOW_KINDS): amount is
                # always positive, direction lives in `kind` alone. Fidelity's raw
                # Amount column is signed (negative for a withdrawal/purchase-style
                # outflow, e.g. "ELECTRONIC FUNDS TRANSFER PAID" exports -2000.00),
                # so this abs() is load-bearing here, unlike Coinbase's twin where
                # the raw export is already positive.
                amount = abs(amount)
                cash.append(
                    CanonicalCash(
                        occurred_at=when,
                        kind=cash_kind,
                        amount=amount,
                        currency="USD",
                        symbol=symbol or None,
                        external_ref=account,
                        note=(row.get("description") or "").strip() or None,
                    )
                )
                continue

            if "BOUGHT" not in action and "SOLD" not in action:
                warnings.append(f"line {line_no}: unhandled action {action!r}")
                unmapped.append(str(raw_row))
                continue

            try:
                raw_qty = _decimal(row.get("quantity"))
                price = _decimal(row.get("price"))
                fee = _decimal(row.get("commission")) + _decimal(row.get("fees"))
            except InvalidOperation as exc:
                warnings.append(f"line {line_no}: bad number ({exc})")
                unmapped.append(str(raw_row))
                continue

            if raw_qty == 0:
                warnings.append(f"line {line_no}: zero quantity, skipped")
                unmapped.append(str(raw_row))
                continue

            # Decimal("NaN")/Decimal("Infinity") are valid constructions, so they are
            # not caught by the `except InvalidOperation` above. Left unchecked,
            # Infinity survives Fill.__post_init__'s `quantity > 0` check and the
            # DB's `quantity > 0` CHECK, becoming a live allocation in group_fills.
            # fee is included too: Fill.__post_init__ never validates fee, and
            # Postgres NUMERIC (PG14+) accepts Infinity, so nothing else catches it.
            if not raw_qty.is_finite() or not price.is_finite() or not fee.is_finite():
                warnings.append(f"line {line_no}: non-finite number, skipped")
                unmapped.append(str(raw_row))
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
