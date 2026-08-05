from datetime import UTC, datetime
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
