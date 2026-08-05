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
