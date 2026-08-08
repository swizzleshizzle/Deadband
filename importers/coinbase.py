"""Coinbase transaction CSV → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

from importers.base import (
    CanonicalCash,
    ImportBatch,
    normalize_field,
    zero_amount_warning,
)

# Trade-row transaction types this importer RECOGNISES but, since the
# gap-6 cut-over below, deliberately does not map to fills. Kept as a set
# (not the Side-keyed dict this used to be) because nothing downstream of
# `kind in _FILL_TYPES` reads a value anymore -- see the warning in the
# branch below for why these rows are reported instead of silently dropped.
_FILL_TYPES = {"buy", "advanced trade buy", "sell", "advanced trade sell"}

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


def _carries_money(raw: str | None) -> bool:
    """True if a raw quantity/subtotal/total field is non-zero -- or is
    present but unparseable, which must be treated as "might carry money"
    rather than silently read as empty. Mirrors importers/fidelity.py's twin:
    blocking on a false positive costs a human a glance; failing OPEN on a
    garbled money field (silently reading it as if it were absent/zero and
    letting the row through unblocked) is exactly the silent-loss failure
    mode this whole task exists to close. Returning True here -- refusing to
    treat "unparseable" as "zero" -- is failing CLOSED: it is the
    mitigation, not the failure mode."""
    try:
        return _decimal(raw) != 0
    except InvalidOperation:
        return True


def _row_carries_money(row: dict[str, str]) -> bool:
    """Coinbase's notion of "carries money" is its own, unlike Fidelity's
    single Amount column: a row carries money if it has a non-zero Quantity
    Transacted OR a non-zero Subtotal/Total, judged on the RAW strings
    before conversion. Subtotal/Total matter independently of Quantity
    Transacted because a cash-shaped row's dollar figure can live there even
    when quantity itself is what's garbled (or vice versa for a fill-shaped
    row).

    `row` MUST already be normalized (see normalize_field in
    importers/base.py and this importer's parse(), which builds it the same
    way importers/fidelity.py does) -- finding B: this used to look up the
    raw, exact-cased header names ("Quantity Transacted", "Subtotal",
    "Total (inclusive of fees and/or spread)") directly. If the venue
    re-cases or renames a column, every one of those lookups misses, this
    function returns False, and a money-carrying row silently stops
    blocking -- the same shape as the defect that motivated the whole task,
    just on header casing instead of a currency suffix. Reading from the
    normalized row (lowercased, trailing parenthetical qualifier stripped)
    is what fixes that, mirroring importers/fidelity.py's own normalization
    instead of inventing a second scheme."""
    return (
        _carries_money(row.get("quantity transacted"))
        or _carries_money(row.get("subtotal"))
        or _carries_money(row.get("total"))
    )


class CoinbaseImporter:
    venue = "coinbase"
    # Equal to `venue`: this importer's own identity IS the account venue
    # (see importers/base.py's Importer.account_venue docstring). No
    # Protocol-level default reaches here -- CoinbaseImporter satisfies
    # Importer structurally rather than by inheriting from it, so the
    # attribute has to be set explicitly on every concrete importer.
    account_venue = "coinbase"

    def parse(self, text: str) -> ImportBatch:
        # M8: no `fills` accumulator any more. Since the gap-6 cut-over (below)
        # this importer emits CASH ONLY -- every trade row is reported and
        # points at `sync coinbase`. The empty list that used to be built here
        # and returned as `fills=tuple(fills)` was the last vestige of the
        # retired fill path, and it made the cut-over invisible at a glance.
        # ImportBatch.fills defaults to (), which is now the honest statement.
        cash: list[CanonicalCash] = []
        warnings: list[str] = []
        unmapped: list[str] = []
        # I4: the money-carrying-unmapped-row blocking policy (see
        # importers/fidelity.py and ImportBatch.blocking's docstring) is
        # venue-neutral in the spec. It was first wired up here for ONLY the
        # "unhandled transaction type" branch -- but that left every
        # matched-but-bad-data path (top-level parse failure, non-finite
        # quantity/price/fee, non-positive quantity in the fill branch,
        # non-finite amount in the cash branch) still appending to
        # `unmapped`/`warnings` directly and never to `blocking`. A row that
        # DID match a rule (Buy/Sell/Deposit/...) but carried a garbled or
        # negative quantity/price/fee/amount therefore dropped a real dollar
        # figure with only a warning nobody has to read, while --commit
        # proceeded with rc=0 -- the exact asymmetry already closed for
        # Fidelity (finding I3). `reject()` below is the one path every
        # unmapped branch now flows through, same shape as Fidelity's twin.
        # Each entry is (external_ref, message) for symmetry with Fidelity's
        # ImportBatch.blocking, even though this importer never populates a
        # row's external_ref at all (Coinbase's export carries no per-row
        # account number) -- every entry's ref is therefore always None.
        blocking: list[tuple[str | None, str]] = []

        def reject(
            row: dict[str, str], raw_row: dict[str, str], line_no: int, message: str
        ) -> None:
            """ONE path for every row parse() drops as unmapped -- top-level
            parse failure, non-finite quantity/price/fee, non-positive
            quantity, non-finite cash amount, and "unhandled transaction
            type" alike. See the `blocking` comment above for why routing
            every such row through this one function (instead of only the
            "no rule matched" branch consulting `_row_carries_money`) is
            what stops the asymmetry from recurring the next time a new
            guard is added.

            `row` is the NORMALIZED dict (see normalize_field/parse() below)
            -- `_row_carries_money` must read normalized keys (finding B).
            `raw_row` is the original, as-exported dict, kept only for the
            unmapped-row display text so a human sees the venue's own header
            spelling rather than the normalized one -- mirrors
            importers/fidelity.py's identical row/raw_row split."""
            warnings.append(message)
            unmapped.append(str(raw_row))
            if _row_carries_money(row):
                blocking.append((None, message))

        # Strip UTF-8 BOM if present (common in Coinbase exports)
        text = text.lstrip("﻿")

        if not text.strip():
            return ImportBatch()

        reader = csv.DictReader(io.StringIO(text))
        for line_no, raw_row in enumerate(reader, start=2):
            # Normalize header casing once, same as importers/fidelity.py's
            # twin -- finding B: reading even one raw, exact-cased header
            # name is one venue re-casing or renaming away from silently
            # missing that column and reading it as Decimal("0") with no
            # warning at all.
            row = {normalize_field(k): v for k, v in raw_row.items()}
            kind = (row.get("transaction type") or "").strip().lower()
            asset = (row.get("asset") or "").strip().upper()
            currency = (row.get("spot price currency") or "USD").strip().upper()

            try:
                when = datetime.fromisoformat(
                    (row.get("timestamp") or "").replace("Z", "+00:00")
                ).astimezone(UTC)
                quantity = _decimal(row.get("quantity transacted", ""))
                price = _decimal(row.get("spot price at transaction", ""))
                # "Fees and/or Spread" is deliberately NOT parsed here anymore.
                # It was read only to populate a fill's `fee` -- the only
                # branch that ever consumed it -- and that branch is gone
                # (gap 6: fills come from the API now). Cash movements never
                # used it. Parsing a field with no reader left would be dead
                # work, and validating it would fail closed on garbage a
                # cash-only import has no reason to care about.
            except (ValueError, InvalidOperation) as exc:
                reject(row, raw_row, line_no, f"line {line_no}: could not parse row ({exc})")
                continue

            if kind in _FILL_TYPES:
                # §10 gap 6, closed 2026-08-08: Coinbase fills are imported
                # from the Advanced Trade API (`deadband sync coinbase`),
                # keyed on the venue's own trade id. Mapping them here too
                # would give one fill two dedupe keys -- content_hash from
                # this path, venue_fill_id from that one -- so a fill
                # imported by both would not dedupe against itself.
                #
                # Reported, never silently skipped: a trade row vanishing
                # without a word is the same silent-loss shape as the defect
                # that started this effort. It does NOT block, because a
                # cash-only Coinbase CSV import is now the intended use.
                warnings.append(
                    f"line {line_no}: {kind!r} is a trade row -- Coinbase fills are "
                    "imported via `deadband sync coinbase` (coinbase-api), not from CSV"
                )
                continue
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
                    reject(row, raw_row, line_no, f"line {line_no}: non-finite amount, skipped")
                    continue
                # For non-fiat cash (e.g., rewards in ETH), warn if price is missing.
                # This already explains a zero amount for that specific shape (no
                # spot price to convert quantity into a dollar figure), so it takes
                # priority over the generic zero_amount_warning below -- firing
                # both would double-warn the same row for the same underlying
                # cause.
                if asset != currency and price == 0:
                    warnings.append(
                        f"line {line_no}: {row.get('transaction type')!r} in {asset} has no "
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
                        note=(row.get("notes") or "").strip() or None,
                    )
                )
            else:
                # Never drop a row silently — an unrecognized type is a reporting gap.
                # Same reasoning as Fidelity's twin guard: blocking on every
                # unrecognised type is unworkable (the venue's type
                # vocabulary is open-ended), and blocking on none of them is
                # exactly how the silent-loss defect this task exists to
                # close looked like success. reject() only escalates to
                # blocking when the row ALSO carries money (see
                # _row_carries_money).
                reject(
                    row,
                    raw_row,
                    line_no,
                    f"line {line_no}: unhandled transaction type {row.get('transaction type')!r}",
                )

        return ImportBatch(
            cash=tuple(cash),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
            blocking=tuple(blocking),
        )
