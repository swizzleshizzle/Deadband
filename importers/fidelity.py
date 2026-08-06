"""Fidelity account-activity CSV → canonical rows. Pure — no I/O, no clock."""

from __future__ import annotations

import csv
import enum
import io
import re
from dataclasses import dataclass
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


class FidelityImporter:
    venue = "fidelity"

    def parse(self, text: str) -> ImportBatch:
        fills: list[CanonicalFill] = []
        cash: list[CanonicalCash] = []
        warnings: list[str] = []
        unmapped: list[str] = []
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
            # external_ref MUST be the account NUMBER, never the nickname
            # ("Account" in a real export): the nickname is neither stable
            # nor unique (two accounts can share one), so routing on it
            # would silently merge or misroute rows. No fallback to the
            # nickname column when the number is absent -- unroutable (None)
            # is the honest outcome, not a guess.
            account = (row.get("account number") or "").strip() or None
            if account:
                refs_seen.add(account)

            try:
                when = datetime.strptime((row.get("run date") or "").strip(), "%m/%d/%Y").replace(
                    tzinfo=UTC
                )
            except ValueError as exc:
                warnings.append(f"line {line_no}: bad date ({exc})")
                unmapped.append(str(raw_row))
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

            # rule.outcome is Outcome.CASH
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

        return ImportBatch(
            fills=tuple(fills),
            cash=tuple(cash),
            warnings=tuple(warnings),
            unmapped_rows=tuple(unmapped),
            refs_seen=tuple(sorted(refs_seen)),
            blocking=tuple(blocking),
        )
