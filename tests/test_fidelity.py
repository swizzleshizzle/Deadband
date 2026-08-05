import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from importers.fidelity import FidelityImporter, parse_option_symbol
from ledger.types import AssetClass, Side

FIXTURE = pathlib.Path("tests/fixtures/fidelity/activity.csv").read_text()


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
