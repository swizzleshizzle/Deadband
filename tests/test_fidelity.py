import pathlib
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

# Private by name, imported deliberately: _fill_dedupe_keys is the single
# computation commit_batch and probe_duplicates both use (see its docstring),
# so it is the real dedupe mechanism, not a test-only reimplementation of it.
# It is pure -- no connection, no I/O -- so importing it here costs no
# database.
from db.importing import _fill_dedupe_keys
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

# Any stable account id: content_hash mixes it in, so it must be the SAME for
# both fills being compared or they would differ for the wrong reason.
_DEDUPE_ACCOUNT = UUID("00000000-0000-0000-0000-0000000000a1")


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


# --- Final review, F1: the amendment matcher must be account-AWARE ---------
#
# The rest of the amendment-matcher surface is tested in
# tests/test_fidelity_history.py, against the History dialect. This one case
# cannot live there: the History dialect has no Account Number column at all,
# so every row's account is None and a cross-account collision is not
# expressible. It belongs in this module, beside the other multi-account
# fixtures, because the Activity & Orders dialect is the one that carries the
# column -- and one real export of it spans five distinct account refs.
#
# The hazard is silent DELETION, which is the one outcome D4 ("degrade to
# blocking, never to guessing") does not permit. Account A has a cancel and a
# correction whose own original lies outside this file's window. Account B has
# an ordinary, unrelated buy that happens to share (symbol, date, |quantity|,
# price) with what A's cancel is looking for. Keyed without the account, A's
# cancel adopts B's buy as "its" original, suppresses it, and nets -- B's real
# fill is not blocked and not warned about, it is simply gone, while A gains a
# fill whose original it never had. Before the companion fix that moved the
# suppression `continue` below refs_seen, B disappeared from refs_seen as well,
# so db/importing.py's unregistered-ref check could not see it either.
#
# Same defect family as test_one_correction_cannot_be_consumed_by_two_cancels:
# uniqueness inside a bucket is not uniqueness across a dimension the key
# omits. Every value below is fabricated and checked against the real exports.

_CROSS_ACCOUNT_AMENDMENT_FIXTURE = (
    "Run Date,Account,Account Number,Action,Symbol,Description,Quantity,"
    "Price,Commission,Fees,Amount\n"
    # Account B: an ordinary buy, nothing to do with any amendment. Its date
    # is the as-of date the cancel below quotes, and its symbol, absolute
    # quantity and price are the ones that cancel is matching on -- which is
    # the whole collision.
    "01/02/2026,Individual,A0000002,YOU BOUGHT,ZZZQ,ZZZQ HOLDINGS INC,"
    "3,12.50,0.00,0.00,-37.50\n"
    # Account A: the two amendment legs. A's own original is deliberately
    # absent -- the year-file holding it has not been imported, which is
    # exactly the real shape gap #54 describes.
    "01/21/2026,Individual,A0000001,BUY CANCEL OPENING TRANSACTION CXL "
    "DESCRIPTION CANCELLED TRADE as of 2026-01-02 ZZZQ HOLDINGS INC (Cash),"
    "ZZZQ,ZZZQ HOLDINGS INC,-3,12.50,0.00,0.00,37.50\n"
    "01/21/2026,Individual,A0000001,YOU BOUGHT OPENING TRANSACTION CORR "
    "DESCRIPTION CORRECTED CONFIRM as of 2026-01-02 ZZZQ HOLDINGS INC (Cash),"
    "ZZZQ,ZZZQ HOLDINGS INC,3,12.50,0.00,0.01,-37.51\n"
)


def test_an_amendment_leg_cannot_net_away_another_accounts_fill():
    result = FidelityImporter().parse(_CROSS_ACCOUNT_AMENDMENT_FIXTURE)

    assert not [w for w in result.warnings if "netted" in w.lower()], (
        "A's cancel has no original in THIS account; nothing may net"
    )
    # B's fill survives, intact, with its own date and its own ref.
    survivors = [f for f in result.fills if f.external_ref == "A0000002"]
    assert len(survivors) == 1, "account B's genuine buy must not be suppressed"
    assert survivors[0].quantity == Decimal("3")
    assert survivors[0].executed_at.date().isoformat() == "2026-01-02"
    # And A's two unplaceable legs block, rather than producing a fill.
    assert not [f for f in result.fills if f.external_ref == "A0000001"]
    assert [m for _, m in result.blocking if "CANCEL" in m]
    assert [m for _, m in result.blocking if "CORRECTED CONFIRM" in m]
    # Both accounts stay visible to db/importing.py's unregistered-ref check,
    # including the one whose every row was refused.
    assert set(result.refs_seen) == {"A0000001", "A0000002"}


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
    pattern already applied to the fill branch's Quantity/Price/Commission/Fees.

    Restated for I3: this row is a MATCHED rule (DIVIDEND RECEIVED) that then
    fails on a garbled Amount ("N/A") -- exactly the case I3 closes. Before
    I3's fix this row warned and fell out as unmapped but never reached
    `blocking` at all, because only "no rule matched" ever consulted
    _carries_money; the bad-amount path here was one of the five parallel
    paths that fell out through their own InvalidOperation/non-finite checks
    instead. _carries_money fails open on InvalidOperation ("N/A" cannot be
    parsed, so it is treated as "might carry money" rather than silently
    read as zero), so this row must now also block.

    This does NOT reinstate the original defect being tested here ("one bad
    row must not abort the whole FILE"): parse() still does not raise, the
    two good fills still parse, and blocking's job is to refuse the COMMIT
    (in cli.py, at import time) rather than to raise out of parse() itself --
    see ImportBatch.blocking's docstring. The original assertions (fill/cash/
    unmapped/warning counts, and that "bad amount" is the warning text) are
    kept verbatim; only the new blocking assertion is added."""
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
    assert len(result.blocking) == 1, "a matched rule with a garbled Amount must now block"
    assert result.blocking[0][0] == "X1"
    assert "bad amount" in result.blocking[0][1]


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


def test_investment_gain_loss_does_not_swallow_its_prefix_neighbours():
    """`investment_gain_loss` is INTERNAL -- it asserts a row means nothing --
    so its verb must stay narrow. Broadening it to a bare `INVESTMENT` prefix
    survived every other test in the suite, which is precisely why this one
    exists: the venue emits other `INVESTMENT …` actions, and `INVESTMENT
    ADVISORY FEE` is real money leaving the account.

    Silently classifying a fee as "produces nothing" is the silent-loss shape
    this whole effort exists to close, and an over-broad INTERNAL is the one
    outcome that loses money without even a warning -- an unmapped row at
    least blocks the commit."""
    assert classify("INVESTMENT GAIN/LOSS", "").outcome is Outcome.INTERNAL
    assert classify("INVESTMENT ADVISORY FEE", "") is None
    assert classify("INVESTMENT EXPENSE Q1 2026", "") is None


# One (action, symbol) sample per rule in RULES, using synthetic tickers only,
# each engineered to be the FIRST matching rule for its sample so the
# reachability test below can prove no rule is shadowed by an earlier one.
RULE_COVERAGE_SAMPLES = [
    ("REINVESTMENT MONEY MARKET (SPAXX) (CASH)", "SPAXX"),  # reinvest_sweep
    ("REINVESTMENT ACME CORP (AAA) (CASH)", "AAA"),  # reinvest_security
    ("EXCHANGED TO MONEY MARKET (SPAXX)", "SPAXX"),  # exchange_sweep
    ("INVESTMENT GAIN/LOSS", ""),  # investment_gain_loss
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
    ("ROLLOVER CASH CHECK RECEIVED IRA DIR ROLOVR (Cash)", ""),  # rollover_deposit
    ("EARLY DIST NO EXCEPT VS AAA00-000000-0 CASH (Cash)", ""),  # early_distribution
    ("EXPIRED CALL (ZXCO) ZXCO CORP", "-ZXCO261121C500"),  # expired_option
    ("ASSIGNED CALL (ZXCO) ZXCO CORP", "-ZXCO261121C500"),  # assigned_option
    ("EXERCISED CALL (ZXCO) ZXCO CORP", "-ZXCO261121C500"),  # exercised_option
    # Corporate-action rows -- History dialect only. Hand-crafted independent
    # of tests/fixtures/fidelity/real_shape_history.csv (different fabricated
    # company, CUSIP scheme, and #REOR base) rather than copied from it, same
    # convention as every other sample above -- a copy would move in lockstep
    # with the fixture and stop cross-checking it. All values fabricated.
    ("REVERSE SPLIT R/S TO ACME000009#REOR B1234567890002 ACME HOLDINGS "
     "CORP COM (ACME000010) (Cash)", ""),  # reverse_split
    ("NAME CHANGED N/C TO ACME000011#REOR B1234567890102 ACME RENAMED "
     "INDUSTRIES INC COM (ACME000012) (Cash)", ""),  # name_change
    ("MERGER MER PAYOUT #REOR B1234567890200 ACME LEGACY HOLDINGS CORP "
     "COM (ACME000013) (Cash)", ""),  # merger
    ("DISTRIBUTION SPINOFF FROM:(AAA ) ACME SPINCO NEW WTS EXP "
     "06/30/2027 (Cash)", "AAAWS"),  # spinoff_distribution
    ("IN LIEU OF FRX SHARE FRACTIONAL PAYOUT ACME000009 ACME HOLDINGS "
     "CORP COM (Cash)", "AAA"),  # cash_in_lieu
    # Deliberately NOT "DISTRIBUTION SPINOFF ..." -- that would match
    # spinoff_distribution first and prove nothing about this rule's own
    # reachability, which is the whole point of the ordering guard in
    # importers/fidelity.py's RULES comment.
    ("DISTRIBUTION ACME HOLDINGS SPON ADS EA... (ACME) (Cash)", "ACME"),  # share_distribution
    ("TRANSFER OF ASSETS ACAT DELIVER FAKECO INC COM (ZXCO) (Cash)", "ZXCO"),  # acat_transfer
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


# --- I3: blocking watched only "no rule matched", not a matched row dropped -
# --- for a bad quantity or amount. ------------------------------------------
#
# _carries_money deliberately fails open (returns True) on InvalidOperation --
# "failing open on a garbled money field is exactly the silent-loss failure
# mode this task exists to close" -- but that only protected the "no rule
# matched" branch. A garbled Amount/Quantity on a row that DID match a rule
# fell out through build_fill's or the cash branch's own InvalidOperation/
# zero/non-finite checks, which appended to unmapped + warnings directly and
# never consulted _carries_money at all.


def test_a_bought_row_with_a_blank_quantity_but_a_real_amount_blocks():
    """A YOU BOUGHT row is a MATCHED rule (not an unhandled action) that then
    fails build_fill's zero-quantity guard. Before I3's fix this only warned
    and marked the row unmapped -- blocking stayed empty even though the
    row's own Amount column still carries a real, non-zero dollar figure."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n06/01/2026,X1,YOU BOUGHT,AAA,DESC,,500.00,0.00,0.00,-5000.00\n"
    result = FidelityImporter().parse(row)
    assert result.fills == ()
    assert len(result.unmapped_rows) == 1
    assert result.blocking, "a matched rule with a blank quantity but real money must block"
    assert result.blocking[0][0] == "X1"


def test_renaming_the_quantity_column_recreates_the_original_defect_and_now_blocks():
    """Mutant-gate for I3, from the finding's own demonstration: renaming the
    fixture's Quantity column to Shares reproduces the original silent-loss
    defect bit-for-bit at the parser level -- every fill-shaped row's
    quantity resolves to Decimal('0') via _decimal(None), so all five hit
    build_fill's zero-quantity guard. Each row's Amount column is
    untouched by the rename and still carries real money, so all five must
    now appear in blocking. Before I3's fix this test would see
    len(blocking) == 0 (fills 0, unmapped 5, blocking ()), matching the
    finding's "RC = 0" report verbatim."""
    header = FIXTURE.splitlines()[0].replace("Quantity", "Shares")
    body = "\n".join(FIXTURE.splitlines()[1:])
    result = FidelityImporter().parse(header + "\n" + body + "\n")

    assert result.fills == ()
    assert len(result.unmapped_rows) == 5
    assert len(result.blocking) == 5


# --- Finding A: a bad Run Date silently dropped money -----------------------
#
# The top-level date-parse failure branch appended straight to
# unmapped/warnings and never routed through reject() -- reasoned, when I3
# closed every OTHER unmapped path, as "no rule can have matched yet, so
# there's nothing to be inconsistent with." That reasoning is about RULE
# consistency; it says nothing about money loss. A row whose Run Date fails
# to parse can still carry a real dollar figure in Amount, and that money
# was dropped with only a warning nobody has to read -- exactly the silent-
# loss shape I3 closed for every other path. The money check must not depend
# on the date having parsed: it reads the raw quantity/amount fields
# directly, independent of `when`.


def test_a_bad_run_date_carrying_money_blocks_the_commit():
    """The finding's own reproduction: a $4,321.00 dividend row whose date is
    garbage. Before the fix this fell into the bad-date branch, which never
    consulted _carries_money at all -- blocking stayed empty and --commit
    reported success while silently dropping the row's money."""
    header = FIXTURE.splitlines()[0]
    row = header + "\nNOT-A-DATE,A0000001,DIVIDEND RECEIVED,AAA,DESC,,,,,4321.00\n"
    result = FidelityImporter().parse(row)
    assert result.cash == ()
    assert len(result.unmapped_rows) == 1
    assert any("bad date" in w for w in result.warnings)
    assert result.blocking, "a bad-date row carrying money must block"
    assert result.blocking[0][0] == "A0000001"
    assert "bad date" in result.blocking[0][1]


def test_a_bad_run_date_with_no_money_only_warns_without_blocking():
    """The single most important test in this fix: a bad-date row with NO
    financial content (blank Quantity and blank Amount, same shape as the
    trailing disclaimer block every real export ends with) must still warn
    but must NOT block. Get this wrong -- e.g. by making every bad-date row
    block unconditionally instead of routing through the money-aware
    reject() -- and every real Fidelity export refuses forever, since every
    export ends with a disclaimer block whose date also fails to parse."""
    header = FIXTURE.splitlines()[0]
    row = header + "\nNOT-A-DATE,A0000001,SOME DISCLAIMER TEXT,,,,,,,\n"
    result = FidelityImporter().parse(row)
    assert result.cash == ()
    assert len(result.unmapped_rows) == 1
    assert any("bad date" in w for w in result.warnings)
    assert result.blocking == (), "a bad-date row with no money must not block"


def test_the_actual_trailing_disclaimer_line_still_does_not_block_after_the_fix():
    """Guards the guard against a regression in the other direction on the
    REAL disclaimer shape (no commas at all, so most fields come back None
    from csv.DictReader's restval rather than empty strings) -- not just the
    synthetic no-money row above."""
    result = FidelityImporter().parse(FIXTURE + "This report is for informational purposes only.\n")
    assert result.blocking == ()
    assert result.unmapped_rows


# --- Task 1: the EXPIRY outcome and the closing fill ------------------------
#
# An EXPIRED row already blocks the commit today, via its nonzero Quantity --
# cmd_import refuses the entire file rather than silently dropping just this
# row (cli.py:317-325). What's missing is the correct handling: closing the
# position at zero, rather than leaving the account permanently unimportable.
# Reuses this file's own established convention (a data row built from
# FIXTURE's own header) rather than introducing a second CSV helper.
# Fabricated underlying (ZXCO) per the repo's public-data rule.


def test_expired_short_call_closes_with_a_buy_at_zero():
    """The row describes the POSITION being removed, not a trade direction.
    A negative quantity is a short, and a short is closed by buying it back.
    Reading the sign as a side would open a second short instead of closing
    the first, and the phantom would never go away."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n11/24/2026,X1,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,-1,,,,0.00\n"
    result = FidelityImporter().parse(row)
    (fill,) = result.fills
    assert fill.side is Side.BUY
    assert fill.quantity == Decimal(1)
    assert fill.price == Decimal(0)


def test_expired_long_put_closes_with_a_sell_at_zero():
    header = FIXTURE.splitlines()[0]
    row = header + "\n11/24/2026,X1,EXPIRED PUT (ZXCO) ZXCO CORP,-ZXCO261121P10,,2,,,,0.00\n"
    result = FidelityImporter().parse(row)
    (fill,) = result.fills
    assert fill.side is Side.SELL
    assert fill.quantity == Decimal(2)


def test_expiry_is_dated_from_the_symbol_not_the_run_date():
    """The expiry is the TRUE event date -- the position ceased to exist on
    it -- and `expiry` sits inside instrument_natural_key, so it is the same
    value that mints the instrument. In the real export Fidelity booked an
    expiry three days after it; the dates below reuse that gap for arithmetic
    clarity, not because they fall on a Friday/Monday (2026-11-21 is a
    Saturday).

    This does NOT change any drift `reconcile` reports today, and an earlier
    version of this docstring claimed it did. `open_positions` takes no
    `as_of` and has no date filter, so `--as-of` selects which STATEMENT to
    compare against, never which positions. Dating from the symbol is what
    PREVENTS a phantom-open-across-a-statement-date once position
    reconstruction becomes as-of aware (gap #29)."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n11/24/2026,X1,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,-1,,,,0.00\n"
    result = FidelityImporter().parse(row)
    (fill,) = result.fills
    assert fill.executed_at == datetime(2026, 11, 21, tzinfo=UTC)


def test_expiry_does_not_trip_the_zero_price_guard():
    """The guard exists because downstream of _decimal a missing column and a
    genuine zero are indistinguishable. This path never reads `price` at all,
    so the ambiguity cannot arise. Asserting the absence of the warning is
    what pins the carve-out -- asserting price == 0 alone would still pass if
    the guard fired."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n11/24/2026,X1,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,-1,,,,0.00\n"
    result = FidelityImporter().parse(row)
    assert not any("zero price" in w.lower() for w in result.warnings)


def test_two_same_size_lots_expiring_the_same_day_get_distinct_dedupe_keys():
    """Two identical expiry rows must survive the DEDUPE, which is what the
    occurrence counter in db.importing._fill_dedupe_keys exists for: without
    it both rows hash to the same content_hash and the second is silently
    dropped on commit, losing a lot. The real export's same-day pair differs
    in quantity, so testing against that data alone would never catch this.

    Asserting on `len(parse().fills)` alone would prove NOTHING here -- parse
    performs no dedupe of any kind, so that count stays 2 even with the
    occurrence index deleted outright. The keys are what the commit path
    actually deduplicates on, so this calls the real key function.
    `_fill_dedupe_keys` is pure (it hashes rows; no connection, no I/O), so
    no database is needed to exercise it.

    It is also the only coverage that an EXPIRY fill participates in dedupe
    at all. `_fill_dedupe_keys` is origin-agnostic -- it hashes whatever
    CanonicalFills it is handed, without caring which branch built them -- so
    participation is true by construction; this pins that the construction
    holds for fills the expiry branch produces.
    """
    header = FIXTURE.splitlines()[0]
    data_row = "11/24/2026,X1,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,-3,,,,0.00"
    rows = "\n".join([header, data_row, data_row])
    result = FidelityImporter().parse(rows + "\n")
    assert len(result.fills) == 2

    keys = _fill_dedupe_keys(_DEDUPE_ACCOUNT, result.fills)
    assert len(keys) == 2
    # Neither expiry fill carries a venue_fill_id, so both must take the
    # content_hash arm -- if either took the (venue_fill_id, None) arm the
    # occurrence counter would not be what is keeping them apart, and this
    # test would be pinning the wrong mechanism.
    assert all(venue_id is None and content is not None for venue_id, content in keys)
    assert keys[0][1] != keys[1][1]


def test_expiry_with_an_unparsable_symbol_is_refused_not_guessed():
    header = FIXTURE.splitlines()[0]
    row = header + "\n11/24/2026,X1,EXPIRED SOMETHING ODD,NOTANOPTION,,-1,,,,0.00\n"
    result = FidelityImporter().parse(row)
    assert result.fills == ()
    assert any("option symbol" in w for w in result.warnings)


def test_expiry_with_zero_quantity_is_refused_not_guessed():
    """Neither direction nor size is knowable, and guessing either is how you
    get a plausible wrong number."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n11/24/2026,X1,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,0,,,,0.00\n"
    result = FidelityImporter().parse(row)
    assert result.fills == ()
    assert any("quantity" in w for w in result.warnings)


# --- Task 2: UNSUPPORTED -- refuse assignment and exercise loudly -----------
#
# Scope is deliberately expiry-only. A realistic ASSIGNED/EXERCISED row
# already blocks today via its nonzero Quantity (an unmapped row blocks
# whenever it carries a nonzero Quantity OR Amount -- reject ->
# _carries_money). UNSUPPORTED earns its place anyway: it names the verb in
# the refusal instead of a generic "unhandled action", and it blocks
# unconditionally, independent of what the row's money columns happen to
# hold, rather than depending on Quantity staying nonzero. That is defence
# in depth and a better error message, not the only thing standing between
# an assignment and a silent drop.


def test_an_assigned_option_blocks_the_commit_even_with_no_money_on_the_row():
    """Scope is expiry-only, which stays safe only if the refusal does not
    depend on the row's money columns. A realistic assignment row already
    blocks via its nonzero Quantity; this row deliberately leaves BOTH
    Quantity and Amount blank to isolate that independence -- pinning that
    UNSUPPORTED blocks because the verb is recognised and refused, not
    because some column happened to be nonzero."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n11/24/2026,X1,ASSIGNED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,,,,,0.00\n"
    batch = FidelityImporter().parse(row)
    assert batch.fills == ()
    assert batch.blocking != ()
    assert any("ASSIGNED" in message for _ref, message in batch.blocking)


def test_an_exercised_option_blocks_the_commit():
    header = FIXTURE.splitlines()[0]
    row = header + "\n11/24/2026,X1,EXERCISED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,,,,,0.00\n"
    batch = FidelityImporter().parse(row)
    assert batch.fills == ()
    assert any("EXERCISED" in message for _ref, message in batch.blocking)


def test_an_expiry_does_not_block():
    """The counterpart assertion: the two outcomes must not be conflated."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n11/24/2026,X1,EXPIRED CALL (ZXCO) ZXCO CORP,-ZXCO261121C500,,-1,,,,0.00\n"
    batch = FidelityImporter().parse(row)
    assert batch.blocking == ()
    assert len(batch.fills) == 1


def test_every_outcome_member_has_a_dispatch_branch():
    """The dispatch used to end in a bare `# rule.outcome is Outcome.CASH`
    fallthrough, so a new Outcome with no branch would be silently treated as
    a cash movement. This pins that it cannot happen again."""
    for rule in RULES:
        assert rule.outcome in {
            Outcome.FILL,
            Outcome.CASH,
            Outcome.INTERNAL,
            Outcome.EXPIRY,
            Outcome.TRANSFER,
            Outcome.UNSUPPORTED,
            Outcome.CORPORATE_ACTION,
        }


# --- TRANSFER OF ASSETS ACAT (branch B): the share leg becomes an
# --- asset-transfer write, the cash residual a transfer_out movement, and any
# --- inbound-looking shape refuses the file. Shapes mirror the real rows by
# --- sign and column; every value is invented.

_HISTORY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Price ($),Quantity,"
    "Commission ($),Fees ($),Accrued Interest ($),Amount ($),"
    "Cash Balance ($),Settlement Date"
)


def _history(*rows):
    return "\n".join([_HISTORY_HEADER, *rows]) + "\n"


def test_acat_share_delivery_becomes_a_transfer():
    batch = FidelityImporter().parse(
        _history(
            '03/11/2026,TRANSFER OF ASSETS ACAT DELIVER FAKECO INC COM'
            ' (ZXCO) (Cash),ZXCO,FAKECO INC COM,Cash,"","-40","","","","-259.2",0,""'
        )
    )
    assert batch.blocking == ()
    assert len(batch.transfers) == 1
    t = batch.transfers[0]
    assert t.quantity == Decimal("40")
    assert t.market_value == Decimal("259.2")
    assert t.instrument.symbol == "ZXCO"
    assert batch.fills == ()
    assert batch.cash == ()


def test_acat_cash_residual_becomes_transfer_out_cash():
    batch = FidelityImporter().parse(
        _history(
            '03/11/2026,TRANSFER OF ASSETS ACAT DELIVER (Cash),"",'
            'No Description,Cash,"",0,"","","","-114.37",0,""'
        )
    )
    assert batch.blocking == ()
    assert batch.transfers == ()
    assert len(batch.cash) == 1
    c = batch.cash[0]
    assert c.kind == "transfer_out"
    assert c.amount == Decimal("114.37")


def test_inbound_shaped_acat_blocks_the_import():
    batch = FidelityImporter().parse(
        _history(
            '03/11/2026,TRANSFER OF ASSETS ACAT RECEIVE FAKECO INC COM'
            ' (ZXCO) (Cash),ZXCO,FAKECO INC COM,Cash,"","40","","","","259.2",0,""'
        )
    )
    assert batch.transfers == ()
    assert batch.cash == ()
    assert len(batch.blocking) == 1
    _ref, reason = batch.blocking[0]
    assert "TRANSFER OF ASSETS" in reason
    assert "inbound" in reason.lower()


def test_zero_money_acat_row_warns_without_blocking():
    """A TRANSFER OF ASSETS row that moves nothing (zero quantity, zero
    amount) is unmapped-but-harmless, the same policy _carries_money enforces
    everywhere else -- it must warn, not refuse the whole file with a message
    claiming it is inbound-shaped."""
    batch = FidelityImporter().parse(
        _history(
            '03/11/2026,TRANSFER OF ASSETS ACAT DELIVER MEMO (Cash),"",'
            'No Description,Cash,"",0,"","","",0,0,""'
        )
    )
    assert batch.blocking == ()
    assert batch.transfers == ()
    assert batch.cash == ()
    assert any("TRANSFER OF ASSETS" in w for w in batch.warnings)


def test_blank_symbol_row_is_identified_by_its_isin():
    """Issue #27. `instrument.symbol` is TEXT NOT NULL but the EMPTY STRING was
    never forbidden, and because instrument_natural_key derives from the
    symbol, every blank-symbol row for ANY security collapsed onto one
    `equity::USD` instrument -- on the real ledger, 17 fills across 2022-2025
    and three distinct securities, presenting as one nameless-but-valuable
    position with unvaluable_reason NULL.

    The ISIN is preferred over the description because it survives the renames
    that a reorganisation or reverse split inflicts on a listing."""
    batch = FidelityImporter().parse(
        _history(
            '03/11/2026,YOU BOUGHT,"",FAKECO MINING INC COM NEW ISIN ZZ000000BBB2,'
            'Cash,0.61,164,"","","",-100.04,0,""'
        )
    )
    assert batch.blocking == ()
    assert len(batch.fills) == 1
    assert batch.fills[0].instrument.symbol == "ISIN:ZZ000000BBB2"
    assert any("blank symbol" in w for w in batch.warnings)


def test_two_isins_for_one_company_stay_two_instruments():
    """The whole point of keying on the ISIN. One issuer in the real
    ledger carries three ISINs from two successive identity changes; a
    description-derived key would merge them back into one row and reintroduce
    issue #27 in a subtler form, since the descriptions differ only by a
    suffix. ISINs below are invented."""
    batch = FidelityImporter().parse(
        _history(
            '03/11/2026,YOU BOUGHT,"",FAKECO MINING INC COM NPV ISIN ZZ000000AAA1,'
            'Cash,0.61,164,"","","",-100.04,0,""',
            '03/12/2026,YOU BOUGHT,"",FAKECO MINING INC COM NEW ISIN ZZ000000BBB2,'
            'Cash,0.46,215,"","","",-98.90,0,""',
        )
    )
    assert batch.blocking == ()
    assert {f.instrument.symbol for f in batch.fills} == {
        "ISIN:ZZ000000AAA1",
        "ISIN:ZZ000000BBB2",
    }


def test_blank_symbol_without_an_isin_falls_back_to_the_description():
    """4 of the real ledger's 17 blank-symbol rows carry no ISIN -- they are a
    reverse-split notice -- but all four share one identical description, so a
    description-derived key groups them correctly rather than refusing rows
    that are perfectly identifiable to a human."""
    row = (
        '03/11/2026,YOU BOUGHT,"",FAKE TR II SOMEFUND OP 1 FOR 5 R/S INTO FAKE TRUST II,'
        'Cash,22.53,20,"","","",-450.60,0,""'
    )
    batch = FidelityImporter().parse(_history(row, row.replace("03/11", "03/12")))
    assert batch.blocking == ()
    assert len(batch.fills) == 2
    symbols = {f.instrument.symbol for f in batch.fills}
    assert len(symbols) == 1, "identical descriptions must yield ONE instrument"
    assert symbols.pop().startswith("DESC:FAKE TR II SOMEFUND")


def test_blank_symbol_and_blank_description_is_refused():
    """The one genuinely unnameable case. Inventing a name here would be worse
    than refusing: it would mint an instrument nobody can ever identify."""
    batch = FidelityImporter().parse(
        _history('03/11/2026,YOU BOUGHT,"","",Cash,0.61,164,"","","",-100.04,0,""')
    )
    assert batch.fills == ()
    assert len(batch.blocking) == 1
    assert "blank symbol" in batch.blocking[0][1]


def test_a_named_fill_row_is_still_accepted():
    """The derivation must not disturb ordinary rows."""
    batch = FidelityImporter().parse(
        _history(
            '03/11/2026,YOU BOUGHT,ZXCO,FAKECO INC COM,Cash,0.61,164,'
            '"","","",-100.04,0,""'
        )
    )
    assert batch.blocking == ()
    assert len(batch.fills) == 1
    assert batch.fills[0].instrument.symbol == "ZXCO"
    assert not any("blank symbol" in w for w in batch.warnings)
