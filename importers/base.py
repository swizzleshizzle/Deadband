"""Canonical import types and the dedupe hash. Pure — no I/O, no clock.

Importers map venue rows to these types and never touch the database. Import is
three-phase — parse, preview, commit — so nothing is written before it is seen.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
OUTFLOW_KINDS = frozenset({"withdrawal", "fee", "tax", "transfer_out"})


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
class CanonicalTransfer:
    """The share leg of an outbound ACAT (branch B): shares leave the account
    with their basis; `market_value` is the broker's stamp, informational only
    and never a transaction price. Written directly by commit_batch (spec D5)
    -- account-scoped and mechanical like fills, unlike corporate actions,
    which stay proposals."""

    instrument: Instrument
    occurred_at: datetime
    quantity: Decimal  # always positive; direction is implicitly 'out' (spec D2)
    market_value: Decimal | None
    venue_ref: str | None = None
    external_ref: str | None = None  # venue's account number, for routing
    note: str | None = None


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
class CorporateActionProposal:
    """A logical corporate action, grouped from one or more recognised
    CORPORATE_ACTION rows (see importers/fidelity.py's Outcome.CORPORATE_ACTION
    and _group_corporate_actions). Never written anywhere -- `cli.py`'s
    `corporate add` is the only path that turns one into a stored action, and
    nothing in this import pipeline calls it. `ratio` is always None coming
    out of `importers/`; Task 3 fills it (spec §6), and the spinoff case is
    filled later still, by `cli.py` against a ledger holding (spec §6, last
    row of the table) -- which is why the field is optional here at all.
    """
    kind: str                       # 'reverse_split' | 'name_change' | 'merger' | 'spinoff'
    ex_date: date
    source_cusip: str | None
    resulting_cusip: str | None
    description: str                # the venue's own text, for a human to identify it
    quantities: tuple[Decimal, ...]  # the evidence the ratio was derived from
    ratio: tuple[Decimal, Decimal] | None = None   # filled by Task 3; None until then
    # Where `ratio` came from -- spec §6a's "use what there is and record
    # which source the ratio came from", so a consumer never has to
    # re-derive provenance from `kind` alone. One of:
    #   'constant'         -- name_change's fixed 1:1, no row data involved.
    #   'derived'          -- from the paired rows' quantities (spec §6),
    #                         with NO stated ratio in the text to check it
    #                         against. Exactly one source existed.
    #                         Deliberately NOT used for a confirmed
    #                         two-source match -- see 'derived+confirmed' --
    #                         and, since the final fix wave, no longer used
    #                         for a two-source DISAGREEMENT either: that is
    #                         'derived+disputed' below. Collapsing those two
    #                         made the consumer's own sentence ("no
    #                         independent confirmation was found in the
    #                         venue's own text") false on every real reverse
    #                         split in the exports, where confirmation WAS
    #                         found and disagreed.
    #   'derived+disputed' -- from the paired rows' quantities, AND a ratio
    #                         WAS stated in the text, and the two disagree
    #                         (spec §6a's cross-check firing). `approximate`
    #                         is True and `stated_ratio` carries the other
    #                         candidate, so a consumer can show both numbers
    #                         rather than presenting one as unopposed. Every
    #                         reverse split in the real exports lands here:
    #                         the text states a whole "N FOR N" while the
    #                         paired quantities -- one lot, its fractional
    #                         remainder cashed out rather than converted --
    #                         reduce to something else entirely.
    #   'derived+confirmed' -- from the paired rows' quantities AND matching
    #                         the ratio stated in the text (spec §6a's
    #                         "strongest evidence available"). Kept distinct
    #                         from plain 'derived' so a consumer can tell
    #                         "two sources agreed" from "only one source
    #                         existed" -- collapsing them would make it
    #                         impossible to tell a confirmed cross-check from
    #                         one that never ran at all.
    #   None               -- spinoff (spec §6, last row: not derivable from
    #                         the file), or a merger (structurally never
    #                         derivable -- spec §6, corrected: a merger's
    #                         group is always 3 rows, but deriving a ratio
    #                         needs exactly 2, so the two rules can never
    #                         both hold; see importers/fidelity.py's
    #                         _derive_quantity_ratio).
    ratio_source: str | None = None
    # True when the derived ratio DISAGREES with the ratio stated in the
    # venue's own description text (spec §6a's cross-check) -- e.g. a
    # fractional share paid out as cash-in-lieu instead of converting, or a
    # misparse of either source. Reducing raw quantities by their own gcd
    # always "succeeds" trivially (a/gcd and b/gcd reconstruct a and b
    # exactly by construction), so this can only be set by comparing against
    # the independent, second source -- never by inspecting the derived pair
    # alone. `quantities` above still carries the raw evidence either way, so
    # a human can see the distortion even when `ratio` and `approximate`
    # disagree about what happened.
    approximate: bool = False
    # The ratio the venue's OWN description text states, reduced -- the
    # second, independent source `ratio_source` and `approximate` are decided
    # by. None when the text states no ratio at all (spec §6a's single-source
    # case), and None for every kind whose ratio never comes from a text
    # cross-check (name_change's constant, spinoff, merger).
    #
    # Carried on the proposal rather than left in a warning string: a
    # disagreement is only adjudicable with BOTH numbers in front of you, and
    # warnings go to stderr while the proposal section goes to stdout (D5 --
    # the section is meant to be the self-contained decision surface). Before
    # this field existed, the one number needed to settle the disagreement
    # was never in the artefact the user acts on.
    stated_ratio: tuple[Decimal, Decimal] | None = None
    # The parent instrument's own ticker, when the venue's row STATES it --
    # Fidelity's spinoff rows read "DISTRIBUTION SPINOFF FROM:(TICKER )" with
    # the CHILD in the Symbol column, so the parent is stated, not merely
    # inferrable. None for every other kind (whose parent side is identified
    # by CUSIP and quantity sign instead) and for any spinoff row that
    # carries no such token.
    #
    # This is the only identifier on the proposal that is a SYMBOL rather
    # than a CUSIP, and it exists because the consumer that needs it
    # (cli.py's _complete_spinoff_ratio) matches against ledger instruments,
    # which are keyed by symbol. Without it that consumer had to identify the
    # parent by elimination -- "the account's sole LONG holding" -- which is
    # ambiguous on 100% of the real accounts (see gap #47).
    parent_symbol: str | None = None
    # The ticker a share distribution was received ON -- the row's own Symbol
    # column. Distinct from parent_symbol, which a SPINOFF row states about a
    # DIFFERENT instrument: here the subject and the instrument receiving
    # shares are the same one, which is why the split ratio reads that
    # instrument's own holding rather than another's. None for every other
    # kind, and for any row whose Symbol column is empty.
    subject_symbol: str | None = None
    group_ref: str | None = None    # the #REOR reference, or None when the fallback keyed it


@dataclass(frozen=True, slots=True)
class ImportBatch:
    fills: tuple[CanonicalFill, ...] = ()
    cash: tuple[CanonicalCash, ...] = ()
    # Outbound ACAT share legs (branch B). Committed directly with content-hash
    # dedupe, same trust level as fills/cash; see CanonicalTransfer.
    transfers: tuple[CanonicalTransfer, ...] = ()
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
    # Proposed corporate actions grouped from CORPORATE_ACTION rows (spec
    # §5) -- never applied here. `cli.py`'s `corporate add` is the only
    # place one of these is turned into a stored action; nothing in this
    # pipeline calls it. Empty means either no corporate-action rows were
    # present, or every group found was reported as unrecognised instead
    # (see `warnings`) -- never "recognised but silently dropped."
    corporate_actions: tuple[CorporateActionProposal, ...] = ()
    # Cash-in-lieu-of-fractional-shares rows, kept OUT of `corporate_actions`
    # deliberately: it moves real cash (gap #43, which is the same arithmetic
    # gap #35 tracks one layer up for merger cash) and is never
    # applied, so listing it beside the proposals would imply an action
    # `corporate add` can record, which it cannot (spec §7, D6). Each entry
    # is the row's own description text, verbatim, so a human can still see
    # it happened even though nothing acts on it.
    cash_in_lieu: tuple[str, ...] = ()


def _locator(where: int | str) -> str:
    """Render a row coordinate for a warning message.

    An `int` is a CSV line number and renders as `line N` -- the idiom every
    CSV importer here uses. A `str` is a locator the caller has already
    formatted for its own row shape: importers/coinbase_api.py parses a JSON
    array, where the coordinate is `fill 3 (trade_id='t4')`, not a line.
    Passing the array index to the int form produced "line 0: BTC ..." from a
    parser whose every other message said "fill 0 (trade_id=...)" -- the same
    row named two different ways in one batch's warning list (M3).
    """
    return f"line {where}" if isinstance(where, int) else where


def zero_price_warning(
    where: int | str, symbol: str, quantity: Decimal, price: Decimal
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
        return f"{_locator(where)}: {symbol} has quantity {quantity} at zero price"
    return None


def zero_amount_warning(where: int | str, kind: str, amount: Decimal) -> str | None:
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

    `where` follows zero_price_warning's convention exactly (see `_locator`).
    It has no non-CSV caller today; taking the same parameter shape anyway is
    deliberate, because "an invariant applied correctly in one place and not
    in its twin" is the defect shape this file's docstrings keep naming.
    """
    if amount == 0:
        return f"{_locator(where)}: {kind} cash movement has zero amount"
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
