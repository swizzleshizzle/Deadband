"""The A-2 §9 acceptance-bar fixture: an anonymized file with a REAL export's shapes.

Why this module exists, separately from test_fidelity.py
-------------------------------------------------------
`tests/fixtures/fidelity/activity.csv` is hand-written. It was written from the
venue's *documentation* rather than from a file the venue actually emits, and
the difference is not cosmetic: part 2a's importer passed 244 tests against it
while mapping 19% of a real export and silently valuing the rest at zero. Every
real-world shape that gap consisted of is now reconstructed ad hoc inside
individual tests in test_fidelity.py, which means no single artifact represents
the acceptance bar -- and a branch can pass every per-task review while not
importing the owner's actual file.

`real_shape_activity.csv` is that artifact. Its numbers, symbols, account
nicknames and account numbers are fabricated; its *shapes* are copied from a
real export, row for row:

  * A UTF-8 BOM, then two blank preamble lines -- the header is line 3.
  * Fourteen columns, not ten, with `($)` suffixed onto every money column and
    `Price ($)` ordered BEFORE `Quantity`. `Account`, `Type`,
    `Accrued Interest ($)` and `Settlement Date` do not exist in the
    hand-written fixture at all.
  * Compound action text: the verb, then the security's name, then its ticker
    in parentheses, then a `(Cash)` suffix -- so an action field is prose, and
    a bare "BOUGHT"/"SOLD" substring scan would hijack unrelated rows.
  * `REINVESTMENT as of <date> ...` -- the back-dated reinvestment variant.
  * An option symbol quoted WITH A LEADING SPACE (`" -ZXCO280121C100"`).
  * TWO dialects. Brokerage rows write an empty field as `""` and set `Type`;
    employer-plan rows write it bare, use Title-case verbs (`Contributions`,
    `Investment Gain/Loss`), leave `Symbol` EMPTY with the fund named only in
    `Description`, and carry a short unprefixed account number. A guard written
    against one dialect and not its twin is this project's named recurring
    defect; having both in one file is what makes that mechanical to catch.
  * A trailing legal disclaimer block, including the venue's own inconsistency
    (`Fees($)` with no space, against the header's `Fees ($)`) that is the
    reason header normalization is structural rather than an alias table.
  * A final `Date downloaded ...` line with no trailing newline.

Real exports never enter this repository -- see the public-repo-hygiene skill.

Two tests here pin CURRENT behaviour that is KNOWN WRONG, and say so: an
employer-plan gain/loss row blocks every commit, and plan unit quantities are
discarded. They are written as assertions rather than xfails so that changing
the behaviour forces a deliberate edit here, with the gap's resolution recorded
in docs/known-gaps.md.
"""

import csv
import io
import pathlib
import re
from decimal import Decimal

from importers.fidelity import RULES, FidelityImporter, Outcome, classify
from ledger.types import AssetClass, Side

# Anchored to this file's location, never the process cwd -- same hazard as
# test_fidelity.py's FIXTURE path.
_FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
_FIXTURE_PATH = _FIXTURES_DIR / "fidelity" / "real_shape_activity.csv"
FIXTURE = _FIXTURE_PATH.read_text(encoding="utf-8")

# The account refs the fixture is BUILT from. Asserted rather than derived so
# that a real account number pasted into this file fails a test as well as the
# pre-commit hook -- two independent guards, since the hook's deny-list is
# gitignored and therefore absent from a fresh clone.
SYNTHETIC_REFS = frozenset({"X23456789", "112233445", "556677889", "90210"})

# Every dated row must resolve to exactly one of these. See
# test_no_dated_row_disappears_without_a_trace.
_DATED_ROWS = 23


def batch():
    return FidelityImporter().parse(FIXTURE)


# A FULL date, not a "starts with digits" test. The first cut of this helper
# used `[:2].isdigit()` and silently counted the disclaimer block's bare
# document-id line ("9900001.1.0") as a 24th data row -- the same
# accept-anything-shaped laxity, in the test's own row counter, that the
# importer's date handling exists to refuse.
_RUN_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _data_rows():
    """The fixture's dated rows, read the way the importer reads them."""
    text = FIXTURE.lstrip("﻿")
    lines = text.splitlines()
    header_idx = next(i for i, line in enumerate(lines) if line.startswith("Run Date,"))
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    return [r for r in reader if _RUN_DATE_RE.match((r.get("Run Date") or "").strip())]


# --- The file's shape, before any parsing question ------------------------


def test_header_carries_the_columns_the_venue_actually_emits():
    """Pins the four columns and the `($)` suffixes the hand-written fixture
    lacks. `Price ($)` precedes `Quantity` here and follows it there -- the
    importer reads by NAME, and this is what proves that rather than assuming
    it."""
    lines = FIXTURE.lstrip("﻿").splitlines()
    assert lines[0] == ""
    assert lines[1] == ""
    header = lines[2].split(",")
    assert header == [
        "Run Date",
        "Account",
        "Account Number",
        "Action",
        "Symbol",
        "Description",
        "Type",
        "Price ($)",
        "Quantity",
        "Commission ($)",
        "Fees ($)",
        "Accrued Interest ($)",
        "Amount ($)",
        "Settlement Date",
    ]
    assert header.index("Price ($)") < header.index("Quantity")


def test_bom_is_present_and_the_bom_strip_is_what_saves_the_headerless_case():
    """A BOM makes csv.DictReader name the first field '﻿Run Date', failing
    every row.

    The first version of this test asserted only that the fixture starts with
    a BOM and still parses -- and SURVIVED a mutant that deleted the BOM strip
    entirely. In a real export the BOM lands on a blank preamble line, two
    lines above the header, so it never reaches csv.DictReader and the strip is
    genuinely unreachable via the fixture as-shipped. The test could not fail:
    exactly the 'assertion that cannot fail' shape this project keeps hitting.

    So both facts are asserted, and the second one reaches the code: the BOM is
    present in the artifact, AND with the preamble removed -- putting the BOM
    directly against the header, which is what the strip exists for -- the file
    still parses."""
    assert FIXTURE.startswith("﻿")
    assert len(batch().fills) > 0

    without_preamble = "﻿" + "\n".join(FIXTURE.lstrip("﻿").splitlines()[2:])
    assert not without_preamble.lstrip("﻿").startswith("\n")
    stripped_batch = FidelityImporter().parse(without_preamble)
    assert len(stripped_batch.fills) == len(batch().fills)
    assert len(stripped_batch.cash) == len(batch().cash)


def test_only_synthetic_account_refs_appear():
    """Anti-drift guard: fails if a real account number is ever pasted in."""
    assert set(batch().refs_seen) == SYNTHETIC_REFS


# --- The §9 requirement: every rule exercised by a real-shape row ---------


def test_every_rule_is_exercised_by_a_fixture_row():
    """§9: 'Every rule in the table must be exercised by at least one fixture
    row.' test_fidelity.py's twin asserts this against hand-written action
    STRINGS; this asserts it against rows of an actual file, so a rule that
    only matches text nobody's export contains is no longer coverage.

    Classification is checked directly rather than through parse() because two
    rules resolve to INTERNAL and deliberately produce no output -- they are
    invisible to any assertion made on fills or cash."""
    matched = set()
    for row in _data_rows():
        rule = classify(row["Action"], row["Symbol"])
        if rule is not None:
            matched.add(rule.name)
    assert {r.name for r in RULES} - matched == set()


def test_no_dated_row_disappears_without_a_trace():
    """The invariant the silent-zero defect violated: a row carrying a valid
    date resolves to a fill, a cash movement, a deliberate INTERNAL, or a
    REPORTED rejection -- never to nothing at all.

    The first version of this test summed those four counts and asserted
    `>= _DATED_ROWS`. That could not fail. `unmapped_rows` also holds the nine
    disclaimer lines, so the total was 32 against a bar of 23 and stayed above
    it no matter how many dated rows the importer silently dropped -- an
    assertion whose slack was larger than the defect it was watching for.

    Two things fix it. The count is EXACT, so a dropped row breaks it in
    either direction; and `unmapped_rows` is filtered to entries that carry a
    real date, so the disclaimer block cannot pad the total."""
    b = batch()
    rows = _data_rows()
    assert len(rows) == _DATED_ROWS

    internal = sum(
        1
        for row in rows
        if (rule := classify(row["Action"], row["Symbol"])) is not None
        and rule.outcome is Outcome.INTERNAL
    )
    # unmapped_rows entries are str(raw_row) -- a stringified dict, so the
    # match is ANCHORED to the 'Run Date' field holding a date and nothing
    # else. A bare `\d{2}/\d{2}/\d{4}` search anywhere in the string was tried
    # first and over-counted: the footer's "Date downloaded 09/12/2026 02:30
    # pm" lands in the Run Date field and contains a date, so a disclaimer
    # line was counted as a dated row. Same accept-anything-shaped laxity as
    # _RUN_DATE_RE's, found only because the count above is exact.
    dated_unmapped = sum(
        1 for u in b.unmapped_rows if re.search(r"'Run Date': '\d{2}/\d{2}/\d{4}'", u)
    )

    assert len(b.fills) + len(b.cash) + internal + dated_unmapped == _DATED_ROWS
    # And the padding really is excluded: the disclaimer block is still
    # reported, just not counted here.
    assert len(b.unmapped_rows) > dated_unmapped


# --- Shapes the hand-written fixture does not have ------------------------


def test_option_symbol_survives_its_leading_space():
    """The venue quotes an option symbol with a leading space (`" -ZXCO..."`),
    a shape the hand-written fixture has nowhere.

    What this gates, established by mutation rather than assumed: TWO strips
    defend this row -- parse()'s call site and `parse_option_symbol`'s own --
    and they are mutually redundant. Removing either one alone leaves this
    test GREEN; removing BOTH turns it red. So no single-mutant gate can prove
    this test's worth, and claiming one strip as "the" load-bearing line would
    be an attribution nobody checked.

    The real export's only whitespace-padded symbol is this option row, so no
    real-shape input can separate them. Manufacturing a leading-space EQUITY
    row to force the issue was rejected deliberately: inventing a shape the
    venue does not emit is what made the hand-written fixture untrustworthy in
    the first place."""
    option = next(f for f in batch().fills if f.instrument.asset_class is AssetClass.OPTION)
    assert option.instrument.symbol == "-ZXCO280121C100"
    assert option.instrument.underlying == "ZXCO"
    assert option.instrument.strike == Decimal("100")
    assert option.instrument.option_right == "call"
    assert option.quantity == Decimal("4")
    assert option.price == Decimal("0.75")
    # Commission 2.6 + fees 0.05, both read from `($)`-suffixed columns.
    assert option.fee == Decimal("2.65")


def test_compound_action_text_does_not_hijack_the_trade_branch():
    """Every action here is prose containing a security's name. Exactly the
    three intended rows become externally-funded fills; nothing else is
    dragged into the YOU BOUGHT/YOU SOLD branch by a substring."""
    external = [f for f in batch().fills if f.funding_source == "external"]
    assert len(external) == 3
    assert [f.side for f in external] == [Side.BUY, Side.BUY, Side.SELL]


def test_signed_quantity_becomes_a_positive_sell():
    sell = next(f for f in batch().fills if f.side is Side.SELL)
    assert sell.quantity == Decimal("11")
    assert sell.price == Decimal("19.4")


# --- Double-counting, in both directions ---------------------------------


def test_sweep_dividend_and_its_reinvestment_are_one_cash_event():
    """A2-9: the sweep IS cash, so its dividend and the reinvestment of that
    dividend back into it are two legs of ONE event. Counting both doubles the
    money. Asserted on the real-shape file rather than a synthesised pair."""
    b = batch()
    sweep_cash = [c for c in b.cash if c.symbol == "SPAXX"]
    assert len(sweep_cash) == 1
    assert sweep_cash[0].kind == "dividend"
    assert sweep_cash[0].amount == Decimal("7.44")
    assert not [f for f in b.fills if f.instrument.symbol == "SPAXX"]


def test_security_drip_records_both_legs():
    """The opposite case, and the one an over-broad INTERNAL rule would break:
    a real security's dividend is cash in, and the reinvestment is a genuine
    acquisition spending it. Both belong in the ledger."""
    b = batch()
    dividend = next(c for c in b.cash if c.symbol == "DVDX")
    fill = next(f for f in b.fills if f.instrument.symbol == "DVDX")
    assert dividend.kind == "dividend"
    assert dividend.amount == Decimal("11.28")
    assert fill.funding_source == "reinvestment"
    assert fill.side is Side.BUY
    assert fill.quantity == Decimal("0.197")


def test_back_dated_reinvestment_uses_the_run_date():
    """`REINVESTMENT as of 2026-04-25 ...` on a row whose Run Date is 04/29.
    The 'as of' date is inside the action prose and must not be mistaken for
    the row's date."""
    fill = next(f for f in batch().fills if f.instrument.symbol == "DVDX")
    assert fill.executed_at.date().isoformat() == "2026-04-29"


def test_exchange_between_sweeps_produces_nothing():
    b = batch()
    assert not [c for c in b.cash if c.symbol == "FDRXX"]
    assert not [f for f in b.fills if f.instrument.symbol == "FDRXX"]


# --- The employer-plan dialect -------------------------------------------


def test_plan_dialect_rows_route_and_map():
    """Bare-empty fields, Title-case verbs, an EMPTY Symbol column and a short
    unprefixed account number. The plan account's rows must route and classify
    exactly like the brokerage dialect's."""
    plan = [c for c in batch().cash if c.external_ref == "90210"]
    assert {c.kind for c in plan} == {"deposit", "dividend", "rebate", "fee"}
    assert all(c.symbol is None for c in plan)


def test_plan_dialect_dividend_uses_the_bare_verb():
    """`Dividends` (plan) and `DIVIDEND RECEIVED` (brokerage) are separate
    rules. Both must be live -- this is the twin-invariant shape that keeps
    recurring."""
    plan_dividend = next(
        c for c in batch().cash if c.external_ref == "90210" and c.kind == "dividend"
    )
    assert plan_dividend.amount == Decimal("1.27")


# --- The trailing disclaimer block ---------------------------------------


def test_disclaimer_block_warns_but_never_blocks():
    """A real export's legal footer is permanently unmapped by design. It must
    produce warnings and never refuse a commit -- blocking on it would make
    every real file unimportable."""
    b = batch()
    disclaimer = [w for w in b.warnings if "bad date" in w]
    assert len(disclaimer) >= 8
    assert not [m for _, m in b.blocking if "bad date" in m]


def test_venues_own_header_inconsistency_is_present_in_the_fixture():
    """The footer writes `Fees($)`; the header writes `Fees ($)`. This is the
    concrete reason normalize_field strips the parenthetical structurally
    instead of aliasing observed spellings. If the fixture ever loses this
    line, that reasoning loses its evidence."""
    assert "Fees($)" in FIXTURE
    assert "Fees ($)" in FIXTURE.splitlines()[2]


# --- Known-wrong behaviour, pinned deliberately --------------------------


def test_employer_plan_gain_loss_blocks_every_commit():
    """KNOWN GAP -- this is the real export's actual behaviour today.

    An employer-plan `Investment Gain/Loss` row carries a dollar figure in
    Amount, matches no rule, and therefore blocks the commit under §8. The
    owner's real export contains several, so it cannot be committed at all as
    the importer stands.

    The row is periodic market-value change, not a transaction: recording it
    as cash would inject money that never moved, and Deadband derives
    unrealized value from positions and prices instead. INTERNAL is the likely
    resolution, but it is a design decision, not a bug fix -- see
    docs/known-gaps.md. When it is settled, this test changes deliberately."""
    b = batch()
    assert [(ref, "INVESTMENT GAIN/LOSS" in msg) for ref, msg in b.blocking] == [("90210", True)]


def test_employer_plan_unit_quantities_are_currently_discarded():
    """KNOWN GAP -- pins a real loss of information.

    A plan `Contributions` row carries Quantity 2.874 and Amount 118.44: it is
    the PURCHASE of 2.874 units of the plan's fund at an implied 41.21, with
    the price column left empty. The rule table maps it to CASH(deposit), so
    the units -- and therefore the entire position held inside the plan -- are
    invisible to the ledger. `RECORDKEEPING FEE` is the same shape in reverse:
    a fee paid by SELLING 0.017 units.

    Recorded rather than fixed: deciding whether a plan holding is a position
    (and what instrument it is, given the export supplies no ticker) is
    subsystem-shaped work. See docs/known-gaps.md."""
    b = batch()
    contribution = next(c for c in b.cash if c.external_ref == "90210" and c.kind == "deposit")
    assert contribution.amount == Decimal("118.44")
    # The units are nowhere: no fill exists for the plan account at all.
    assert not [f for f in b.fills if f.external_ref == "90210"]
