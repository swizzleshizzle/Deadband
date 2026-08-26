"""Fidelity account-activity CSV → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import csv
import enum
import io
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation

from importers.base import (
    CanonicalCash,
    CanonicalFill,
    CanonicalTransfer,
    CorporateActionProposal,
    ImportBatch,
    normalize_field,
    zero_amount_warning,
    zero_price_warning,
)
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
# plausible and financially meaningless. normalize_field (importers/base.py)
# strips the parenthetical structurally rather than aliasing the observed
# spellings, and is shared with importers/coinbase.py (finding B) so the two
# venues can never drift onto two different normalization schemes: the
# export's own disclaimer text writes "Fees($)" without the space its header
# row uses, so an alias table would be one Fidelity inconsistency away from
# silently zeroing a column again.
_normalize_field = normalize_field


# Membership is DATA, not logic, so it can be reviewed at a glance. Identified
# explicitly rather than by `price == 1.00`: a real security can trade at
# exactly a dollar, and that heuristic would silently convert a genuine
# position into cash.
#
# Use the venue's PUBLISHED sweep vehicles -- the full documented list, not
# only the ones this user happens to hold. A sweep ticker is product
# infrastructure attached to essentially every account at the venue, so the
# complete list discloses nothing about anyone's holdings, and completeness is
# what keeps a sweep from being misclassified as a position.
SWEEP_SYMBOLS: frozenset[str] = frozenset({
    "SPAXX", "FDRXX", "FZFXX", "SPRXX", "FDLXX", "QPIHQ",
})


def is_sweep(symbol: str | None) -> bool:
    return (symbol or "").strip().upper() in SWEEP_SYMBOLS


# Sweep funds hold a $1.00 NAV by construction. Used ONLY to WARN (see
# _sweep_par_warning below), never to classify -- see SWEEP_SYMBOLS' docstring
# for why price must never drive classification.
_SWEEP_PAR = Decimal("1.00")
_SWEEP_PAR_TOLERANCE = Decimal("0.01")


def _sweep_par_warning(symbol: str, raw_price: str | None) -> str | None:
    """Surface the two ways SWEEP_SYMBOLS can silently decay, on a
    REINVESTMENT row (the only place a per-unit price is meaningfully
    comparable to a sweep's $1.00 NAV).

    1. A LISTED sweep priced away from par: the set has acquired a non-sweep
       symbol, or a genuine sweep broke the buck.
    2. An UNLISTED symbol reinvesting at par: sweep_only=False in the rule
       table means "not one of these six," not "is a real security" -- an
       unlisted sweep gets classified as a security, its reinvestment leg
       becomes a fill that spends the dividend, and a phantom position
       appears with nothing warning. This is the direction that costs money.

    A spurious warning here (a genuine $1 security) is cheap -- a human
    dismisses it in seconds. That asymmetry is exactly why this heuristic is
    fine here and is NOT fine in `is_sweep`/`classify`.
    """
    try:
        price = _decimal(raw_price)
    except InvalidOperation:
        return None
    if not price.is_finite():
        return None

    sym = (symbol or "").strip().upper()
    deviation = abs(price - _SWEEP_PAR)

    if is_sweep(symbol):
        if deviation > _SWEEP_PAR_TOLERANCE:
            return (
                f"{sym} is a listed sweep symbol but priced at {price}, "
                f"{deviation} away from its ${_SWEEP_PAR} par -- SWEEP_SYMBOLS "
                "may have acquired a non-sweep symbol, or the sweep broke the buck"
            )
    elif deviation <= _SWEEP_PAR_TOLERANCE:
        return (
            f"{sym} is not in SWEEP_SYMBOLS but reinvested at {price}, within "
            f"${_SWEEP_PAR_TOLERANCE} of the ${_SWEEP_PAR} sweep par -- "
            "SWEEP_SYMBOLS may be missing this ticker"
        )
    return None


class Outcome(enum.Enum):
    FILL = "fill"
    CASH = "cash"
    # Recognised and deliberately produces nothing. Exists to prevent
    # double-counting: a sweep dividend appears as BOTH a dividend row and a
    # reinvestment of that dividend back into the sweep. Since the sweep IS
    # cash (A2-9), those are two legs of one event; recording both counts the
    # money twice. The dividend leg records, this leg does not.
    INTERNAL = "internal"
    # An option leaving the book because it expired worthless. Produces a
    # CLOSING fill at price zero. Distinct from FILL because the zero is a
    # constant this code supplies rather than a value parsed from the row,
    # so zero_price_warning must not run on it -- see build_expiry_fill.
    EXPIRY = "expiry"
    # Recognised and deliberately REFUSED. Scope is expiry-only by decision
    # E1 of the spec. A realistic ASSIGNED/EXERCISED row already blocks on
    # its own nonzero Quantity via the ordinary carries-money check; this
    # exists so the refusal names the verb, and blocks unconditionally
    # rather than depending on what the row's money columns happen to hold.
    UNSUPPORTED = "unsupported"
    # An outbound ACAT (branch B): the share leg becomes an asset_transfer
    # write, the cash residual a transfer_out cash movement -- both committed
    # directly with content-hash dedupe, unlike corporate actions, which stay
    # proposals (spec D5). Any other shape under the verb -- an inbound-looking
    # row above all -- REFUSES the file (spec D6): an inbound transfer arrives
    # with basis this ledger has no source for, and guessing would be worse.
    TRANSFER = "transfer"
    # Recognised, produces nothing, and does NOT block -- the row's follow-up
    # is a `corporate add` proposal, not a fill or a cash movement.
    #
    # Distinct from INTERNAL, which also produces nothing but has no follow-up,
    # and from UNSUPPORTED, which is recognised and deliberately REFUSED. These
    # rows carry a nonzero quantity, so before this existed they hit the
    # money-carrying-unmapped policy and refused the entire import -- the same
    # shape investment_gain_loss was added for, and the reason two accounts
    # could not be imported at all.
    CORPORATE_ACTION = "corporate_action"


@dataclass(frozen=True, slots=True)
class Rule:
    name: str
    verb: str                       # matched against the action's leading text
    outcome: Outcome
    cash_kind: str | None = None
    side: Side | None = None
    funding_source: str = "external"
    sweep_only: bool | None = None  # None = symbol irrelevant to this rule


RULES: tuple[Rule, ...] = (
    # Ordered: more specific verbs first. `test_every_rule_is_reachable`
    # fails if any rule is shadowed by an earlier one.
    Rule("reinvest_sweep", "REINVESTMENT", Outcome.INTERNAL, sweep_only=True),
    Rule("reinvest_security", "REINVESTMENT", Outcome.FILL,
         side=Side.BUY, funding_source="reinvestment", sweep_only=False),
    Rule("exchange_sweep", "EXCHANGED TO", Outcome.INTERNAL, sweep_only=True),
    # F1, DECIDED 2026-08-08. An employer-plan row reporting the period's
    # market-value change on a plan holding -- not a transaction, and not
    # money entering or leaving the account. Recording it as CASH would
    # inject money that never moved AND double-count appreciation the ledger
    # already derives from positions and prices.
    #
    # INTERNAL rather than "leave it unmapped": the row carries a dollar
    # figure in Amount, so under §8 an unmapped one BLOCKS the commit -- a
    # real export contains several, and could not be imported at all until
    # this rule existed. "Recognised, and deliberately produces nothing" is
    # the honest description, and it is what INTERNAL means.
    #
    # sweep_only is None (symbol irrelevant) because plan rows carry NO
    # symbol at all -- the fund is named only in Description. Do not tighten
    # this to a symbol predicate without re-reading the plan dialect's shape
    # in tests/fixtures/fidelity/real_shape_activity.csv.
    Rule("investment_gain_loss", "INVESTMENT GAIN/LOSS", Outcome.INTERNAL),
    # Corporate actions -- History dialect only (the Activity & Orders
    # fixtures never carry one; see tests/test_fidelity_history.py). Verbs
    # observed on real export rows: "REVERSE SPLIT R/S FROM/TO ...", "NAME
    # CHANGED N/C FROM/TO ...", "MERGER MER FROM/PAYOUT ...", "DISTRIBUTION
    # SPINOFF FROM:(...) ...", and "IN LIEU OF FRX SHARE ... PAYOUT ..." for
    # the cash paid out on the fractional remainder a reverse split leaves.
    # None of these five prefixes overlaps any other rule's verb in either
    # direction, so their position in RULES is not load-bearing today -- the
    # mutation gate (moving them to the end of RULES, after expired_option)
    # left every test green. See the task report for the full record.
    Rule("reverse_split", "REVERSE SPLIT", Outcome.CORPORATE_ACTION),
    Rule("name_change", "NAME CHANGED", Outcome.CORPORATE_ACTION),
    Rule("merger", "MERGER", Outcome.CORPORATE_ACTION),
    Rule("spinoff_distribution", "DISTRIBUTION SPINOFF", Outcome.CORPORATE_ACTION),
    Rule("cash_in_lieu", "IN LIEU OF", Outcome.CORPORATE_ACTION),
    # ORDERING IS LOAD-BEARING HERE, unlike the corporate-action block above:
    # classify() is startswith + first-match-wins and "DISTRIBUTION" is a
    # proper prefix of "DISTRIBUTION SPINOFF". This rule MUST stay after
    # spinoff_distribution -- placed before it, every spinoff in every export
    # silently reclassifies as a share distribution.
    Rule("share_distribution", "DISTRIBUTION", Outcome.CORPORATE_ACTION),
    Rule("dividend_received", "DIVIDEND RECEIVED", Outcome.CASH, cash_kind="dividend"),
    Rule("dividends", "DIVIDENDS", Outcome.CASH, cash_kind="dividend"),
    Rule("interest", "INTEREST EARNED", Outcome.CASH, cash_kind="interest"),
    Rule("return_of_capital", "RETURN OF CAPITAL", Outcome.CASH,
         cash_kind="return_of_capital"),
    Rule("foreign_tax", "FOREIGN TAX PAID", Outcome.CASH, cash_kind="tax"),
    Rule("fee_charged", "FEE CHARGED", Outcome.CASH, cash_kind="fee"),
    Rule("recordkeeping_fee", "RECORDKEEPING FEE", Outcome.CASH, cash_kind="fee"),
    Rule("revenue_credit", "REVENUE CREDIT", Outcome.CASH, cash_kind="rebate"),
    Rule("eft_in", "ELECTRONIC FUNDS TRANSFER RECEIVED", Outcome.CASH,
         cash_kind="deposit"),
    Rule("eft_out", "ELECTRONIC FUNDS TRANSFER PAID", Outcome.CASH,
         cash_kind="withdrawal"),
    Rule("cash_contribution", "CASH CONTRIBUTION", Outcome.CASH, cash_kind="deposit"),
    Rule("employer_contribution", "CO CONTR", Outcome.CASH, cash_kind="deposit"),
    Rule("participant_contribution", "PARTIC CONTR", Outcome.CASH, cash_kind="deposit"),
    Rule("contributions", "CONTRIBUTIONS", Outcome.CASH, cash_kind="deposit"),
    # Retirement cash flows. D1: these map to the GENERIC kinds, not to
    # retirement-specific ones -- cash_movement.kind is a CHECK constraint
    # with no retirement value in it, and the four contribution rules above
    # already collapse the same way. A later tax-reporting feature wanting
    # the distinction back recovers it from the note.
    #
    # The verb is "ROLLOVER CASH CHECK", not the shorter "ROLLOVER": both
    # observed variants (one carries a trailing MOBILE DEPOSIT) share that
    # prefix, and the narrower one does not speculate about ROLLOVER verbs
    # the exports have never shown.
    Rule("rollover_deposit", "ROLLOVER CASH CHECK", Outcome.CASH, cash_kind="deposit"),
    Rule("early_distribution", "EARLY DIST", Outcome.CASH, cash_kind="withdrawal"),
    # Branch B. One rule for the whole verb family: the two legs (shares vs
    # residual cash) and the inbound refusal are told apart by row SHAPE in
    # parse(), not by verb text -- see Outcome.TRANSFER's docstring.
    Rule("acat_transfer", "TRANSFER OF ASSETS", Outcome.TRANSFER),
    Rule("expired_option", "EXPIRED", Outcome.EXPIRY),
    Rule("assigned_option", "ASSIGNED", Outcome.UNSUPPORTED),
    Rule("exercised_option", "EXERCISED", Outcome.UNSUPPORTED),
)


def classify(action: str, symbol: str) -> Rule | None:
    """First match wins. Keyed on action AND symbol, because the reinvestment
    rule resolves differently for a sweep fund than for a real security and
    cannot be expressed by the action alone."""
    a = (action or "").strip().upper()
    for rule in RULES:
        if not a.startswith(rule.verb):
            continue
        if rule.sweep_only is not None and rule.sweep_only != is_sweep(symbol):
            continue
        return rule
    return None


# The `#REOR` reorganisation reference. Verified against the real exports,
# not invented -- see tests/test_fidelity_history.py's module docstring,
# which spec §5 requires this to be derived from rather than guessed at. A
# reference is a shared BASE plus a trailing per-leg suffix. What is
# actually verified (against both this fixture and the real exports): that
# suffix is three constant zeros plus one varying digit -- e.g.
# `M9990000010001` and `M9990000010000` are two legs of ONE event because
# only their LAST CHARACTER differs, not because the two full references are
# equal. The data does not distinguish a wider suffix -- the leading zeros
# are constant in every observed case, so stripping 1 character or 4
# produces the identical grouping partition here -- so this strips only the
# one character that is verified to vary. See _reor_base's docstring for why
# that choice (not "4 happens to also work") is deliberate. NOT because the
# trailing character predicts anything about the row's role in the event --
# see _derive_cusip_pair below for why role is read from quantity sign and
# the paren-adjacent cusip instead.
_REOR_RE = re.compile(r"#REOR\s+(\S+)")
_REOR_LEG_SUFFIX_LEN = 1


def _reor_base(token: str) -> str:
    """The shared portion of a #REOR reference that ties one event's rows
    together -- see _REOR_RE's comment just above for what is actually
    verified.

    Stripping only the last character (rather than the full observed
    "three zeros plus a digit" suffix) is deliberate, not merely sufficient:
    the two failure modes are asymmetric. A base string that is too SHORT
    (over-stripping) can silently merge two distinct events that happen to
    share a longer common prefix into one confidently wrong proposal, which
    spec §7 forbids outright. A base string that is too LONG (under-
    stripping, this function's direction) can only ever split legs of the
    same event across two keys, which the leg-count check in
    _group_corporate_actions turns into a loud "unrecognised" warning
    instead of silent data loss. Spec §7's whole posture is report rather
    than guess, so the fail-loud direction is the one this code takes. Do
    not widen this without re-reading tests/test_fidelity_history.py's
    module docstring first."""
    if len(token) <= _REOR_LEG_SUFFIX_LEN:
        return token
    return token[:-_REOR_LEG_SUFFIX_LEN]


# A cusip-shaped token inside parentheses in the row's action text. This is
# the row's OWN entity -- the security the description text right before the
# parenthetical names -- not a counterparty. Confirmed by cross-referencing
# each row's own ISIN/SEDOL against its paren-adjacent token: on the
# reverse-split pair, the TO row's paren token matches the ISIN that row's
# own Description also carries, and likewise for the FROM row -- so the
# paren token is self-describing, not a reference to the other leg. (An
# earlier version of this code read the token immediately before "#REOR"
# instead, which is actually the FROM/TO verb's COUNTERPARTY argument, and
# produced an inverted source/resulting pair as a result -- see
# _derive_cusip_pair.)
#
# The SHAPE is a plain CUSIP: exactly nine alphanumerics. An earlier version
# of this pattern required letters-then-digits (`[A-Z]{1,6}\d{5,9}`), fitted
# to a fabricated fixture token rather than to the real exports, and matched
# ZERO of the corporate-action rows in five years of them -- so source_cusip
# and resulting_cusip were always None in production and the whole
# CUSIP-reporting path was dead on real data while four tests certified it.
# Nine alphanumerics is deliberately not narrowed to "digits first": the
# real rows do all begin with digits, but a CINS (a foreign issuer's CUSIP)
# begins with a LETTER by construction, and refusing to match one would
# reintroduce exactly the same blind spot for the next export.
#
# The parenthesis anchor is what keeps this from over-matching, and it is
# load-bearing: "(Cash)" is 4 characters and a bare ticker like "(ZXCO )"
# or "(ZXCB )" is at most 5, so neither can be nine. Measured across every
# real export, every 9-character uppercase-alphanumeric parenthesised token
# present is a CUSIP -- there are no other tokens of that shape to collide
# with.
_PAREN_CUSIP_RE = re.compile(r"\(([0-9A-Z]{9})\)")

# The parent a spinoff distributes FROM, stated by the venue's own row:
# "DISTRIBUTION SPINOFF FROM:(TICKER ) <child description>", with the CHILD
# in the row's Symbol column. The parent is therefore a fact the row
# supplies, not an inference -- which is what lets cli.py stop identifying
# it by elimination (see CorporateActionProposal.parent_symbol and gap #47).
#
# Anchored to "FROM:" rather than reusing the paren scan: several other row
# kinds carry a parenthesised ticker too (a name change's resulting leg, a
# cash-in-lieu row), and none of those is a spinoff parent. Trailing
# whitespace inside the parenthesis is the venue's own padding and is
# dropped. "(Cash)" cannot match -- it is neither preceded by "FROM:" nor
# upper-case.
_SPINOFF_PARENT_RE = re.compile(r"FROM:\s*\(\s*([A-Z][A-Z0-9.\-]{0,9})\s*\)")


def _parse_spinoff_parent(action: str) -> str | None:
    """The ticker of the instrument a spinoff was distributed on, as the row
    itself states it -- see _SPINOFF_PARENT_RE. None when the row states
    none, in which case the consumer falls back to its own inference."""
    match = _SPINOFF_PARENT_RE.search(action or "")
    return match.group(1) if match else None


def _parse_reor(action: str) -> tuple[str | None, tuple[str, ...]]:
    """Extract the row's own #REOR reference (or None) and any cusip-shaped
    tokens found in parentheses in the row's action text -- see
    _PAREN_CUSIP_RE's comment for why the paren token, not the token
    adjacent to "#REOR", is the row's own entity."""
    ref_match = _REOR_RE.search(action)
    reor_ref = ref_match.group(1) if ref_match else None
    paren_cusips = tuple(_PAREN_CUSIP_RE.findall(action))
    return reor_ref, paren_cusips


# rule.name -> CorporateActionProposal.kind. cash_in_lieu is deliberately
# absent: it never becomes a proposal (see ImportBatch.cash_in_lieu).
_KIND_BY_RULE_NAME: dict[str, str] = {
    "reverse_split": "reverse_split",
    "name_change": "name_change",
    "merger": "merger",
    "spinoff_distribution": "spinoff",
    "share_distribution": "split",
}

# Group shapes from spec §5/§1: two legs for a reverse split and a name
# change, three for a merger, one for a spinoff. A group whose row count
# doesn't match its kind's entry here is reported as unrecognised rather
# than coerced into the nearest match (spec §7).
_EXPECTED_LEG_COUNT: dict[str, int] = {
    "reverse_split": 2,
    "name_change": 2,
    "merger": 3,
    "spinoff": 1,
    # One row: the shares received. There is no second leg -- the holding it
    # was received on is in the ledger, not in the file.
    "split": 1,
}


@dataclass(frozen=True, slots=True)
class _CorporateActionRow:
    """One recognised, not-yet-grouped CORPORATE_ACTION row (cash-in-lieu
    excluded -- see ImportBatch.cash_in_lieu). Internal to this module:
    importers/base.py only ever sees the grouped CorporateActionProposal that
    _group_corporate_actions produces from a list of these."""
    line_no: int
    kind: str
    ex_date: date
    quantity: Decimal
    description: str
    symbol: str | None
    row_cusip: str | None  # this row's own entity -- see _PAREN_CUSIP_RE
    group_key: tuple
    # The parent ticker a spinoff row states -- see _SPINOFF_PARENT_RE. None
    # for every other kind, and for a spinoff row that states none.
    parent_symbol: str | None = None
    # The row's own Symbol column, carried through for a share_distribution
    # row -- see CorporateActionProposal.subject_symbol. None for every
    # other kind.
    subject_symbol: str | None = None


def _group_corporate_actions(
    rows: list["_CorporateActionRow"],
) -> tuple[tuple[CorporateActionProposal, ...], tuple[str, ...]]:
    """Group recognised corporate-action rows into proposals on the venue's
    own #REOR reference (spec §5), preserving first-seen order. A row with no
    usable #REOR token (this fixture's spinoff row has none at all) was
    already assigned a (ex-date, CUSIP-pair) fallback key when it was built
    -- see the CORPORATE_ACTION branch in parse().

    A group whose row count doesn't match its kind's expected shape, or
    whose rows disagree on kind (should not happen given RULES, but is
    checked rather than assumed), is reported as unrecognised -- spec §7 —
    and produces no proposal.
    """
    groups: dict[tuple, list[_CorporateActionRow]] = {}
    order: list[tuple] = []
    for row in rows:
        bucket = groups.setdefault(row.group_key, [])
        if not bucket:
            order.append(row.group_key)
        bucket.append(row)

    proposals: list[CorporateActionProposal] = []
    warnings: list[str] = []

    for key in order:
        group_rows = groups[key]
        lines = ", ".join(str(r.line_no) for r in group_rows)
        kinds = {r.kind for r in group_rows}

        if len(kinds) != 1:
            warnings.append(
                f"unrecognised corporate action: lines {lines} share a "
                f"reorganisation reference but disagree on kind "
                f"({sorted(kinds)!r})"
            )
            continue

        kind = next(iter(kinds))
        expected = _EXPECTED_LEG_COUNT[kind]
        if len(group_rows) != expected:
            warnings.append(
                f"unrecognised corporate action: {kind} at lines {lines} has "
                f"{len(group_rows)} row(s), expected {expected}"
            )
            continue

        source_cusip, resulting_cusip = _derive_cusip_pair(group_rows)
        description = " | ".join(r.description for r in group_rows if r.description)
        ratio, ratio_source, approximate, stated_ratio, ratio_warning = _derive_ratio(
            kind, group_rows, description, lines
        )
        if ratio_warning:
            warnings.append(ratio_warning)
        # A spinoff's group is a single row, so "the group's parent" is that
        # row's own stated parent; the other kinds never carry one.
        parent_symbol = next((r.parent_symbol for r in group_rows if r.parent_symbol), None)
        # Likewise, a share distribution's group is a single row -- its own
        # Symbol column, carried the same way parent_symbol is above.
        subject_symbol = next(
            (r.subject_symbol for r in group_rows if r.subject_symbol), None
        )
        proposals.append(
            CorporateActionProposal(
                kind=kind,
                ex_date=group_rows[0].ex_date,
                source_cusip=source_cusip,
                resulting_cusip=resulting_cusip,
                description=description,
                quantities=tuple(r.quantity for r in group_rows),
                ratio=ratio,
                ratio_source=ratio_source,
                approximate=approximate,
                stated_ratio=stated_ratio,
                parent_symbol=parent_symbol,
                subject_symbol=subject_symbol,
                group_ref=key[1] if key[0] == "reor" else None,
            )
        )

    return tuple(proposals), tuple(warnings)


def _derive_cusip_pair(
    group_rows: list["_CorporateActionRow"],
) -> tuple[str | None, str | None]:
    """source_cusip (the entity given up) and resulting_cusip (the entity
    received), read from each row's OWN paren-adjacent cusip token (see
    _PAREN_CUSIP_RE) rather than guessed from FROM/TO English, which this
    fixture's own rows do not use consistently enough to trust on its own.

    Role comes from quantity sign, which is unambiguous regardless of
    wording: strictly negative (shares given up) is source_cusip, strictly
    positive (shares received) is resulting_cusip. A side is only resolved
    when exactly ONE row sits on it -- a merger's two-row positive side (two
    different resulting companies) has no single value to report, so it is
    left None rather than picking one arbitrarily. spec §7: an unresolvable
    identifier is blank, never a guess -- and never backwards.
    """
    negative = [r for r in group_rows if r.quantity < 0]
    positive = [r for r in group_rows if r.quantity > 0]
    source_cusip = negative[0].row_cusip if len(negative) == 1 else None
    resulting_cusip = positive[0].row_cusip if len(positive) == 1 else None
    return source_cusip, resulting_cusip


# Spec §6a: the ratio is also stated in the description text itself, not just
# implied by the paired quantities. "N FOR N" occurs 21 times across the real
# exports. §6a originally also claimed "N:N" occurs 10 times -- that was a
# miscount, corrected after the reviewer checked every match: all 11
# digit:digit occurrences in the real exports are the "Date downloaded
# MM/DD/YYYY HH:MM pm" footer timestamp, not a ratio. Zero are ratios. There
# is therefore no colon form to parse, and a bare digit:digit pattern is
# actively dangerous rather than merely redundant -- it collides with that
# footer and with any time-like text a description happens to contain (e.g.
# "SETTLED AT 02:31 PM" would parse as ratio (2, 31)). A spurious stated
# ratio is worse than none, per spec §6a: agreeing with it certifies a ratio
# nobody stated, disagreeing with it raises a false alarm in the exact
# mechanism this cross-check exists to make trustworthy. So this parses ONLY
# "N FOR N" -- see test_stated_ratio_does_not_mistake_a_clock_time_for_a_ratio
# in tests/test_fidelity_history.py, which pins that a colon never parses.
_STATED_RATIO_FOR_RE = re.compile(r"(\d+)\s*FOR\s*(\d+)", re.IGNORECASE)


def _parse_stated_ratio(text: str) -> tuple[Decimal, Decimal] | None:
    """The ratio as Fidelity's own text states it, new:old -- the same
    direction _derive_quantity_ratio computes from the paired quantities.
    None when the pattern doesn't match (this fixture's name change and
    merger rows state no ratio at all, and -- deliberately -- neither does
    any bare "N:N"; see the comment above _STATED_RATIO_FOR_RE)."""
    match = _STATED_RATIO_FOR_RE.search(text)
    if not match:
        return None
    return Decimal(match.group(1)), Decimal(match.group(2))


def _gcd_decimal(a: Decimal, b: Decimal) -> Decimal:
    """Euclidean GCD over Decimal -- Decimal in, Decimal out, never float,
    and no int() truncation, so this stays exact even for a non-integral
    quantity (none appear in this fixture, but nothing here assumes whole
    shares)."""
    a, b = abs(a), abs(b)
    while b:
        a, b = b, a % b
    return a


def _reduce_ratio(new_qty: Decimal, old_qty: Decimal) -> tuple[Decimal, Decimal]:
    """Reduce a (new, old) pair to the smallest integer pair with the same
    ratio. This ALWAYS "succeeds" mathematically -- a/gcd and b/gcd
    reconstruct a and b exactly by construction -- so a clean reduction here
    is never, by itself, evidence the ratio is right. Only a second,
    independent source (the stated text, spec §6a) can show that -- see
    CorporateActionProposal.approximate's docstring."""
    divisor = _gcd_decimal(new_qty, old_qty)
    if divisor == 0:
        return new_qty, old_qty
    return new_qty / divisor, old_qty / divisor


def _derive_quantity_ratio(
    group_rows: list["_CorporateActionRow"],
) -> tuple[Decimal, Decimal] | None:
    """(new, old), reduced -- the direction adjust_fills consumes, and the
    direction the "1 FOR 3" idiom itself uses (1 new FOR 3 old). Only
    derived when exactly one row sits on each side (2 rows total), the same
    rule _derive_cusip_pair uses to resolve a single entity. Ambiguous stays
    None rather than a guess (spec §7).

    This is why a merger NEVER derives a ratio, structurally rather than
    incidentally (spec §6, corrected): _EXPECTED_LEG_COUNT fixes a merger's
    group at exactly 3 rows, while deriving a ratio here requires exactly 1
    negative row and exactly 1 positive row -- 2 rows total. 3 != 2, so
    those two rules can never both hold for a merger; this branch always
    returns None for one, not only for this fixture's particular two-issuer
    shape (9 shares of one resulting company, 4 of another) where summing
    across issuers would otherwise silently manufacture a ratio out of
    shares of two unrelated securities."""
    negative = [r.quantity for r in group_rows if r.quantity < 0]
    positive = [r.quantity for r in group_rows if r.quantity > 0]
    if len(negative) != 1 or len(positive) != 1:
        return None
    return _reduce_ratio(positive[0], abs(negative[0]))


# ratio_source values, spec §6a: 'constant' (name_change's fixed 1:1),
# 'derived' (quantities only, with NO stated text found to check against),
# 'derived+confirmed' (quantities AND the stated text agree -- spec §6a's
# "strongest evidence available"), 'derived+disputed' (quantities AND a
# stated text that DISAGREES -- the cross-check firing, `approximate` True
# and `stated_ratio` carrying the other candidate), or None (spinoff, or a
# merger, which is structurally never derivable -- see
# _derive_quantity_ratio's docstring). All three 'derived*' values are
# deliberately distinct: collapsing 'derived' and 'derived+confirmed' would
# let a consumer mistake "never checked" for "two sources agreed" (which is
# what the reviewer demonstrated by pointing out that deleting the
# cross-check entirely still produced 'derived' either way), and collapsing
# 'derived' and 'derived+disputed' -- which this code did until the final
# fix wave -- made the consumer print "no independent confirmation was found
# in the venue's own text" on the exact rows where confirmation was found and
# CONTRADICTED the number printed beside that sentence.


def _derive_ratio(
    kind: str,
    group_rows: list["_CorporateActionRow"],
    description: str,
    lines: str,
) -> tuple[
    tuple[Decimal, Decimal] | None, str | None, bool, tuple[Decimal, Decimal] | None, str | None
]:
    """Ratio, its source, whether it's flagged approximate, the ratio the
    venue's own text states (reduced, or None when it states none), and an
    optional warning to surface a stated/derived disagreement -- spec §6 and
    §6a. Returns (ratio, ratio_source, approximate, stated_ratio, warning).

    `stated_ratio` is returned alongside rather than folded into `ratio`
    precisely because the two can disagree: which one is right is not this
    layer's call to make (spec §6a forbids silently preferring a source), and
    a consumer cannot present the choice to a human without both numbers.
    """
    if kind == "name_change":
        return (Decimal(1), Decimal(1)), "constant", False, None, None
    if kind == "spinoff":
        # Not derivable from the file at all -- spec §6, last row: the
        # child shares are here, the parent holding at the ex-date is not.
        # cli.py fills this from the ledger.
        return None, None, False, None, None

    # reverse_split: derive from the paired quantities, then cross-check
    # against the stated text. merger always returns None here -- see
    # _derive_quantity_ratio's docstring for why that is structural, not a
    # gap in this dispatch.
    derived = _derive_quantity_ratio(group_rows)
    if derived is None:
        # Ambiguous shape, or a merger (always -- see
        # _derive_quantity_ratio's docstring) -- blank, never a guess, the
        # same silent-blank precedent _derive_cusip_pair sets for
        # resulting_cusip in this exact merger.
        return None, None, False, None, None

    stated = _parse_stated_ratio(description)
    if stated is None:
        # Only one source available -- record it as such (spec §6a), not as
        # a confirmed match.
        return derived, "derived", False, None, None

    stated_reduced = _reduce_ratio(*stated)
    if stated_reduced == derived:
        # Two independent sources agreeing is the strongest evidence
        # available that this is right (spec §6a) -- recorded distinctly
        # from the "only one source existed" case above.
        return derived, "derived+confirmed", False, stated_reduced, None

    # Disagreement is the signal that matters (spec §6a): a fractional
    # remainder paid out as cash in lieu, or a misparse of one side. Never
    # silently prefer one source -- carry BOTH candidates ('derived+disputed'
    # plus `stated_ratio`), flag it, and say so where a human will see it.
    # `ratio` still holds the quantities-derived pair, which is the evidence
    # this file actually contains; a consumer must not present it as the
    # action's ratio while the disagreement stands (cli.py renders both and
    # asks the human to fill one in).
    warning = (
        f"{kind} at lines {lines}: ratio derived from quantities "
        f"({derived[0]}:{derived[1]}) disagrees with the ratio stated in "
        f"the description ({stated_reduced[0]}:{stated_reduced[1]}) -- "
        "possible cash-in-lieu remainder or misparse"
    )
    return derived, "derived+disputed", True, stated_reduced, warning


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


def _carries_money(raw: str | None) -> bool:
    """True if a raw quantity/amount field is non-zero -- or is present but
    unparseable, which must be treated as "might carry money" rather than
    silently read as empty. Blocking on a false positive costs a human a
    glance; failing open on a garbled money field is exactly the silent-loss
    failure mode this whole task exists to close."""
    try:
        return _decimal(raw) != 0
    except InvalidOperation:
        return True


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


# All 45 date-bearing `as of` tokens across the real exports are ISO
# YYYY-MM-DD; none is the MM/DD/YYYY the Run Date column uses. (The phrase
# occurs 56 times in all; the other 11 are disclaimer prose -- "as of the
# date it ..." -- and carry no date at all, which is why this pattern
# requires the date rather than merely the words.) Strict on purpose -- an
# unparsable as-of date means the row is not netted and keeps blocking (D4),
# which is safer than guessing a format the venue has never emitted.
_AS_OF_RE = re.compile(r"\bAS OF\s+(\d{4}-\d{2}-\d{2})\b")

# Anchored on the two-word phrases, NOT on bare `CXL`/`CORR` tokens. Action
# text carries the security name with its ticker in parentheses, so a bare
# three- or four-letter token can collide with a real ticker and net an
# ordinary trade out of existence. The phrases cannot.
_CANCEL_PHRASE = "CANCELLED TRADE"
_CORRECTION_PHRASE = "CORRECTED CONFIRM"


@dataclass(frozen=True, slots=True)
class _AmendmentPlan:
    """Which rows the amendment pass removes, how it re-dates the ones it
    keeps, and which amendment legs it could NOT place. Keyed by the row's
    line number so the main loop can consult it without re-deriving
    anything."""

    suppressed: frozenset[int]
    redated: dict[int, datetime]
    notes: tuple[str, ...]
    # line_no -> why this CORRECTED CONFIRM row was not netted. A correction
    # leads with "YOU BOUGHT", so the ordinary path for one is the dedicated
    # trade branch, which emits a fill -- meaning an unplaced correction
    # would silently DUPLICATE the original it was supposed to replace,
    # with no warning and nothing blocking. It is the one amendment leg the
    # classifier already "handles", and therefore the only one that fails
    # open. The main loop rejects these instead (D4: degrade to blocking,
    # never to guessing).
    unmatched: dict[int, str]


def _amendment_plan(rows: list[tuple[int, dict[str, str]]]) -> _AmendmentPlan:
    """Net original -> cancel -> correction clusters down to the correction,
    dated to its as-of date (spec D3).

    A complete chain is: an original whose (account, symbol, date,
    |quantity|, price) matches a cancel's as-of tuple; and a correction
    sharing that cancel's (account, symbol, as-of date). Every match must be
    UNIQUE -- an ambiguous one is treated as no match at all.

    **Every key leads with the row's account ref**, and that is load-bearing
    rather than decorative. The Activity & Orders dialect carries an
    `Account Number` column and one real export of it spans five distinct
    accounts; the History dialect has no such column, so `account` is
    uniformly None there and the component is a constant that changes
    nothing. Without it the matcher is account-BLIND, and the failure is
    silent deletion, not a refusal: a cancel in account A whose own original
    lies outside the file's window matches an unrelated genuine trade in
    account B sharing (symbol, date, |quantity|, price), suppresses B's row,
    and hands A a netted fill it never earned. B's fill is not blocked and
    not warned about -- it is simply gone, and (before the fix below the
    suppression check) B vanished from `refs_seen` too, so db/importing.py's
    unregistered-ref net could not see it either. This is the same defect
    family as the two-cancels-one-correction bug: uniqueness inside a bucket
    is not uniqueness across a dimension the key omits. The account was the
    omitted dimension.

    Anything incomplete or ambiguous is left entirely alone, so its rows
    reach the ordinary paths and, being unmapped and money-carrying, block
    (D4). This matcher is fitted to a single real cluster; refusing to act is
    the failure mode it is allowed to have.

    `rows` carries NORMALIZED field names (the same {_normalize_field(k): v}
    mapping the row loop reads), not the raw header spellings -- a real
    export writes "Price ($)", and reading the raw key here would silently
    see every price as absent and match originals to cancels on a shared
    Decimal("0").
    """
    cancels: dict[tuple, list[int]] = {}
    corrections: dict[tuple, list[int]] = {}
    originals: dict[tuple, list[int]] = {}
    # Every cancel line that could claim a given (account, symbol, as-of)
    # correction bucket. Cancels are keyed on the FULL tuple and corrections
    # on (account, symbol, as-of) alone, so two cancels differing only in
    # price are two
    # distinct `cancels` entries that both look up the SAME correction --
    # each seeing len(correction_lines) == 1 and each netting, consuming one
    # correction twice and deleting the second original outright. Uniqueness
    # within a bucket does not imply uniqueness ACROSS the cancels competing
    # for it, which is what this second index makes checkable.
    cancel_claims: dict[tuple, list[int]] = {}

    for line_no, row in rows:
        action = (row.get("action") or "").strip().upper()
        symbol = (row.get("symbol") or "").strip()
        # Read exactly the way parse()'s row loop reads it, including the
        # deliberate refusal to fall back to the nickname column: two
        # accounts can share a nickname, so keying on one would re-open the
        # cross-account collision this leading component exists to close.
        account = (row.get("account number") or "").strip() or None
        try:
            qty = _decimal(row.get("quantity"))
            price = _decimal(row.get("price"))
        except InvalidOperation:
            continue                      # not nettable; the ordinary paths handle it
        if not (qty.is_finite() and price.is_finite()):
            continue

        as_of = _AS_OF_RE.search(action)
        if as_of is None:
            # A candidate ORIGINAL is dated by its own Run Date, and carries
            # no as-of marker at all -- that is what distinguishes it from
            # the two amendment legs.
            try:
                when = datetime.strptime(
                    (row.get("run date") or "").strip(), "%m/%d/%Y"
                ).replace(tzinfo=UTC)
            except ValueError:
                continue
            if action.startswith("YOU BOUGHT") or action.startswith("YOU SOLD"):
                key = (account, symbol, when.date(), abs(qty), price)
                originals.setdefault(key, []).append(line_no)
            continue

        try:
            as_of_date = date.fromisoformat(as_of.group(1))
        except ValueError:
            # _AS_OF_RE validates the SHAPE (four digits, two digits, two
            # digits) but not the CALENDAR -- "AS OF 2026-02-30" matches it
            # and reached date.fromisoformat() unguarded here, raising out of
            # parse() itself: a garbled or corrupted export date crashed the
            # whole import instead of the file merely failing to net (D4).
            # Same shape as the `except ValueError: continue` just above for
            # a bad Run Date on a candidate original -- this row is simply
            # not nettable, and the row loop's own reject() (which every
            # other "recognised shape, unusable value" case in this module
            # routes through) reports it: CANCELLED TRADE/CORRECTED CONFIRM
            # match no rule in RULES, so an unnetted one already falls
            # through to "unhandled action", named with its full text,
            # blocking if it carries money exactly like any other unmapped
            # row would.
            continue
        key = (account, symbol, as_of_date, abs(qty), price)
        if _CANCEL_PHRASE in action:
            cancels.setdefault(key, []).append(line_no)
            cancel_claims.setdefault((account, symbol, as_of_date), []).append(line_no)
        elif _CORRECTION_PHRASE in action:
            # The correction's own quantity and price are the CORRECTED ones,
            # so it is matched on (symbol, as-of) only -- keying it on the
            # full tuple would fail exactly when the correction changed one
            # of those values, which is the case corrections exist for.
            corrections.setdefault((account, symbol, as_of_date), []).append(line_no)

    suppressed: set[int] = set()
    redated: dict[int, datetime] = {}
    notes: list[str] = []

    for key, cancel_lines in cancels.items():
        account, symbol, as_of_date, _qty, _price = key
        original_lines = originals.get(key, [])
        correction_lines = corrections.get((account, symbol, as_of_date), [])
        # Every leg must be UNIQUE, and the correction must be claimed by
        # exactly ONE cancel -- `cancel_claims` is that last condition, and
        # without it two cancels at different prices each net against the
        # same correction (see its comment above). An ambiguous match in any
        # direction is treated as no match at all (D4) -- the rows fall
        # through and block.
        if (
            len(cancel_lines) != 1
            or len(original_lines) != 1
            or len(correction_lines) != 1
            or len(cancel_claims.get((account, symbol, as_of_date), [])) != 1
        ):
            continue
        cancel_line, original_line = cancel_lines[0], original_lines[0]
        correction_line = correction_lines[0]
        suppressed.update({cancel_line, original_line})
        redated[correction_line] = datetime.combine(as_of_date, time.min, tzinfo=UTC)
        # "netted" appears in this message and deliberately NOWHERE else in
        # the module -- the refusal message below is worded to avoid it, so
        # that scanning warnings for the word cannot mistake a refusal for a
        # netting (it did: "not netted" matched, and a test read it as one).
        notes.append(
            f"netted an amendment cluster on {symbol}: lines {original_line} "
            f"(original) and {cancel_line} (cancel) suppressed; line "
            f"{correction_line} (correction) dated to {as_of_date.isoformat()}"
        )

    # Every correction the loop above did not place. See
    # _AmendmentPlan.unmatched for why this one leg cannot simply be left to
    # the ordinary paths the way an unplaced cancel or original can.
    unmatched = {
        line_no: (
            f"CORRECTED CONFIRM as of {as_of_date.isoformat()} on "
            f"{symbol or '(no symbol)'} matched no unique cancelled original "
            "-- the amendment pass declined to act, and this row is not "
            "imported as an ordinary trade, which would duplicate the fill "
            "it was issued to correct"
        )
        for (_account, symbol, as_of_date), correction_lines in corrections.items()
        for line_no in correction_lines
        if line_no not in redated
    }

    return _AmendmentPlan(frozenset(suppressed), redated, tuple(notes), unmatched)


class FidelityImporter:
    venue = "fidelity"
    # Equal to `venue`: see importers/base.py's Importer.account_venue
    # docstring, and CoinbaseImporter's identical comment for why this can't
    # come from a Protocol-level default instead.
    account_venue = "fidelity"

    def parse(self, text: str) -> ImportBatch:
        fills: list[CanonicalFill] = []
        cash: list[CanonicalCash] = []
        transfers: list[CanonicalTransfer] = []
        warnings: list[str] = []
        unmapped: list[str] = []
        # CORPORATE_ACTION rows (cash-in-lieu excluded), collected here and
        # grouped into CorporateActionProposals AFTER the row loop -- a
        # reorganisation's legs can arrive on non-adjacent lines (see the
        # merger's three rows in tests/fixtures/fidelity/real_shape_history.csv),
        # so grouping cannot happen row-by-row the way fills/cash do.
        corporate_action_rows: list[_CorporateActionRow] = []
        # Cash-in-lieu-of-fractional-shares rows -- see ImportBatch.cash_in_lieu
        # for why these are kept out of corporate_action_rows entirely.
        cash_in_lieu: list[str] = []
        # Reasons the whole batch must refuse to commit -- see
        # ImportBatch.blocking's docstring for why this is narrower than
        # "every unmapped row" and wider than "none of them". Each entry is
        # (account, message) -- attributed to the row's own account so a
        # caller can drop reasons belonging to an ignore_on_import account
        # (see ImportBatch.blocking's docstring) without dropping every
        # reason in the file.
        blocking: list[tuple[str | None, str]] = []

        def reject(
            row: dict[str, str],
            raw_row: dict[str, str],
            account: str | None,
            line_no: int,
            message: str,
        ) -> None:
            """ONE path for every row parse() or build_fill drops as
            unmapped -- bad number, zero quantity, non-finite quantity/price/
            fee, bad amount, non-finite amount, and "no rule matched" alike.
            Before this existed, only the "no rule matched" branch consulted
            _carries_money; the other five (build_fill's InvalidOperation/
            zero-quantity/non-finite paths, and the cash branch's
            InvalidOperation/non-finite-amount paths) appended to `unmapped`
            and `warnings` directly and never to `blocking` -- so a row that
            DID match a rule but carried a garbled quantity or amount failed
            open exactly the way an unmatched row used to, before Task 5.
            Routing every such row through this one function is what stops
            that asymmetry from recurring the next time a new guard is added.
            """
            warnings.append(message)
            unmapped.append(str(raw_row))
            if _carries_money(row.get("quantity")) or _carries_money(row.get("amount")):
                blocking.append((account, message))

        # Every account ref seen in the raw rows, independent of whether the
        # row went on to become a fill/cash movement or fell out as
        # unmapped -- see ImportBatch.refs_seen's docstring for why this
        # can't be derived from fills/cash after the fact.
        refs_seen: set[str] = set()

        def build_fill(
            row: dict[str, str],
            raw_row: dict[str, str],
            line_no: int,
            symbol: str,
            when: datetime,
            account: str | None,
            side: Side,
            funding_source: str,
        ) -> None:
            """Shared by the dedicated YOU BOUGHT/YOU SOLD branch and the
            reinvest_security rule — both produce a CanonicalFill from the
            same quantity/price/fee columns, differing only in side and
            funding_source."""
            try:
                raw_qty = _decimal(row.get("quantity"))
                price = _decimal(row.get("price"))
                fee = _decimal(row.get("commission")) + _decimal(row.get("fees"))
            except InvalidOperation as exc:
                reject(row, raw_row, account, line_no, f"line {line_no}: bad number ({exc})")
                return

            if raw_qty == 0:
                reject(row, raw_row, account, line_no, f"line {line_no}: zero quantity, skipped")
                return

            # Decimal("NaN")/Decimal("Infinity") are valid constructions, so they are
            # not caught by the `except InvalidOperation` above. Left unchecked,
            # Infinity survives Fill.__post_init__'s `quantity > 0` check and the
            # DB's `quantity > 0` CHECK, becoming a live allocation in group_fills.
            # fee is included too: Fill.__post_init__ never validates fee, and
            # Postgres NUMERIC (PG14+) accepts Infinity, so nothing else catches it.
            if not raw_qty.is_finite() or not price.is_finite() or not fee.is_finite():
                reject(
                    row, raw_row, account, line_no, f"line {line_no}: non-finite number, skipped"
                )
                return

            # Real quantity at zero price is almost always a parse failure
            # (see importers.base.zero_price_warning's docstring), not a free
            # trade -- report it, but still record the fill: suppressing it
            # would trade one silent-loss failure mode for another.
            warn = zero_price_warning(line_no, symbol, abs(raw_qty), price)
            if warn is not None:
                warnings.append(warn)

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
                    side=side,
                    quantity=abs(raw_qty),
                    price=price,
                    fee=fee,
                    fee_currency="USD",
                    external_ref=account,
                    funding_source=funding_source,
                )
            )

        def build_expiry_fill(
            row: dict[str, str],
            raw_row: dict[str, str],
            line_no: int,
            symbol: str,
            account: str | None,
        ) -> None:
            """An option that expired worthless: close the position at zero.

            Deliberately NOT routed through build_fill. build_fill reads
            `price` from the row and runs zero_price_warning on the result;
            this path never reads `price` at all. Giving build_fill a price
            override would make the guard bypassable from any future call
            site, which is the opposite of what importers.base's
            zero_price_warning docstring asks for. The near-duplication of
            the quantity checks below is the price of keeping the guard
            unreachable from here, and is deliberate.
            """
            instrument = parse_option_symbol(symbol)
            if instrument is None:
                reject(
                    row,
                    raw_row,
                    account,
                    line_no,
                    f"line {line_no}: expiry with no parsable option symbol "
                    f"({symbol!r}), skipped",
                )
                return

            try:
                raw_qty = _decimal(row.get("quantity"))
            except InvalidOperation as exc:
                reject(row, raw_row, account, line_no, f"line {line_no}: bad number ({exc})")
                return

            # NaN == 0 is False, so the finiteness test must be part of the
            # same guard rather than a later one.
            if not raw_qty.is_finite() or raw_qty == 0:
                reject(
                    row,
                    raw_row,
                    account,
                    line_no,
                    f"line {line_no}: expiry with zero or non-finite quantity, skipped",
                )
                return

            # The row describes the POSITION being removed, not a trade
            # direction -- there is no verb here to read a side from. A short
            # (negative) position is closed by buying it back, a long one by
            # selling it.
            side = Side.BUY if raw_qty < 0 else Side.SELL

            # The option's own expiry, NOT `Run Date`. In the real export
            # this fix was built from, Fidelity booked a Friday expiry the
            # following Monday, three days later. The fixture below reuses
            # that three-day gap (11/21/2026 expiry, 11/24/2026 Run Date) for
            # arithmetic clarity, not because those fall on a Friday/Monday
            # -- 2026-11-21 is a Saturday.
            # The expiry is the TRUE event date -- the position ceased to
            # exist on it -- and `expiry` sits inside instrument_natural_key,
            # so this is the same value that mints the instrument and cannot
            # disagree with it. Midnight UTC matches the date-only convention
            # the Run Date branch uses.
            #
            # What this does NOT do is change any drift `reconcile` reports
            # today, and an earlier version of this comment claimed it did.
            # `reconcile` has no window to see: open_positions
            # (db/positions.py) takes no `as_of` and has no date filter of
            # any kind -- its predicate is `t.status = 'open'` plus an
            # optional account scope -- so `--as-of` selects which STATEMENT
            # to compare against and never which positions -- cmd_reconcile's
            # own comments say so (cli.py). Once both fills are imported the
            # trade is closed either way, whether the close is dated Nov 21
            # or Nov 24. The phantom-open-across-a-statement-date problem is
            # what dating from the symbol PREVENTS once position
            # reconstruction becomes as-of aware (gap #29 in
            # docs/known-gaps.md, which is what would make the ledger side
            # honour a cutoff): only then does the three-day gap become
            # visible, as a short that a statement dated inside the window
            # would show as already gone.
            when = datetime(
                instrument.expiry.year,
                instrument.expiry.month,
                instrument.expiry.day,
                tzinfo=UTC,
            )

            fills.append(
                CanonicalFill(
                    instrument=instrument,
                    executed_at=when,
                    side=side,
                    quantity=abs(raw_qty),
                    price=Decimal(0),
                    fee=Decimal(0),
                    fee_currency="USD",
                    external_ref=account,
                    funding_source="external",
                )
            )

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
        # Materialised, not streamed. The amendment pass (_amendment_plan)
        # has to see every row before the first one is turned into a fill:
        # a correction row leads with "YOU BOUGHT", so the dedicated branch
        # below matches it on leading text and emits a fill regardless of
        # what classify() thinks -- the decision to suppress its cancelled
        # siblings cannot be made row-by-row. Real exports are a few
        # thousand rows; holding them is not a memory concern.
        #
        # Normalize header casing once, here rather than inside the loop — a
        # real export's header is found case-insensitively (above), so the
        # fields must be read the same way or a differently-cased header
        # parses to zero usable rows, and _amendment_plan reads the same
        # normalized names the loop does.
        materialised: list[tuple[int, dict[str, str], dict[str, str]]] = [
            (line_no, raw_row, {_normalize_field(k): v for k, v in raw_row.items()})
            for line_no, raw_row in enumerate(reader, start=preamble_offset + 2)
        ]
        plan = _amendment_plan([(line_no, row) for line_no, _raw, row in materialised])
        # §6: a netting that happens silently is indistinguishable from rows
        # being dropped, so every one of them is reported.
        warnings.extend(plan.notes)

        for line_no, raw_row, row in materialised:
            action = (row.get("action") or "").strip().upper()
            symbol = (row.get("symbol") or "").strip()
            # external_ref MUST be the account NUMBER, never the nickname
            # ("Account" in a real export): the nickname is neither stable
            # nor unique (two accounts can share one), so routing on it
            # would silently merge or misroute rows. No fallback to the
            # nickname column when the number is absent -- unroutable (None)
            # is the honest outcome, not a guess.
            account = (row.get("account number") or "").strip() or None
            if account:
                refs_seen.add(account)

            if line_no in plan.suppressed:
                # A cancelled original, or the cancel that reversed it.
                # The pair nets to nothing and the correction row (which is
                # NOT suppressed) carries the surviving truth -- recording
                # either of them alongside it double-counts one trade.
                #
                # This `continue` sits BELOW refs_seen deliberately.
                # ImportBatch.refs_seen's docstring makes an unconditional
                # claim -- every account ref in the RAW rows, "whether or
                # not the row went on to become a fill or cash movement" --
                # and skipping before the add quietly made it conditional.
                # It is also the one safety net (db/importing.py's
                # unregistered-ref check) that would surface an account
                # whose rows were ALL suppressed, which is exactly the shape
                # an account-blind matcher used to produce.
                continue

            try:
                when = datetime.strptime((row.get("run date") or "").strip(), "%m/%d/%Y").replace(
                    tzinfo=UTC
                )
            except ValueError as exc:
                # Finding A: this branch used to append straight to
                # unmapped/warnings and never route through reject(), on the
                # reasoning that "no rule can have matched yet, so there is
                # nothing to be inconsistent with." That reasoning is about
                # RULE consistency and says nothing about MONEY loss -- a row
                # whose date fails to parse can still carry a real dollar
                # figure in Amount, and dropping it with only a warning is
                # the exact silent-loss shape reject() exists to close for
                # every other path. reject() reads row.get("quantity")/
                # row.get("amount") directly from the raw string fields, so
                # the money determination here does not depend on `when`
                # ever having been computed -- it can't be, since the date is
                # precisely what failed.
                reject(row, raw_row, account, line_no, f"line {line_no}: bad date ({exc})")
                continue

            # The surviving leg of a netted cluster is dated to the cluster's
            # as-of date, not to the Run Date on which the venue re-booked
            # it (spec D3): the trade happened on the as-of date, and the
            # correction's own Run Date is nineteen days later in the real
            # cluster this was built from. Rows outside a netting keep their
            # Run Date, which is what `when` already holds.
            when = plan.redated.get(line_no, when)

            # A CORRECTED CONFIRM row the amendment pass could not place.
            # Unlike an unplaced cancel (which no rule matches, so it falls
            # through to the unmapped path and blocks on its own), a
            # correction leads with "YOU BOUGHT" and would be taken by the
            # dedicated trade branch below -- emitting a second fill for the
            # trade it exists to RESTATE, with nothing said. Routed through
            # reject() rather than appended to `blocking` directly, so it
            # shares the one path every other dropped row uses (see
            # reject()'s docstring): a correction carries a quantity, so in
            # practice this always blocks, but a hypothetical one carrying
            # no money warns instead of refusing, exactly as every other
            # unmapped row does.
            unmatched_reason = plan.unmatched.get(line_no)
            if unmatched_reason is not None:
                reject(row, raw_row, account, line_no, f"line {line_no}: {unmatched_reason}")
                continue

            # YOU BOUGHT / YOU SOLD keep their own dedicated branch: direction
            # comes from the action text and the sign is corroboration, which
            # the rule table below does not (and should not) model.
            #
            # Anchored on the leading "YOU BOUGHT"/"YOU SOLD" verb via
            # startswith, NOT a bare "BOUGHT"/"SOLD" substring scan. This
            # task's whole premise is that the security NAME is concatenated
            # into the same action field (see classify()) -- a bare substring
            # scan would let a name like "SOLDIERS FIELD CAP" hijack a
            # dividend row as a phantom sell.
            if action.startswith("YOU BOUGHT") or action.startswith("YOU SOLD"):
                build_fill(
                    row,
                    raw_row,
                    line_no,
                    symbol,
                    when,
                    account,
                    side=Side.SELL if action.startswith("YOU SOLD") else Side.BUY,
                    funding_source="external",
                )
                continue

            # Staleness guard: a REINVESTMENT row is the only place a
            # per-unit price is meaningfully comparable to a sweep's $1.00
            # NAV. Runs independent of which rule matches below (or whether
            # any does) -- never suppress the row, warn and continue.
            if action.startswith("REINVESTMENT"):
                par_warning = _sweep_par_warning(symbol, row.get("price"))
                if par_warning is not None:
                    warnings.append(par_warning)

            rule = classify(action, symbol)
            if rule is None:
                # A row the classifier doesn't recognise is guaranteed --
                # the venue's action vocabulary is open-ended, and a real
                # export's trailing legal disclaimer is permanently unmapped
                # by design. Blocking on every such row is unworkable;
                # blocking on none of them is exactly how the silent-zero
                # defect looked like success. So: only a row that ALSO
                # carries money (a non-zero quantity or amount) refuses the
                # commit -- one with no financial content only warns. See
                # reject()'s docstring for why this now shares one path with
                # every other unmapped branch instead of being the only one
                # that consulted _carries_money.
                reject(
                    row,
                    raw_row,
                    account,
                    line_no,
                    f"line {line_no}: unhandled action {action!r}",
                )
                continue

            if rule.outcome is Outcome.INTERNAL:
                # The offsetting leg of an event already recorded elsewhere
                # (e.g. a sweep-fund reinvestment of a dividend that was
                # already counted as cash in). Recording it too would count
                # the money twice — see Outcome.INTERNAL's docstring.
                continue

            if rule.outcome is Outcome.FILL:
                # A DRIP into a real security: a genuine acquisition with
                # real cost basis, funded by the position's own distribution
                # rather than external capital.
                build_fill(
                    row,
                    raw_row,
                    line_no,
                    symbol,
                    when,
                    account,
                    side=rule.side,
                    funding_source=rule.funding_source,
                )
                continue

            if rule.outcome is Outcome.EXPIRY:
                build_expiry_fill(row, raw_row, line_no, symbol, account)
                continue

            if rule.outcome is Outcome.UNSUPPORTED:
                message = (
                    f"line {line_no}: {action.split()[0]} is recognised but not "
                    "supported; import refuses rather than guessing at the "
                    "resulting stock leg"
                )
                warnings.append(message)
                unmapped.append(str(raw_row))
                blocking.append((account, message))
                continue

            if rule.outcome is Outcome.CORPORATE_ACTION:
                # Recognised and DEFERRED, not recorded and not refused --
                # see Outcome.CORPORATE_ACTION's docstring for why this is
                # neither INTERNAL (no follow-up) nor UNSUPPORTED (blocks
                # unconditionally). Nothing is appended to `fills`, `cash`,
                # `unmapped`, or `blocking`: the row is fully accounted for
                # by being recognised, not by being reported -- except that
                # it is collected here for grouping (or, for cash-in-lieu,
                # for its own separate report) after the row loop.
                description = (row.get("description") or "").strip()

                if rule.name == "cash_in_lieu":
                    # Recognised, reported separately, never applied (spec
                    # §7, D6) -- see ImportBatch.cash_in_lieu's docstring.
                    cash_in_lieu.append(description or action)
                    continue

                try:
                    quantity = _decimal(row.get("quantity"))
                except InvalidOperation as exc:
                    # Does not block (Outcome.CORPORATE_ACTION never does),
                    # but silently dropping the row would leave a merger or
                    # split one leg short with nothing saying why -- warn and
                    # exclude it from grouping, same "warn rather than fail
                    # open OR closed" shape as every other guard in this
                    # file.
                    warnings.append(
                        f"line {line_no}: corporate action has unparsable "
                        f"quantity ({exc}), excluded from grouping"
                    )
                    continue
                # Decimal("NaN")/Decimal("Infinity") CONSTRUCT fine and slip
                # past the `except InvalidOperation` above -- the same hazard
                # the fill and cash branches each guard (see the non-finite
                # amount check below, and migration
                # 002_reject_non_finite_numerics.sql). Without this, a NaN
                # quantity reaches _derive_cusip_pair/_derive_quantity_ratio,
                # whose `< 0`/`> 0` comparisons raise InvalidOperation out of
                # parse() itself -- a crash, where spec §7's posture is
                # degrade and report. Warn and exclude the row from grouping,
                # exactly as the unparsable case above does.
                if not quantity.is_finite():
                    warnings.append(
                        f"line {line_no}: corporate action has non-finite "
                        f"quantity ({quantity}), excluded from grouping"
                    )
                    continue

                # D5: only a positive quantity makes a plain DISTRIBUTION a
                # SHARE distribution. A zero-quantity one has never been
                # observed; treat it as unmapped so it blocks and is looked
                # at, rather than proposing a split derived from no shares.
                if rule.name == "share_distribution" and quantity <= 0:
                    reject(
                        row,
                        raw_row,
                        account,
                        line_no,
                        f"line {line_no}: DISTRIBUTION with no positive quantity "
                        f"({quantity}) -- not a share distribution; see gap for D5",
                    )
                    continue

                reor_ref, paren_cusips = _parse_reor(action)
                if reor_ref:
                    group_key = ("reor", _reor_base(reor_ref))
                else:
                    # Fallback per spec §5: (ex-date, CUSIP pair). No row in
                    # this fixture's real_shape_history.csv without a #REOR
                    # token has a cusip-shaped token either (the spinoff row
                    # has neither), so the symbol is included as a last
                    # resort to keep such rows from all colliding onto one
                    # (ex_date, ()) key if more than one ever appears on the
                    # same date.
                    cusip_tuple = tuple(sorted(set(paren_cusips)))
                    group_key = ("fallback", when.date(), cusip_tuple or (symbol or "",))

                # This row's own entity -- see _PAREN_CUSIP_RE's comment for
                # why it is the paren token and not the token before #REOR.
                # None when the row carries zero or more than one
                # cusip-shaped paren token (ambiguous either way; honest
                # absence rather than picking one).
                row_cusip = paren_cusips[0] if len(paren_cusips) == 1 else None

                kind = _KIND_BY_RULE_NAME[rule.name]
                # Only a spinoff row states its parent (see
                # _SPINOFF_PARENT_RE); asking for one anywhere else would
                # read a "FROM:(...)" that means something different.
                parent_symbol = _parse_spinoff_parent(action) if kind == "spinoff" else None
                # Only a share_distribution row's subject is its OWN Symbol
                # column -- see CorporateActionProposal.subject_symbol.
                subject_symbol = symbol or None if rule.name == "share_distribution" else None

                corporate_action_rows.append(
                    _CorporateActionRow(
                        line_no=line_no,
                        kind=kind,
                        ex_date=when.date(),
                        quantity=quantity,
                        description=description,
                        symbol=symbol or None,
                        row_cusip=row_cusip,
                        group_key=group_key,
                        parent_symbol=parent_symbol,
                        subject_symbol=subject_symbol,
                    )
                )
                continue

            if rule.outcome is Outcome.TRANSFER:
                try:
                    quantity = _decimal(row.get("quantity"))
                    amount = _decimal(row.get("amount"))
                except InvalidOperation as exc:
                    reject(row, raw_row, account, line_no, f"line {line_no}: bad number ({exc})")
                    continue
                if not quantity.is_finite() or not amount.is_finite():
                    reject(
                        row,
                        raw_row,
                        account,
                        line_no,
                        f"line {line_no}: non-finite number, skipped",
                    )
                    continue

                if symbol and quantity < 0 and amount <= 0:
                    # Shares delivered out. Amount is the broker's market-value
                    # stamp, NOT a transaction price -- it rides along as
                    # information and never touches P&L (the position closes at
                    # average cost; see ledger/pnl.py).
                    instrument = parse_option_symbol(symbol) or Instrument(
                        id=None,
                        asset_class=AssetClass.EQUITY,
                        symbol=symbol.upper(),
                        quote_currency="USD",
                    )
                    transfers.append(
                        CanonicalTransfer(
                            instrument=instrument,
                            occurred_at=when,
                            quantity=abs(quantity),
                            market_value=abs(amount) or None,
                            external_ref=account,
                            note=(row.get("description") or "").strip() or None,
                        )
                    )
                    continue

                if not symbol and quantity == 0 and amount < 0:
                    # The residual cash leg. Canonical amounts are positive;
                    # direction lives in the kind (OUTFLOW_KINDS).
                    cash.append(
                        CanonicalCash(
                            occurred_at=when,
                            kind="transfer_out",
                            amount=abs(amount),
                            currency="USD",
                            symbol=None,
                            external_ref=account,
                            note=(row.get("description") or "").strip() or None,
                        )
                    )
                    continue

                if quantity == 0 and amount == 0:
                    # Moves nothing -- unmapped-but-harmless, the same policy
                    # _carries_money enforces everywhere else. Warn, never
                    # refuse: blocking a no-money memo row with an
                    # "inbound-shaped" diagnosis would be both unactionable
                    # and wrong.
                    warnings.append(
                        f"line {line_no}: TRANSFER OF ASSETS row carries no "
                        "quantity and no amount; recorded nothing"
                    )
                    unmapped.append(str(raw_row))
                    continue

                message = (
                    f"line {line_no}: TRANSFER OF ASSETS row is not an outbound "
                    "delivery -- an inbound transfer arrives with basis this "
                    "ledger has no source for (spec D2/D6); refusing the file"
                )
                warnings.append(message)
                unmapped.append(str(raw_row))
                blocking.append((account, message))
                continue

            if rule.outcome is not Outcome.CASH:
                raise AssertionError(f"unhandled rule outcome {rule.outcome!r}")
            try:
                amount = _decimal(row.get("amount"))
            except InvalidOperation as exc:
                reject(row, raw_row, account, line_no, f"line {line_no}: bad amount ({exc})")
                continue
            # Decimal("Infinity")/Decimal("NaN") are valid constructions and slip
            # past the `except InvalidOperation` above (same hazard as quantity/
            # price above); cash_movement.amount has no CHECK constraint to catch
            # one downstream.
            if not amount.is_finite():
                reject(
                    row, raw_row, account, line_no, f"line {line_no}: non-finite amount, skipped"
                )
                continue
            # Canonical convention (see importers.base.OUTFLOW_KINDS): amount is
            # always positive, direction lives in `kind` alone. Fidelity's raw
            # Amount column is signed (negative for a withdrawal/purchase-style
            # outflow, e.g. "ELECTRONIC FUNDS TRANSFER PAID" exports -2000.00),
            # so this abs() is load-bearing here, unlike Coinbase's twin where
            # the raw export is already positive.
            amount = abs(amount)
            # C2: cash rows had no equivalent of the fill branch's
            # zero_price_warning -- a missing/misnamed Amount column
            # silently produced a $0.00 cash movement with no warning at
            # all. Warn, but still record it: suppressing it would trade one
            # silent-loss failure mode for another, same reasoning as
            # zero_price_warning on the fill side.
            warn = zero_amount_warning(line_no, rule.cash_kind, amount)
            if warn is not None:
                warnings.append(warn)
            cash.append(
                CanonicalCash(
                    occurred_at=when,
                    kind=rule.cash_kind,
                    amount=amount,
                    currency="USD",
                    symbol=symbol or None,
                    external_ref=account,
                    note=(row.get("description") or "").strip() or None,
                )
            )

        corporate_actions, group_warnings = _group_corporate_actions(corporate_action_rows)
        warnings.extend(group_warnings)

        return ImportBatch(
            fills=tuple(fills),
            cash=tuple(cash),
            transfers=tuple(transfers),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
            refs_seen=tuple(sorted(refs_seen)),
            blocking=tuple(blocking),
            corporate_actions=corporate_actions,
            cash_in_lieu=tuple(cash_in_lieu),
        )
