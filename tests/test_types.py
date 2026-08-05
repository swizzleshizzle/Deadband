from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from ledger.types import (
    AssetClass,
    Fill,
    FillSource,
    Instrument,
    Side,
    instrument_natural_key,
)

ACC = UUID("00000000-0000-0000-0000-0000000000a1")


def make_fill(side: Side, qty: str, price: str) -> Fill:
    return Fill(
        id=uuid4(),
        account_id=ACC,
        instrument_id=UUID("00000000-0000-0000-0000-0000000000b1"),
        executed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=None,
        is_estimated=False,
    )


def test_buy_has_positive_signed_quantity():
    assert make_fill(Side.BUY, "1.5", "100").signed_quantity == Decimal("1.5")


def test_sell_has_negative_signed_quantity():
    assert make_fill(Side.SELL, "1.5", "100").signed_quantity == Decimal("-1.5")


def test_negative_quantity_is_rejected():
    with pytest.raises(ValueError, match="positive"):
        make_fill(Side.BUY, "-1", "100")


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        Fill(
            id=None,
            account_id=ACC,
            instrument_id=ACC,
            executed_at=datetime(2026, 8, 1, 12, 0),  # naive
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("1"),
            fee=Decimal("0"),
            fee_currency="USD",
            source=FillSource.MANUAL,
            venue_fill_id=None,
            is_estimated=False,
        )


def test_equity_natural_key():
    inst = Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD")
    assert instrument_natural_key(inst) == "equity:SPY:USD"


def test_option_natural_key_includes_all_contract_terms():
    inst = Instrument(
        id=None,
        asset_class=AssetClass.OPTION,
        symbol="SPY 26SEP19 500 C",
        quote_currency="USD",
        underlying="SPY",
        strike=Decimal("500"),
        expiry=datetime(2026, 9, 19, tzinfo=UTC).date(),
        option_right="call",
        contract_multiplier=Decimal("100"),
    )
    assert instrument_natural_key(inst) == "option:SPY:2026-09-19:500:call:USD"


def test_option_natural_key_is_stable_across_strike_formatting():
    """500 and 500.00 are the same strike and must not create two instruments."""
    base = dict(
        id=None,
        asset_class=AssetClass.OPTION,
        symbol="SPY 26SEP19 500 C",
        quote_currency="USD",
        underlying="SPY",
        expiry=datetime(2026, 9, 19, tzinfo=UTC).date(),
        option_right="call",
        contract_multiplier=Decimal("100"),
    )
    a = instrument_natural_key(Instrument(**base, strike=Decimal("500")))
    b = instrument_natural_key(Instrument(**base, strike=Decimal("500.00")))
    assert a == b


def test_onchain_natural_key_lowercases_address():
    inst = Instrument(
        id=None,
        asset_class=AssetClass.CRYPTO_SPOT,
        symbol="WETH",
        quote_currency="USD",
        chain="ethereum",
        contract_address="0xC02AAA39B223FE8D0A0E5C4F27EAD9083C756CC2",
    )
    key = instrument_natural_key(inst)
    assert key == "crypto_spot:ethereum:0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2:USD"


def test_delimiter_collision_is_prevented():
    """Colons in symbol/quote_currency are escaped to prevent key collisions."""
    # These two different instruments previously produced the same key.
    # Now they must be distinct after escaping.
    inst1 = Instrument(
        id=None,
        asset_class=AssetClass.CRYPTO_PERP,
        symbol="BTC:PERP",
        quote_currency="USD",
    )
    inst2 = Instrument(
        id=None,
        asset_class=AssetClass.CRYPTO_PERP,
        symbol="BTC",
        quote_currency="PERP:USD",
    )
    key1 = instrument_natural_key(inst1)
    key2 = instrument_natural_key(inst2)
    assert key1 != key2, f"collision: {key1} == {key2}"


def test_normalize_decimal_avoids_scientific_notation():
    """Small magnitude decimals like 1E-7 must not emit exponent notation."""
    from ledger.types import _normalize_decimal

    result = _normalize_decimal(Decimal("1E-7"))
    assert "E" not in result, f"result should not contain exponent, got: {result}"
    assert result == "0.0000001"


def test_non_utc_timestamp_is_normalized_to_utc():
    """A timestamp with a +10:00 offset is stored as the equivalent UTC instant."""
    # Create a fill with UTC+10:00 (e.g. Brisbane, which does not observe DST)
    utc_plus_10 = timezone(timedelta(hours=10))
    local_time = datetime(2026, 8, 1, 22, 0, tzinfo=utc_plus_10)  # 22:00 in +10:00
    # This is 12:00 UTC
    fill = Fill(
        id=None,
        account_id=ACC,
        instrument_id=ACC,
        executed_at=local_time,
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("1"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=None,
        is_estimated=False,
    )
    # The fill should be stored with UTC timezone
    assert fill.executed_at.tzinfo is UTC
    # And the hour should be 12 (UTC equivalent)
    assert fill.executed_at.hour == 12


def test_naive_timestamp_still_rejected():
    """Naive timestamps are rejected; normalization only applies to aware timestamps."""
    with pytest.raises(ValueError, match="timezone-aware"):
        Fill(
            id=None,
            account_id=ACC,
            instrument_id=ACC,
            executed_at=datetime(2026, 8, 1, 12, 0),  # naive, no tzinfo
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("1"),
            fee=Decimal("0"),
            fee_currency="USD",
            source=FillSource.MANUAL,
            venue_fill_id=None,
            is_estimated=False,
        )


def test_onchain_with_none_chain_uses_empty_string_sentinel():
    """When chain is None, empty string is used (not "unknown")."""
    inst_no_chain = Instrument(
        id=None,
        asset_class=AssetClass.CRYPTO_SPOT,
        symbol="USDC",
        quote_currency="USD",
        chain=None,  # explicitly None
        contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    )
    inst_unknown = Instrument(
        id=None,
        asset_class=AssetClass.CRYPTO_SPOT,
        symbol="USDC",
        quote_currency="USD",
        chain="unknown",  # explicitly "unknown"
        contract_address="0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    )
    key_none = instrument_natural_key(inst_no_chain)
    key_unknown = instrument_natural_key(inst_unknown)
    assert key_none != key_unknown, "None and 'unknown' chain must produce different keys"


def test_future_natural_key():
    """FUTURE instruments produce a key with root and expiry."""
    inst = Instrument(
        id=None,
        asset_class=AssetClass.FUTURE,
        symbol="ES",  # not used in futures key
        quote_currency="USD",
        root="ES",
        expiry=datetime(2026, 12, 18, tzinfo=UTC).date(),
    )
    key = instrument_natural_key(inst)
    assert key == "future:ES:2026-12-18:USD"
