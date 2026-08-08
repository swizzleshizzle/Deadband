# tests/test_coinbase_api.py
import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from importers.coinbase_api import CoinbaseAPIImporter
from ledger.types import AssetClass, Side

_FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
FIXTURE = (_FIXTURES / "coinbase" / "api_fills.json").read_text()


def batch():
    return CoinbaseAPIImporter().parse(FIXTURE)


def test_a_fill_maps_with_the_venue_trade_id_as_its_dedupe_key():
    f = batch().fills[0]
    assert f.venue_fill_id == "t1"
    assert f.venue_order_id == "o1"
    assert f.instrument.symbol == "BTC"
    assert f.instrument.quote_currency == "USD"
    assert f.instrument.asset_class is AssetClass.CRYPTO_SPOT
    assert f.side is Side.BUY
    assert f.quantity == Decimal("0.0125")
    assert f.price == Decimal("61250.44")
    assert f.fee == Decimal("3.83")


def test_executed_at_is_trade_time_not_sequence_timestamp():
    """They differ by ~half a second in the fixture, deliberately. Reading
    the wrong one shifts every fill by an amount too small to notice and
    large enough to reorder same-second trades."""
    f = batch().fills[0]
    assert f.executed_at == datetime(2026, 5, 11, 14, 3, 21, 512000, tzinfo=UTC)


def test_uppercase_side_is_understood():
    assert batch().fills[1].side is Side.SELL


def test_size_in_quote_blocks_rather_than_recording_a_wrong_quantity():
    """`size` is in QUOTE currency when this flag is set, so importing it as
    a base quantity would record 500 units of the asset instead of $500 of
    it. No safe conversion exists from the fill alone."""
    b = batch()
    assert not [f for f in b.fills if f.venue_fill_id == "t3"]
    assert [ref for ref, _ in b.blocking] == [None]
    assert "size_in_quote" in b.blocking[0][1]


def test_money_fields_never_become_floats():
    """Coinbase quotes its money fields today. If it ever stops for one of
    them, parse_float=Decimal is what keeps a float out of the pipeline."""
    unquoted = FIXTURE.replace('"price": "61250.44"', '"price": 61250.44')
    f = CoinbaseAPIImporter().parse(unquoted).fills[0]
    assert isinstance(f.price, Decimal)
    assert f.price == Decimal("61250.44")


def test_an_empty_document_is_empty_not_an_error():
    assert CoinbaseAPIImporter().parse('{"fills": [], "cursor": ""}').fills == ()
