"""Canonical import types and the dedupe hash. Pure — no I/O, no clock.

Importers map venue rows to these types and never touch the database. Import is
three-phase — parse, preview, commit — so nothing is written before it is seen.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Protocol
from uuid import UUID

from ledger.types import Instrument, Side

# "Price ($)" -> "price", "Total (inclusive of fees and/or spread)" -> "total".
# Real venue exports commonly suffix (or qualify) a money column with a
# trailing parenthetical -- a currency denomination for Fidelity, a
# descriptive qualifier for Coinbase. Strip it STRUCTURALLY rather than
# aliasing the observed spellings: Fidelity's own export is inconsistent with
# itself (its trailing disclaimer text writes "Fees($)" without the space its
# header row uses), so an alias table would be one venue inconsistency away
# from silently zeroing a column again -- see importers/fidelity.py's
# original comment on this, which this shares with importers/coinbase.py so
# the two venues can never drift onto two different normalization schemes.
_FIELD_QUALIFIER_RE = re.compile(r"\s*\([^)]*\)\s*$")


def normalize_field(name: str | None) -> str:
    """Case- and qualifier-insensitive header key: lowercased, trailing
    parenthetical stripped, whitespace trimmed. Every importer must build its
    row dict by normalizing every key this same way (see importers/fidelity.py
    and importers/coinbase.py) and look fields up by their normalized name --
    an importer that reads even one raw, exact-cased header name is one
    venue re-casing or renaming away from silently reading that column as
    missing, which _decimal(None) then turns into a silent Decimal("0") with
    no warning at all. This is the exact defect that started the whole
    effort (see zero_price_warning's docstring)."""
    return _FIELD_QUALIFIER_RE.sub("", (name or "").strip().lower()).strip()

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
    #
    # Each entry is (external_ref, reason) -- the row's own account ref (or
    # None for a venue that carries no per-row account, e.g. Coinbase), not
    # just the message text. A blocking row belonging to an account the user
    # has registered `ignore_on_import` (see db.accounts) must not refuse an
    # import it was never going to be part of -- see cli.py's cmd_import,
    # which drops any reason whose ref is in RoutingPlan.ignored_refs AFTER
    # route_batch runs, rather than refusing on blocking unconditionally
    # before an account's ignore status is even known.
    blocking: tuple[tuple[str | None, str], ...] = ()


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


def zero_amount_warning(line_no: int, kind: str, amount: Decimal) -> str | None:
    """A cash movement recorded at zero amount is almost always a parse
    failure, not a genuine zero-dollar cash event.

    zero_price_warning (above) covers FILLS -- a real quantity priced at
    zero. It was shared across venues but never across row KINDS: cash
    movements (dividends, interest, transfers, fees, ...) are built from
    their own `_decimal(...)` call in both importers, with no equivalent
    check. Renaming the fixture's Amount column to something the importer's
    bare header lookup misses reproduces the exact same silent-zero defect
    zero_price_warning exists to catch, on the larger half of the rows: a
    fully "successful" parse in which every cash figure is $0.00 and nothing
    says so.

    Shared by every importer building a cash movement (see
    importers/fidelity.py and importers/coinbase.py), same rationale as
    zero_price_warning: the guard can never drift between venues, and a
    venue added later gets it by construction.
    """
    if amount == 0:
        return f"line {line_no}: {kind} cash movement has zero amount"
    return None


class Importer(Protocol):
    venue: str
    # The account.venue this importer's rows belong to -- what a caller must
    # route/match against when deciding which registered account a parsed
    # batch may commit into. For every CSV importer this equals `venue`: the
    # importer's own identity IS the venue whose accounts it feeds. It
    # differs from `venue` only when an importer's own name is a TRANSPORT,
    # not a venue -- the coinbase-api case: "coinbase-api" identifies the
    # Advanced Trade API as a distinct parser from the CSV importer (they
    # dedupe on different keys, see importers/registry.py), but there is no
    # "coinbase-api" account anywhere -- every real account is registered
    # under the plain "coinbase" venue, whether its fills arrived via CSV or
    # API. A caller that compares against `venue` instead of `account_venue`
    # here reintroduces exactly the bug this field exists to make
    # structurally impossible to repeat: see cli.py's _preview_or_commit,
    # which uses account_venue for every importer, and its docstring for the
    # sync-coinbase incident that motivated adding this field.
    account_venue: str

    def parse(self, text: str) -> ImportBatch:
        """Map a venue export to canonical rows. Never writes anything."""
        ...
