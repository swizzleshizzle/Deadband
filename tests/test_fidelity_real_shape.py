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
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from importers.fidelity import RULES, FidelityImporter, Outcome, classify
from ledger.grouping import group_fills
from ledger.pnl import compute_pnl
from ledger.types import AssetClass, Direction, Fill, FillSource, Side, TradeStatus

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
_DATED_ROWS = 25


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
    invisible to any assertion made on fills or cash.

    `Outcome.UNSUPPORTED` rules are excluded, keyed on the outcome rather than
    a hardcoded name list so a future unsupported verb is exempt automatically.
    A rule whose entire purpose is to refuse a row we have never received
    cannot also be required to appear in a fixture of rows we have received --
    requiring it would force fabricating the very row shape E1 says not to
    invent (real assignments/exercises: zero, across three accounts and five
    years). That is not a gap in this test's coverage; it is a different
    source of coverage, asserted below in
    test_unsupported_rules_are_exercised_by_the_hand_written_sample_table.

    `Outcome.CORPORATE_ACTION` rules are excluded for the same shape of
    reason, verified rather than assumed (see the design spec's §1): a
    corporate action appears ONLY in the History dialect (no Account/Account
    Number columns, a Cash Balance column instead) -- the multi-year exports
    this fixture is not one of. Requiring these five rules to appear here
    would force inventing rows this dialect structurally cannot emit. Their
    coverage lives in tests/test_fidelity_history.py's
    real_shape_history.csv instead, asserted below in
    test_corporate_action_rules_are_exercised_by_the_history_fixture."""
    matched = set()
    for row in _data_rows():
        rule = classify(row["Action"], row["Symbol"])
        if rule is not None:
            matched.add(rule.name)
    required = {
        r.name
        for r in RULES
        if r.outcome not in (Outcome.UNSUPPORTED, Outcome.CORPORATE_ACTION)
    }
    assert required - matched == set()


def test_unsupported_rules_are_exercised_by_the_hand_written_sample_table():
    """The other half of the split above. `Outcome.UNSUPPORTED` rules are
    exempted from the real-shape requirement because a real export cannot
    supply a row Michael has never received -- but that must not become a
    silent hole in coverage. Every such rule is still required to be
    exercised by SOMETHING: test_fidelity.py's RULE_COVERAGE_SAMPLES, the
    hand-written table its own test_every_rule_is_reachable checks against.
    Net effect: every rule in RULES is covered by exactly one of the two
    files, split by whether the row it needs can plausibly exist."""
    from tests.test_fidelity import RULE_COVERAGE_SAMPLES

    matched = {classify(action, symbol).name for action, symbol in RULE_COVERAGE_SAMPLES}
    required = {r.name for r in RULES if r.outcome is Outcome.UNSUPPORTED}
    assert required - matched == set()


def test_corporate_action_rules_are_exercised_by_the_history_fixture():
    """The three-way split's third leg. `Outcome.CORPORATE_ACTION` rules are
    exempted above because THIS fixture is the wrong dialect for them, not
    because no real-shape coverage exists at all -- they have their own
    real-shape fixture, tests/fixtures/fidelity/real_shape_history.csv, which
    is the dialect that actually carries them. Net effect, extending the
    docstring above: every rule in RULES is covered by exactly one of three
    sources, split by which dialect (or absence of any real occurrence) the
    row it needs belongs to."""
    from tests.test_fidelity_history import FIXTURE as HISTORY_FIXTURE

    text = HISTORY_FIXTURE.read_text(encoding="utf-8")
    lines = text.lstrip("﻿").splitlines()
    header_idx = next(i for i, line in enumerate(lines) if line.startswith("Run Date,"))
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    matched = set()
    for row in reader:
        if not _RUN_DATE_RE.match((row.get("Run Date") or "").strip()):
            continue
        rule = classify(row.get("Action") or "", row.get("Symbol") or "")
        if rule is not None:
            matched.add(rule.name)
    required = {r.name for r in RULES if r.outcome is Outcome.CORPORATE_ACTION}
    assert required - matched == set()


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
    three intended YOU BOUGHT/YOU SOLD rows become externally-funded fills via
    that branch; nothing else is dragged into it by a substring.

    Five in total: the original three, plus the Task 3 expiry pair (the
    opening YOU SOLD and its closing EXPIRY fill, which is funded externally
    too -- see build_expiry_fill). Their order follows the fixture's row
    order, not chronology, same as the original three."""
    external = [f for f in batch().fills if f.funding_source == "external"]
    assert len(external) == 5
    assert [f.side for f in external] == [
        Side.BUY,
        Side.BUY,
        Side.SELL,
        Side.SELL,
        Side.BUY,
    ]


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


def test_employer_plan_gain_loss_is_recognised_and_produces_nothing():
    """F1, DECIDED 2026-08-08: `Investment Gain/Loss` is INTERNAL.

    An employer-plan `Investment Gain/Loss` row is periodic market-value
    change, not a transaction. Deadband derives unrealized value from
    positions and prices, so recording this as `CASH` would inject money that
    never moved and double-count the appreciation the ledger already computes.
    It is recognised and deliberately produces nothing -- the same treatment,
    for the same reason, as a sweep reinvestment leg.

    Before this rule existed the row matched nothing, and because it carries a
    dollar figure in Amount, §8 blocked the commit -- so a real export could
    not be imported at all.

    Three things are asserted, because "produces nothing" has three distinct
    ways to be wrong: it must not block, it must not become cash, and it must
    not become a fill. A rule that merely stopped the blocking while quietly
    recording a cash movement would be the worse outcome of the two."""
    b = batch()
    gain_loss_row = next(
        r for r in _data_rows() if r["Action"].strip().upper().startswith("INVESTMENT GAIN/LOSS")
    )
    rule = classify(gain_loss_row["Action"], gain_loss_row["Symbol"])

    assert rule is not None and rule.outcome is Outcome.INTERNAL
    assert b.blocking == ()
    # The row's Amount reaches no cash movement and no fill.
    assert not [c for c in b.cash if c.amount == Decimal(gain_loss_row["Amount ($)"])]
    assert not [f for f in b.fills if f.external_ref == "90210"]


def test_recognising_gain_loss_did_not_silence_genuinely_unknown_actions():
    """The guard on F1's fix. `INTERNAL` is a claim that a row means nothing,
    and the cheapest way to make an importer stop complaining is to make that
    claim too widely. An action the venue has not been mapped for must still
    reach `blocking` when it carries money.

    Synthesised rather than added to the fixture: the fixture holds shapes a
    real export actually emits, and this is a shape it does not."""
    lines = FIXTURE.lstrip("﻿").splitlines()
    header = lines[2]
    row = (
        "04/02/2026,EXAMPLE SAVINGS PLAN,90210,Some Unmapped Action,,"
        "EXAMPLE REAL ESTATE FUND,,,0,,,,9.99,"
    )
    result = FidelityImporter().parse("\n".join([header, row]))
    assert [ref for ref, _ in result.blocking] == ["90210"]
    assert "SOME UNMAPPED ACTION" in result.blocking[0][1]


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


# --- Task 3: the closing fill actually closes a trade ----------------------


def test_an_expired_short_call_closes_and_realises_its_premium():
    """The whole point of the feature. Before this, only the opening SELL was
    recorded: the short stayed open forever, was valued as a liability that
    did not exist, and its premium was never realised.

    This test pins the PARSED SHAPE only -- sides, prices, dates, quantities.
    That the two fills actually group into one closed trade, and that the
    trade realises the premium, is
    test_an_expired_short_call_leaves_a_closed_trade_realising_the_premium
    below, which walks the ledger path instead of restating this one.
    """
    batch_ = FidelityImporter().parse(FIXTURE)
    opt = [f for f in batch_.fills if f.instrument.symbol == "-ZXCO261121C500"]
    assert len(opt) == 2
    opening = next(f for f in opt if f.side is Side.SELL)
    closing = next(f for f in opt if f.side is Side.BUY)
    assert opening.price == Decimal("4.00")
    assert closing.price == Decimal(0)
    assert closing.executed_at == datetime(2026, 11, 21, tzinfo=UTC)
    assert closing.quantity == opening.quantity


# Synthetic ids, exactly as tests/test_grouping.py and tests/test_pnl.py do
# it: the pure layer needs fill ids only to be distinct, and instrument ids
# only to be equal for fills in the same instrument. Nothing here touches a
# database -- group_fills and compute_pnl are pure functions over rows.
_LEDGER_ACCOUNT = UUID("00000000-0000-0000-0000-0000000000a1")
_LEDGER_INSTRUMENT = UUID("00000000-0000-0000-0000-0000000000b1")


def _as_ledger_fill(cf) -> Fill:
    """A parsed CanonicalFill as the ledger layer will see it once persisted.

    Every field the grouper and the P&L walk actually read -- executed_at,
    side, quantity, price, fee -- is carried over from the PARSED fill rather
    than restated here, so a wrong value produced by the importer reaches the
    assertions instead of being papered over by a hand-built row.
    """
    return Fill(
        id=uuid4(),
        account_id=_LEDGER_ACCOUNT,
        instrument_id=_LEDGER_INSTRUMENT,
        executed_at=cf.executed_at,
        side=cf.side,
        quantity=cf.quantity,
        price=cf.price,
        fee=cf.fee,
        fee_currency=cf.fee_currency,
        source=FillSource.CSV,
        venue_fill_id=cf.venue_fill_id,
        is_estimated=False,
    )


def test_an_expired_short_call_leaves_a_closed_trade_realising_the_premium():
    """Spec §6, seventh testing item: an open short call closed by its expiry
    yields realised P&L equal to the premium received and leaves no open
    position. This is the claim the whole feature rests on, and parse-level
    assertions cannot make it -- they stop before the grouper.

    The two fills are identified by PRICE, never by side, deliberately. The
    side of the closing fill is precisely what this test exists to exercise
    through the real path, so selecting on it would turn a wrong side into a
    StopIteration during setup instead of a failed assertion about a trade
    that never closed.

    aggregate_positions is deliberately NOT called for the "no open position"
    half. It aggregates whatever rows it is handed; the status filter that
    would exclude this trade lives in db/positions.py's SQL
    (`WHERE t.status = 'open'`), so feeding it a closed trade's row would
    assert on the test's own construction rather than on the ledger. The pure
    equivalents of "no open position" are the grouper's CLOSED status and the
    P&L walk's zero residual quantity, both asserted below.
    """
    batch_ = FidelityImporter().parse(FIXTURE)
    opt = [f for f in batch_.fills if f.instrument.symbol == "-ZXCO261121C500"]
    assert len(opt) == 2
    opening = next(f for f in opt if f.price == Decimal("4.00"))
    closing = next(f for f in opt if f.price == Decimal(0))

    ledger_fills = [_as_ledger_fill(opening), _as_ledger_fill(closing)]
    groups = group_fills(ledger_fills)
    assert len(groups) == 1
    g = groups[0]
    assert g.status is TradeStatus.CLOSED
    assert g.direction is Direction.SHORT
    # Closed ON the expiry, not on the Monday Fidelity booked it.
    assert g.closed_at == datetime(2026, 11, 21, tzinfo=UTC)

    pnl = compute_pnl(
        g.allocations,
        {f.id: f for f in ledger_fills},
        # The multiplier the SYMBOL parsed to, not a hand-typed 100: the
        # premium claim is only true if the contract size the instrument
        # carries is the one the arithmetic uses.
        {_LEDGER_INSTRUMENT: opening.instrument.contract_multiplier},
        g.direction,
    )
    # Premium received, in full and exactly: (4.00 - 0) x 1 contract x 100.
    # Not `> 0` -- a wrong side, a wrong multiplier and a wrong closing price
    # all still produce a positive number.
    assert pnl.realized_pnl == Decimal("400.00")
    # Nothing left open: the short is gone, not merely smaller.
    assert pnl.open_quantity == Decimal(0)
