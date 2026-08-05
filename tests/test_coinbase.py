import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from importers.coinbase import CoinbaseImporter
from ledger.types import AssetClass, Side

FIXTURE = pathlib.Path("tests/fixtures/coinbase/transactions.csv").read_text()


def batch():
    return CoinbaseImporter().parse(FIXTURE)


def test_buys_and_sells_become_fills():
    fills = batch().fills
    assert len(fills) == 3
    assert [f.side for f in fills] == [Side.BUY, Side.BUY, Side.SELL]


def test_fill_fields_are_mapped():
    f = batch().fills[0]
    assert f.executed_at == datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
    assert f.quantity == Decimal("0.50000000")
    assert f.price == Decimal("61200.00")
    assert f.fee == Decimal("153.00")
    assert f.fee_currency == "USD"
    assert f.instrument.asset_class is AssetClass.CRYPTO_SPOT
    assert f.instrument.symbol == "BTC"
    assert f.instrument.quote_currency == "USD"


def test_deposits_become_cash_movements():
    cash = [c for c in batch().cash if c.kind == "deposit"]
    assert len(cash) == 1
    assert cash[0].amount == Decimal("5000.00")


def test_rewards_become_interest_cash_movements():
    cash = [c for c in batch().cash if c.kind == "interest"]
    assert len(cash) == 1
    assert cash[0].symbol == "ETH"
    assert cash[0].amount == Decimal("32.00")


def test_unhandled_row_types_are_reported_not_silently_dropped():
    result = batch()
    assert any("Convert" in w for w in result.warnings)
    assert len(result.unmapped_rows) == 1


def test_empty_input_yields_empty_batch():
    result = CoinbaseImporter().parse("")
    assert result.fills == ()
    assert result.cash == ()


def test_header_only_input_yields_empty_batch():
    header = FIXTURE.splitlines()[0]
    assert CoinbaseImporter().parse(header + "\n").fills == ()


def test_malformed_row_is_warned_about_and_skipped():
    bad = FIXTURE.splitlines()[0] + "\n2026-01-15T14:30:00Z,Buy,BTC,notanumber,USD,1,1,1,0,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert len(result.warnings) == 1


def test_utf8_bom_is_stripped():
    """UTF-8 BOM (U+FEFF) at start of file should not break parsing."""
    with_bom = "﻿" + FIXTURE
    result = CoinbaseImporter().parse(with_bom)
    result_no_bom = batch()
    # Both should parse identically
    assert len(result.fills) == len(result_no_bom.fills) == 3
    assert result.fills[0].quantity == result_no_bom.fills[0].quantity
    assert result.fills[0].price == result_no_bom.fills[0].price


def test_non_z_offset_timestamps_are_normalized_to_utc():
    """A timestamp with -05:00 offset should be converted to the equivalent UTC instant."""
    # 2026-01-15T14:30:00-05:00 is 2026-01-15T19:30:00Z in UTC
    csv_with_offset = (
        FIXTURE.splitlines()[0]
        + "\n2026-01-15T14:30:00-05:00,Buy,BTC,0.50000000,USD,61200.00,"
        + "30600.00,30753.00,153.00,Test\n"
    )
    result = CoinbaseImporter().parse(csv_with_offset)
    assert len(result.fills) == 1
    ts = result.fills[0].executed_at
    # Should be converted to UTC (tzinfo must be UTC, not a fixed offset)
    assert ts.tzinfo is UTC
    # And represent the same instant: 19:30 UTC
    assert ts.hour == 19
    assert ts.minute == 30


def test_zero_price_on_non_fiat_cash_is_warned_about():
    """A reward in non-fiat currency (e.g., ETH) with no spot price should warn."""
    csv_with_zero_price = (
        FIXTURE.splitlines()[0]
        + "\n2026-03-01T00:00:00Z,Rewards Income,ETH,0.01000000,USD,,0,0,0,Staking reward\n"
    )
    result = CoinbaseImporter().parse(csv_with_zero_price)
    # Should still create the cash movement
    assert len(result.cash) == 1
    # But should warn about missing price
    assert len(result.warnings) == 1
    assert "no spot price" in result.warnings[0]
    assert result.warnings[0].startswith("line 2:")
    # Amount should be 0 (0.01 * 0)
    assert result.cash[0].amount == Decimal("0")
