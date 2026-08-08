# tests/test_coinbase_api.py
import pathlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest

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


# Two rows that fail for the SAME reason (size_in_quote) and both carry no
# trade_id at all -- the case where trade_id alone cannot tell them apart, so
# the fill's position in the document (`idx`) is the only thing that can.
_TWO_UNIDENTIFIABLE_BAD_ROWS = """
{
  "fills": [
    {"trade_time": "2026-05-01T00:00:00Z", "sequence_timestamp": "2026-05-01T00:00:00Z",
     "price": "1", "size": "1", "size_in_quote": true, "commission": "0",
     "product_id": "BTC-USD", "side": "BUY"},
    {"trade_time": "2026-05-02T00:00:00Z", "sequence_timestamp": "2026-05-02T00:00:00Z",
     "price": "2", "size": "2", "size_in_quote": true, "commission": "0",
     "product_id": "ETH-USD", "side": "SELL"}
  ],
  "cursor": ""
}
"""


def test_blocking_messages_distinguish_two_rows_that_fail_the_same_way():
    """`reject()` accepted `idx` from every call site but never used it, so
    two different malformed rows that both lack a trade_id -- the case where
    trade_id cannot disambiguate them -- produced byte-identical blocking
    messages. With more than one bad row in a batch, a human reading
    `blocking` could not tell which row was which. `idx`, embedded in the
    message, is what fixes that: it is the JSON analogue of the CSV
    importers' `line N`."""
    b = CoinbaseAPIImporter().parse(_TWO_UNIDENTIFIABLE_BAD_ROWS)
    messages = [msg for _, msg in b.blocking]
    assert len(messages) == 2
    assert messages[0] != messages[1]
    assert "fill 0" in messages[0]
    assert "fill 1" in messages[1]


def test_product_id_whitespace_is_stripped_like_side_is():
    """`side` is read with `.strip()` two lines above where `product_id` is
    parsed; product_id previously was not -- an invariant applied correctly
    in one place and not its twin. Incidental whitespace around product_id
    would otherwise pollute the instrument's symbol/quote_currency (e.g.
    symbol " BTC" instead of "BTC") rather than being normalized away or
    causing a rejection."""
    padded = FIXTURE.replace('"product_id": "BTC-USD"', '"product_id": " BTC-USD "')
    f = next(x for x in CoinbaseAPIImporter().parse(padded).fills if x.venue_fill_id == "t1")
    assert f.instrument.symbol == "BTC"
    assert f.instrument.quote_currency == "USD"


def test_money_fields_never_become_floats():
    """Coinbase quotes its money fields today. If it ever stops for one of
    them, parse_float=Decimal is what keeps a float out of the pipeline.

    This test previously targeted the BTC fill's `price: "61250.44"` (7
    significant figures) and only asserted `isinstance(f.price, Decimal)`.
    That value round-trips exactly through `float`:
    `str(float("61250.44")) == "61250.44"`. Because `_decimal()` builds
    every Decimal via `Decimal(str(raw))` regardless of `raw`'s original
    type, that round-trip meant the mutant with `parse_float=Decimal`
    removed still produced a `Decimal` equal to the expected value -- the
    assertion could not fail no matter which code path built it. That is
    the "assertion that cannot fail" defect this project has shipped
    before: a green test watching nothing.

    `float` holds ~15-17 significant decimal digits. The fix is a fixture
    value with more precision than that, in a shape Coinbase genuinely
    produces: a high-supply token (SHIB/PEPE-style) trading in the billions
    of units at 8 decimal places. `t4`'s `size`, "1234567890.12345678", has
    18 significant figures and is genuinely float-lossy --
    `float("1234567890.12345678")` rounds to `1234567890.1234567`, a
    different number -- so this assertion goes red when
    `parse_float=Decimal` is removed. Do not shrink this back to a short,
    round-tripping literal; that is exactly what made the original version
    decorative.
    """
    unquoted = FIXTURE.replace(
        '"size": "1234567890.12345678"', '"size": 1234567890.12345678'
    )
    fills = CoinbaseAPIImporter().parse(unquoted).fills
    f = next(x for x in fills if x.venue_fill_id == "t4")
    assert isinstance(f.quantity, Decimal)
    assert f.quantity == Decimal("1234567890.12345678")


def test_an_empty_document_is_empty_not_an_error():
    assert CoinbaseAPIImporter().parse('{"fills": [], "cursor": ""}').fills == ()


def test_a_document_missing_the_fills_key_raises_rather_than_being_treated_as_empty():
    """`document.get("fills") or []` would fold a document with NO `fills`
    key at all into the same zero-rows result as a genuinely empty account
    (see test_an_empty_document_is_empty_not_an_error, which keeps
    `{"fills": []}` working as the real empty case). A missing key means
    something upstream is truncated or shaped wrong -- e.g. the venue
    client's own JSON, malformed -- and a parse that returns an empty,
    successful-looking ImportBatch for that is exactly the 'sync reports
    success having fetched nothing' shape spec §10 gap 5 names. It must
    raise instead."""
    with pytest.raises(ValueError, match="fills"):
        CoinbaseAPIImporter().parse('{"cursor": ""}')


def test_a_non_list_fills_value_raises_rather_than_being_treated_as_empty():
    """Same defect, different malformed shape: `fills` present but not a
    list (e.g. a server error body accidentally matching the field name).
    `or []` only guards against a falsy `fills` (None, missing) -- a
    truthy non-list value like a string or dict would sail past it into
    `enumerate()`, which either explodes confusingly or, for a dict,
    iterates its keys as if they were fill records."""
    with pytest.raises(ValueError, match="fills"):
        CoinbaseAPIImporter().parse('{"fills": "not-a-list", "cursor": ""}')
