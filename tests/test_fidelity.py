import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from importers.base import OUTFLOW_KINDS
from importers.fidelity import (
    RULES,
    FidelityImporter,
    Outcome,
    classify,
    is_sweep,
    parse_option_symbol,
)
from ledger.types import AssetClass, Side

# Anchored to this test file's own location, not the process cwd — same
# hazard as test_purity.py's discovery bug (item 2) and test_coinbase.py's
# fixture path (item 6's other half): a path relative to "tests/fixtures/..."
# only resolves when pytest happens to be invoked from the repo root.
_FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
FIXTURE = (_FIXTURES_DIR / "fidelity" / "activity.csv").read_text()


def batch():
    return FidelityImporter().parse(FIXTURE)


def test_equity_buy_is_mapped():
    f = batch().fills[0]
    assert f.side is Side.BUY
    assert f.instrument.symbol == "SPY"
    assert f.instrument.asset_class is AssetClass.EQUITY
    assert f.quantity == Decimal("10")
    assert f.price == Decimal("500.00")
    assert f.executed_at == datetime(2026, 1, 15, tzinfo=UTC)


def test_negative_quantity_becomes_a_sell_with_positive_quantity():
    """Fidelity signs quantity; the ledger never stores a negative quantity."""
    f = batch().fills[1]
    assert f.side is Side.SELL
    assert f.quantity == Decimal("10")


def test_commission_and_fees_are_summed():
    f = batch().fills[2]
    assert f.fee == Decimal("1.40")


def test_option_symbol_is_parsed_into_contract_terms():
    inst = parse_option_symbol("-SPY260919C500")
    assert inst is not None
    assert inst.asset_class is AssetClass.OPTION
    assert inst.underlying == "SPY"
    assert inst.expiry == datetime(2026, 9, 19, tzinfo=UTC).date()
    assert inst.option_right == "call"
    assert inst.strike == Decimal("500")
    assert inst.contract_multiplier == Decimal("100")


def test_put_option_symbol_is_parsed():
    inst = parse_option_symbol("-QQQ261218P400.5")
    assert inst is not None
    assert inst.option_right == "put"
    assert inst.strike == Decimal("400.5")


def test_non_option_symbol_returns_none():
    assert parse_option_symbol("SPY") is None


def test_option_fills_use_the_option_instrument():
    opt_fills = [f for f in batch().fills if f.instrument.asset_class is AssetClass.OPTION]
    assert len(opt_fills) == 2
    assert opt_fills[0].instrument.contract_multiplier == Decimal("100")


def test_dividend_becomes_an_attributed_cash_movement():
    dividends = [c for c in batch().cash if c.kind == "dividend"]
    assert len(dividends) == 1
    assert dividends[0].amount == Decimal("42.15")
    assert dividends[0].symbol == "SPY"


def test_transfer_becomes_a_deposit():
    deposits = [c for c in batch().cash if c.kind == "deposit"]
    assert len(deposits) == 1
    assert deposits[0].amount == Decimal("2000.00")


def test_account_number_is_carried_for_routing():
    """A venue with several accounts must route rows to the right one."""
    refs = {f.external_ref for f in batch().fills}
    assert refs == {"X12345678", "X87654321"}


# --- Task 4: external_ref is the account NUMBER, never the nickname --------
#
# Real exports carry both an "Account" (nickname, e.g. "INDIVIDUAL - TOD") and
# a separate "Account Number" column. The nickname is neither stable nor
# unique (two accounts can share one), so routing must key on the number.

MULTI_ACCOUNT_FIXTURE = (
    "Run Date,Account,Account Number,Action,Symbol,Description,Quantity,"
    "Price,Commission,Fees,Amount\n"
    "01/15/2026,Individual,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,"
    "1,100.00,0.00,0.00,-100.00\n"
    # Same nickname as the row above, deliberately -- if external_ref were
    # ever read from the nickname column, both rows would collapse onto the
    # same ref instead of the two distinct account numbers.
    "01/16/2026,Individual,A0000002,YOU BOUGHT,VTI,VANGUARD TOTAL STOCK "
    "MARKET ETF,1,100.00,0.00,0.00,-100.00\n"
)


def test_external_ref_is_the_account_number_not_the_nickname():
    """Real exports carry BOTH an account nickname and an account number. The
    number is the identifier; the nickname is not stable and is not unique."""
    result = FidelityImporter().parse(MULTI_ACCOUNT_FIXTURE)
    assert {f.external_ref for f in result.fills} == {"A0000001", "A0000002"}


def test_refs_seen_includes_an_account_whose_rows_are_entirely_unmapped():
    """The account contributing nothing but unrecognised actions is exactly
    the one a fills/cash-derived report can never see -- refs_seen is derived
    from the raw rows, independent of whether they classified. Fails if
    refs_seen is built from fills/cash instead of from every row's account
    number column."""
    header = (
        "Run Date,Account Number,Action,Symbol,Description,Quantity,"
        "Price,Commission,Fees,Amount"
    )
    rows = "\n".join(
        [
            header,
            "01/15/2026,A0000001,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,1,100.00,0.00,0.00,-100.00",
            "01/16/2026,A0000005,SOME BRAND NEW ACTION NOBODY MAPPED,AAA,DESC,,,,,123.45",
            "01/17/2026,A0000005,ANOTHER UNRECOGNISED ACTION,BBB,DESC,,,,,67.89",
        ]
    )
    result = FidelityImporter().parse(rows + "\n")

    assert set(result.refs_seen) == {"A0000001", "A0000005"}
    # A0000005 contributed zero fills and zero cash -- refs_seen is the ONLY
    # place it's visible. Guards the guard: if this were false, the assertion
    # above could pass vacuously off a fills/cash-derived implementation too.
    assert all(f.external_ref != "A0000005" for f in result.fills)
    assert all(c.external_ref != "A0000005" for c in result.cash)


def test_missing_account_number_column_falls_back_to_none_not_the_nickname():
    """An export without the Account Number column (e.g. an older export
    shape) must not silently fall back to the unreliable nickname -- routing
    on a nickname is exactly the bug this task fixes. Unroutable is the
    correct, honest outcome."""
    header = "Run Date,Account,Action,Symbol,Description,Quantity,Price,Commission,Fees,Amount"
    row = header + "\n06/01/2026,Individual,YOU BOUGHT,AAA,ACME CORP,1,10.00,0.00,0.00,-10.00\n"
    result = FidelityImporter().parse(row)
    assert len(result.fills) == 1
    assert result.fills[0].external_ref is None


# --- "Never drop a row silently" -------------------------------------------


def test_every_input_row_is_accounted_for():
    """Every data row ends up in exactly one of fills, cash, or unmapped_rows."""
    data_rows = len(FIXTURE.strip().splitlines()) - 1  # minus the header
    result = batch()
    assert data_rows == 7
    assert len(result.fills) + len(result.cash) + len(result.unmapped_rows) == data_rows


# --- Amendment: parse failures on option symbols fall back to equity -------


def test_option_symbol_with_invalid_calendar_date_returns_none():
    """A syntactically option-shaped symbol with an impossible date (month 13,
    day 32) must fall back to None, not raise — the caller then treats it as
    an equity rather than crashing the whole import."""
    assert parse_option_symbol("-SPY261332C500") is None


def test_fill_with_invalid_option_date_falls_back_to_equity_instead_of_crashing():
    header = FIXTURE.splitlines()[0]
    bad_row = (
        header
        + "\n06/01/2026,X12345678,YOU BOUGHT,-SPY261332C500,BAD OPTION,1,1.00,0.00,0.00,-1.00\n"
    )
    result = FidelityImporter().parse(bad_row)
    assert len(result.fills) == 1
    assert result.fills[0].instrument.asset_class is AssetClass.EQUITY
    assert result.fills[0].instrument.symbol == "-SPY261332C500"


# --- Amendment one: strip a UTF-8 BOM ---------------------------------------


def test_utf8_bom_is_stripped():
    """UTF-8 BOM (U+FEFF) at start of file should not break parsing.

    Without stripping it, csv.DictReader names the first field "﻿Run Date"
    instead of "Run Date", so every row's date lookup fails and the whole
    file imports at 0%.
    """
    with_bom = "﻿" + FIXTURE
    result = FidelityImporter().parse(with_bom)
    baseline = batch()
    assert len(result.fills) == len(baseline.fills) == 5
    assert len(result.cash) == len(baseline.cash) == 2
    assert result.fills[0].quantity == baseline.fills[0].quantity
    assert result.fills[0].price == baseline.fills[0].price


# --- Amendment three: preamble and trailing disclaimer lines ---------------


def test_preamble_lines_before_header_are_skipped():
    """Real Fidelity exports commonly carry report-title/date preamble lines
    before the header row. parse() must locate "Run Date" rather than
    assuming line 1 is the header, or the whole export fails to parse."""
    preamble = "Fidelity Investments\nAccount Activity Export\nGenerated 2026-08-04\n"
    result = FidelityImporter().parse(preamble + FIXTURE)
    baseline = batch()
    assert len(result.fills) == len(baseline.fills) == 5
    assert len(result.cash) == len(baseline.cash) == 2
    assert result.warnings == baseline.warnings
    assert result.fills[0].price == baseline.fills[0].price


def test_trailing_disclaimer_line_is_reported_as_unmapped_not_dropped():
    """A disclaimer block after the data rows must not be silently discarded —
    it should surface as an unmapped row with a warning, same as any other
    row that fails to parse."""
    trailing = FIXTURE + "This report is for informational purposes only.\n"
    result = FidelityImporter().parse(trailing)
    baseline = batch()
    assert len(result.fills) == len(baseline.fills)
    assert len(result.cash) == len(baseline.cash)
    assert len(result.unmapped_rows) == len(baseline.unmapped_rows) + 1
    assert any("purposes only" in w for w in result.warnings)


# --- Fix round 1, item 1: a malformed cash Amount must cost one row, ------
# --- never abort the whole file. -------------------------------------------


def test_malformed_cash_amount_is_reported_not_fatal():
    """A bad Amount on a cash row (dividend/transfer/interest) must not raise
    and must not take down the rest of the file with it — same defensive
    pattern already applied to the fill branch's Quantity/Price/Commission/Fees."""
    header = FIXTURE.splitlines()[0]
    rows = "\n".join(
        [
            header,
            "01/15/2026,X1,YOU BOUGHT,SPY,SPDR,10,500.00,0.00,0.00,-5000.00",
            "04/01/2026,X1,DIVIDEND RECEIVED,SPY,SPDR,,,,,N/A",
            "02/20/2026,X1,YOU SOLD,SPY,SPDR,10,520.00,0.00,0.03,5199.97",
        ]
    )
    result = FidelityImporter().parse(rows + "\n")
    assert len(result.fills) == 2
    assert len(result.cash) == 0
    assert len(result.unmapped_rows) == 1
    assert len(result.warnings) == 1
    assert "bad amount" in result.warnings[0]


# --- Fix round 1, item 2: direction comes from the action, not the sign, ---
# --- proven with rows where the two disagree. -------------------------------


def test_direction_comes_from_action_even_when_sign_disagrees():
    """A file where sign and action disagree must still resolve direction
    from the action. Every row in the brief's fixture has sign and action in
    agreement, so a sign-based implementation would pass all of those tests
    while being wrong — this is the case that actually distinguishes them."""
    header = FIXTURE.splitlines()[0]
    rows = "\n".join(
        [
            header,
            # SOLD with a POSITIVE quantity — sign says buy, action says sell.
            "07/01/2026,X1,YOU SOLD,SPY,SPDR,10,500.00,0.00,0.00,5000.00",
            # BOUGHT with a NEGATIVE quantity — sign says sell, action says buy.
            "07/02/2026,X1,YOU BOUGHT,SPY,SPDR,-10,500.00,0.00,0.00,-5000.00",
        ]
    )
    result = FidelityImporter().parse(rows + "\n")
    assert len(result.fills) == 2
    sold_positive_sign, bought_negative_sign = result.fills
    assert sold_positive_sign.side is Side.SELL
    assert sold_positive_sign.quantity == Decimal("10")
    assert bought_negative_sign.side is Side.BUY
    assert bought_negative_sign.quantity == Decimal("10")


# --- Fix round 1, item 3: header columns are read case-insensitively too ---


# --- Final fix wave, item 2: poison Decimal values (NaN/Infinity) pass the --
# --- existing `except InvalidOperation` guard, since both are valid Decimal
# --- constructions. Verified upstream: Infinity survives Fill.__post_init__'s
# --- `quantity > 0` check, the DB's `quantity > 0` CHECK, and becomes a live
# --- allocation in group_fills. ----------------------------------------------


def test_non_finite_quantity_is_rejected():
    header = FIXTURE.splitlines()[0]
    bad_row = header + "\n06/01/2026,X1,YOU BOUGHT,SPY,SPDR,Infinity,500.00,0.00,0.00,-5000.00\n"
    result = FidelityImporter().parse(bad_row)
    assert result.fills == ()
    assert len(result.unmapped_rows) == 1
    assert any("non-finite" in w for w in result.warnings)


def test_non_finite_price_is_rejected():
    header = FIXTURE.splitlines()[0]
    bad_row = header + "\n06/01/2026,X1,YOU BOUGHT,SPY,SPDR,10,Infinity,0.00,0.00,-5000.00\n"
    result = FidelityImporter().parse(bad_row)
    assert result.fills == ()
    assert len(result.unmapped_rows) == 1
    assert any("non-finite" in w for w in result.warnings)


# --- Blocker pass, item 4: fee (commission + fees) and cash amount were -----
# --- computed from the same poison-prone Decimal fields as quantity/price ---
# --- but were not checked for finiteness anywhere. Fill.__post_init__ never
# --- validates fee at all, and cash_movement.amount has no CHECK constraint.


def test_non_finite_fee_is_rejected():
    header = FIXTURE.splitlines()[0]
    bad_row = header + "\n06/01/2026,X1,YOU BOUGHT,SPY,SPDR,10,500.00,Infinity,0.00,-5000.00\n"
    result = FidelityImporter().parse(bad_row)
    assert result.fills == ()
    assert len(result.unmapped_rows) == 1
    assert any("non-finite" in w for w in result.warnings)


def test_non_finite_cash_amount_is_rejected():
    """Fails if the cash branch has no finiteness guard: this row would show
    up in `cash` with amount Decimal("Infinity") instead of being warned
    about and skipped."""
    header = FIXTURE.splitlines()[0]
    bad_row = header + "\n06/01/2026,X1,INTEREST EARNED,,Interest,0,0,0.00,0.00,Infinity\n"
    result = FidelityImporter().parse(bad_row)
    assert result.cash == ()
    assert len(result.unmapped_rows) == 1
    assert any("non-finite" in w for w in result.warnings)


# --- Item 3: a preamble line that merely mentions "run date" in prose must --
# --- not be mistaken for the actual header row. ------------------------------


def test_preamble_line_containing_run_date_as_prose_is_not_mistaken_for_the_header():
    """A real preamble line like "Report run date: 08/04/2026" contains the
    phrase "run date" too, so a first-match-wins scan for that phrase alone
    would pick it as the header — csv.DictReader would then take its field
    names from that sentence and every data row would fail to parse (a "bad
    date" warning per row, zero usable rows). Fails if _locate_header ever
    goes back to matching on "run date" alone: this preamble line does NOT
    also contain "action" or "amount", so it must be skipped in favor of the
    real header further down."""
    preamble = "Fidelity Investments\nReport run date: 08/04/2026\nAccount Activity Export\n"
    result = FidelityImporter().parse(preamble + FIXTURE)
    baseline = batch()
    assert len(result.fills) == len(baseline.fills) == 5
    assert len(result.cash) == len(baseline.cash) == 2
    assert result.fills[0].price == baseline.fills[0].price


# --- Item 4: cash amount sign convention. amount is always positive; --------
# --- direction lives in `kind` alone (see importers.base.OUTFLOW_KINDS). ----


def test_withdrawal_amount_is_positive_not_the_raw_negative_export_sign():
    """Fidelity's Amount column is signed (negative for an outflow like
    "ELECTRONIC FUNDS TRANSFER PAID"). Fails if the abs() normalization is
    removed: amount would then be Decimal("-2000.00"), disagreeing with
    Coinbase's twin (which always emits a positive amount for the same
    kind), and anything summing cash_movement.amount across accounts would
    get garbage."""
    header = FIXTURE.splitlines()[0]
    bad_row = header + "\n06/01/2026,X1,ELECTRONIC FUNDS TRANSFER PAID,,,,,,,-2000.00\n"
    result = FidelityImporter().parse(bad_row)
    withdrawals = [c for c in result.cash if c.kind == "withdrawal"]
    assert len(withdrawals) == 1
    assert withdrawals[0].amount == Decimal("2000.00")


def _with_currency_suffixed_headers(text: str, suffix: str) -> str:
    """Rewrite the fixture's money column names into the form a real Fidelity
    export uses, preserving column order so the data rows still align."""
    lines = text.splitlines()
    header = lines[0]
    for col in ("Price", "Commission", "Fees", "Amount"):
        header = header.replace(col, f"{col}{suffix}")
    return "\n".join([header, *lines[1:]]) + "\n"


def test_currency_suffixed_money_headers_are_parsed_not_silently_zeroed():
    """A real Fidelity export names its money columns "Price ($)", not "Price".

    row.get("price") then misses, _decimal(None) returns Decimal("0"), and every
    price, commission, fee and cash amount imports as zero with NO warning —
    quantities and dates survive, so the result looks plausible while being
    financially meaningless. Assert on the values themselves rather than on a
    row count: the row count is identical either way, so only the amounts can
    distinguish the bug from correct behaviour.
    """
    result = FidelityImporter().parse(_with_currency_suffixed_headers(FIXTURE, " ($)"))
    baseline = batch()

    assert [f.price for f in result.fills] == [f.price for f in baseline.fills]
    assert [f.fee for f in result.fills] == [f.fee for f in baseline.fills]
    assert [c.amount for c in result.cash] == [c.amount for c in baseline.cash]
    # Guard the guard: if the baseline were all zeros the comparison above would
    # hold vacuously and could not fail.
    assert result.fills[0].price == Decimal("500.00")
    assert any(f.fee > 0 for f in result.fills)
    assert all(c.amount > 0 for c in result.cash)


def test_money_headers_without_a_space_before_the_paren_are_also_parsed():
    """Fidelity is inconsistent with itself: the export's own trailing disclaimer
    refers to the "Fees($)" column with no space, while the header row writes
    "Fees ($)". Normalisation must therefore be structural, not a lookup table of
    the two spellings that happen to have been observed."""
    result = FidelityImporter().parse(_with_currency_suffixed_headers(FIXTURE, "($)"))
    baseline = batch()
    assert result.fills[0].price == Decimal("500.00")
    assert [f.fee for f in result.fills] == [f.fee for f in baseline.fills]
    assert any(f.fee > 0 for f in result.fills)
    assert [c.amount for c in result.cash] == [c.amount for c in baseline.cash]


def test_lowercase_header_is_parsed_the_same_as_the_standard_header():
    """The header row is located case-insensitively ("run date" in line.lower()),
    so a differently-cased real export must not then read zero usable fields —
    columns must be read the same way the header was found."""
    lines = FIXTURE.splitlines()
    lowercase_fixture = "\n".join([lines[0].lower(), *lines[1:]]) + "\n"
    result = FidelityImporter().parse(lowercase_fixture)
    baseline = batch()
    assert len(result.fills) == len(baseline.fills) == 5
    assert len(result.cash) == len(baseline.cash) == 2
    assert result.fills[0].price == baseline.fills[0].price
    assert result.fills[0].quantity == baseline.fills[0].quantity


# --- Task 2: the declarative action rule table ------------------------------
#
# _CASH_ACTIONS was a dict of four exact prefixes. Real Fidelity action text
# is compound (action, security name, ticker, settlement type, all
# concatenated), and the reinvestment decision cannot be made from the action
# alone: REINVESTMENT means cash when the symbol is a money-market sweep and
# a fill when it's a real security. classify() is keyed on action AND symbol.


def test_reinvestment_of_a_real_security_is_a_fill():
    """A DRIP purchase is a genuine acquisition with real basis, tagged so that
    contributed_capital can exclude it."""
    rule = classify("REINVESTMENT ACME CORP (AAA) (CASH)", "AAA")
    assert rule is not None
    assert rule.outcome is Outcome.FILL
    assert rule.funding_source == "reinvestment"
    assert rule.side is Side.BUY


def test_reinvestment_of_a_sweep_fund_is_internal_not_cash():
    """The sweep IS cash under A2-9, so the dividend leg already recorded this
    money. Recording the reinvestment leg too would count it twice."""
    rule = classify("REINVESTMENT MONEY MARKET (SPAXX) (CASH)", "SPAXX")
    assert rule is not None
    assert rule.outcome is Outcome.INTERNAL


def test_the_same_action_verb_resolves_differently_by_symbol():
    """The whole reason the table is keyed on action AND symbol. An action-only
    table cannot express this, so this test fails against any such design."""
    security = classify("REINVESTMENT ACME CORP (AAA) (CASH)", "AAA")
    sweep = classify("REINVESTMENT MONEY MARKET (SPAXX) (CASH)", "SPAXX")
    assert security.outcome is not sweep.outcome


def test_return_of_capital_is_not_aliased_to_dividend():
    """A return of capital reduces basis rather than being income. Recording it
    as a dividend overstates income and leaves basis high."""
    rule = classify("RETURN OF CAPITAL ACME PFD (AAA) (CASH)", "AAA")
    assert rule.outcome is Outcome.CASH
    assert rule.cash_kind == "return_of_capital"


def test_foreign_tax_paid_is_an_outflow():
    rule = classify("FOREIGN TAX PAID ACME ADR (AAA) (CASH)", "AAA")
    assert rule.outcome is Outcome.CASH
    assert rule.cash_kind == "tax"
    assert "tax" in OUTFLOW_KINDS


def test_an_unrecognised_action_classifies_as_none():
    assert classify("SOME BRAND NEW ACTION NOBODY MAPPED", "AAA") is None


# One (action, symbol) sample per rule in RULES, using synthetic tickers only,
# each engineered to be the FIRST matching rule for its sample so the
# reachability test below can prove no rule is shadowed by an earlier one.
RULE_COVERAGE_SAMPLES = [
    ("REINVESTMENT MONEY MARKET (SPAXX) (CASH)", "SPAXX"),  # reinvest_sweep
    ("REINVESTMENT ACME CORP (AAA) (CASH)", "AAA"),  # reinvest_security
    ("EXCHANGED TO MONEY MARKET (SPAXX)", "SPAXX"),  # exchange_sweep
    ("DIVIDEND RECEIVED ACME CORP (AAA) (CASH)", "AAA"),  # dividend_received
    ("DIVIDENDS ACME CORP (AAA) (CASH)", "AAA"),  # dividends
    ("INTEREST EARNED ON CASH (AAA)", "AAA"),  # interest
    ("RETURN OF CAPITAL ACME PFD (AAA) (CASH)", "AAA"),  # return_of_capital
    ("FOREIGN TAX PAID ACME ADR (AAA) (CASH)", "AAA"),  # foreign_tax
    ("FEE CHARGED ACCOUNT MAINTENANCE FEE", ""),  # fee_charged
    ("RECORDKEEPING FEE Q1 2026", ""),  # recordkeeping_fee
    ("REVENUE CREDIT ACME CORP (AAA)", "AAA"),  # revenue_credit
    ("ELECTRONIC FUNDS TRANSFER RECEIVED", ""),  # eft_in
    ("ELECTRONIC FUNDS TRANSFER PAID", ""),  # eft_out
    ("CASH CONTRIBUTION IRA 2026", ""),  # cash_contribution
    ("CO CONTR 2026 Q1", ""),  # employer_contribution
    ("PARTIC CONTR 2026 Q1", ""),  # participant_contribution
    ("CONTRIBUTIONS MISC 2026", ""),  # contributions
]


def test_every_rule_is_reachable():
    """A rule shadowed by an earlier one is dead code that looks like coverage.
    Each rule must be the FIRST match for at least one sample, or the table has
    an ordering bug."""
    matched = {classify(action, symbol).name for action, symbol in RULE_COVERAGE_SAMPLES}
    assert matched == {r.name for r in RULES}


# --- Fix round 1, item 1: end-to-end tests pinning the double-counting ------
# --- invariant. Every classify()-level test above inspects the Rule object -
# --- returned by classify() alone; none of them parses a CSV, so the wiring
# --- between the rule table and parse() -- the thing that actually prevents
# --- the double count -- was unpinned. A mutant that guts the INTERNAL branch
# --- in parse() (while leaving RULES and classify() untouched) passed the
# --- whole suite green and silently doubled every sweep dividend.


def test_sweep_dividend_and_its_reinvestment_produce_exactly_one_cash_movement():
    """The sweep IS cash (A2-9): a sweep dividend appears as two CSV rows (the
    dividend, then its reinvestment back into the sweep), but that is ONE cash
    event, not two. Fails if the INTERNAL branch in parse() is ever collapsed
    into the cash path or reordered so the reinvestment leg gets recorded."""
    header = FIXTURE.splitlines()[0]
    rows = "\n".join(
        [
            header,
            "04/01/2026,X1,DIVIDEND RECEIVED MONEY MARKET (SPAXX) (CASH),SPAXX,"
            "FIDELITY GOVERNMENT MONEY MARKET,,,,,10.00",
            "04/01/2026,X1,REINVESTMENT MONEY MARKET (SPAXX) (CASH),SPAXX,"
            "FIDELITY GOVERNMENT MONEY MARKET,10,1.00,0.00,0.00,-10.00",
        ]
    )
    result = FidelityImporter().parse(rows + "\n")
    assert len(result.cash) == 1
    assert len(result.fills) == 0
    assert result.cash[0].amount == Decimal("10.00")


def test_real_security_dividend_and_its_reinvestment_produce_both_legs():
    """The opposite case: a real security's dividend is cash in, and the DRIP
    that follows is a genuine acquisition funded by that cash. Both legs must
    record, or contributed_capital/cost_basis silently lose real data."""
    header = FIXTURE.splitlines()[0]
    rows = "\n".join(
        [
            header,
            "04/01/2026,X1,DIVIDEND RECEIVED ACME CORP (AAA) (CASH),AAA,ACME CORP,,,,,5.00",
            "04/01/2026,X1,REINVESTMENT ACME CORP (AAA) (CASH),AAA,"
            "ACME CORP,0.5,10.00,0.00,0.00,-5.00",
        ]
    )
    result = FidelityImporter().parse(rows + "\n")
    assert len(result.cash) == 1
    assert len(result.fills) == 1
    assert result.fills[0].funding_source == "reinvestment"


# --- Fix round 1, item 2: the BOUGHT/SOLD branch must anchor on the leading -
# --- "YOU BOUGHT"/"YOU SOLD" verb, not scan for the bare substring anywhere -
# --- in the action. This task's entire premise is that the security NAME is
# --- concatenated into the action field, so a name containing "SOLD" (e.g.
# --- "SOLDIERS FIELD CAP") would otherwise hijack the row as a phantom sell.


def test_action_containing_sold_inside_a_security_name_is_not_hijacked_as_a_sell():
    header = FIXTURE.splitlines()[0]
    bad_row = (
        header
        + "\n04/01/2026,X1,DIVIDEND RECEIVED SOLDIERS FIELD CAP (AAA) (CASH),AAA,"
        "SOLDIERS FIELD CAP,,,,,7.50\n"
    )
    result = FidelityImporter().parse(bad_row)
    assert result.fills == ()
    assert len(result.cash) == 1
    assert result.cash[0].kind == "dividend"
    assert result.cash[0].amount == Decimal("7.50")


# --- Task 3: sweep membership is explicit, and the staleness guard makes the -
# --- set's decay visible in both directions. -------------------------------


def test_a_sweep_symbol_is_recognised():
    assert is_sweep("SPAXX") is True
    assert is_sweep("spaxx") is True   # case-insensitive


def test_a_real_security_is_not_a_sweep():
    assert is_sweep("AAA") is False
    assert is_sweep("") is False
    assert is_sweep(None) is False


def test_price_is_not_used_to_infer_sweepness():
    """A real security can trade at exactly 1.00. Inferring from price would
    silently convert a genuine position into cash -- which is why the set is
    explicit. This test pins the DESIGN, not just the behaviour."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,YOU BOUGHT,AAA,PENNY CO,100,1.00,0.00,0.00,-100.00\n"
    result = FidelityImporter().parse(row)
    assert len(result.fills) == 1
    assert result.fills[0].instrument.symbol == "AAA"


def test_a_sweep_symbol_priced_far_from_par_warns():
    """Sweep funds hold a 1.00 NAV by construction. A deviation means either the
    set has acquired a non-sweep symbol or a sweep has broken the buck -- both
    need a human, and neither should pass unremarked."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,REINVESTMENT MM (SPAXX) (CASH),SPAXX,MM,10,1.40,0.00,0.00,-14.00\n"
    result = FidelityImporter().parse(row)
    assert any("sweep" in w.lower() and "SPAXX" in w for w in result.warnings)


def test_an_unlisted_symbol_reinvesting_at_par_warns_the_set_may_be_stale():
    """The direction that actually costs money. An unlisted sweep is treated as a
    real security, so its reinvestment becomes a fill that spends the dividend --
    net cash nets to zero and a phantom position appears, silently. The warning is
    the only thing that surfaces a missing ticker."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,REINVESTMENT MM (NEWSW) (CASH),NEWSW,MM,10,1.00,0.00,0.00,-10.00\n"
    result = FidelityImporter().parse(row)
    assert any("NEWSW" in w for w in result.warnings)


def test_a_real_security_at_a_dollar_is_still_imported_as_a_security():
    """The warning must not become classification. A genuine security trading at
    a dollar stays a security -- a spurious warning is cheap, silently converting
    a position into cash is not."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,YOU BOUGHT,AAA,PENNY CO,100,1.00,0.00,0.00,-100.00\n"
    result = FidelityImporter().parse(row)
    assert len(result.fills) == 1
    assert result.fills[0].instrument.symbol == "AAA"


# --- Task 5: silent loss must be impossible ---------------------------------


def test_an_unmapped_row_carrying_money_blocks_the_commit():
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,MYSTERIOUS NEW ACTION,AAA,DESC,,,,,123.45\n"
    result = FidelityImporter().parse(row)
    assert result.blocking, "a money-carrying unmapped row must block"
    # C1: each blocking reason is (external_ref, message) -- attributed to
    # the row's own account so a caller can drop reasons belonging to an
    # account registered ignore_on_import, without dropping every reason.
    assert any(ref == "X1" for ref, _msg in result.blocking)
    assert any("MYSTERIOUS" in msg for _ref, msg in result.blocking)


def test_an_unmapped_row_with_no_financial_content_only_warns():
    """The trailing disclaimer block is permanently unmapped by design. If it
    blocked, no real export could ever be committed."""
    result = FidelityImporter().parse(FIXTURE + "This report is informational only.\n")
    assert result.blocking == ()
    assert result.unmapped_rows


def test_an_unmapped_row_with_a_valid_date_and_no_money_only_warns():
    """The disclaimer case above never actually reaches the money-carrying
    check: its line has no commas, so it fails the *date* parse and is warned
    about via that branch entirely, before classify() is ever consulted --
    confirmed directly against _locate_header/DictReader's own output for that
    line. That means the disclaimer test alone cannot pin the "no financial
    content warns only" half of the guard: a mutant that blocks every
    unmapped row unconditionally would leave the disclaimer test green
    (blocking still empty, for an unrelated reason) despite the guard being
    broken. This row has a VALID date and an unmapped action, and reaches
    classify() with quantity/amount both blank, so it genuinely exercises the
    guard's money check rather than sidestepping it."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,ADMINISTRATIVE NOTICE,AAA,DESC,,,,,\n"
    result = FidelityImporter().parse(row)
    assert result.blocking == ()
    assert result.unmapped_rows


def test_a_fill_shaped_row_with_a_zero_price_is_reported():
    """Downstream of _decimal, a missing column and a genuine zero are
    indistinguishable. The check must live where they still differ."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,YOU BOUGHT,AAA,DESC,10,0.00,0.00,0.00,0.00\n"
    result = FidelityImporter().parse(row)
    assert any("zero price" in w.lower() for w in result.warnings)


# --- C2: cash rows had no zero-amount guard ---------------------------------
#
# zero_price_warning was shared across VENUES but never across ROW KINDS.
# Renaming the fixture's Amount column reproduces the exact silent-zero
# defect that motivated the whole task, on the cash side: a fully
# "successful" parse in which every cash figure is silently $0.00.


def test_renaming_the_amount_column_zeroes_every_cash_movement_with_a_warning():
    """Demonstrated bug: rename FIXTURE's Amount column to Net Amount and
    every cash row's amount silently resolves to Decimal('0') via
    _decimal(None) -- no warning, dates/actions/symbols all correct. Fails
    (no "zero amount" warning present) without the guard."""
    header = FIXTURE.splitlines()[0].replace("Amount", "Net Amount")
    body = "\n".join(FIXTURE.splitlines()[1:])
    result = FidelityImporter().parse(header + "\n" + body + "\n")

    baseline = batch()
    assert len(result.cash) == len(baseline.cash) == 2
    # Guard the guard: the renamed column really did zero every cash amount.
    assert all(c.amount == Decimal("0") for c in result.cash)

    zero_amount_warnings = [w for w in result.warnings if "zero amount" in w.lower()]
    assert len(zero_amount_warnings) == len(result.cash)
