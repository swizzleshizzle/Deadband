"""Ledger instrument -> provider symbol.

The riskiest pure function in this slice: a near-miss symbol that happens to
resolve returns a DIFFERENT security's price into a P&L figure, and nothing
downstream can detect that. Every case here is either an exact expected
symbol or a refusal.
"""

from datetime import date
from decimal import Decimal

import pytest

from marketdata.symbols import UnmappableInstrument, provider_symbol


def _equity(symbol):
    return dict(
        asset_class="equity", symbol=symbol, underlying=None,
        expiry=None, strike=None, option_right=None,
    )


def test_an_equity_maps_to_its_own_ticker():
    assert provider_symbol(**_equity("GME")) == "GME"


def test_an_otc_ticker_maps_unchanged():
    """DEMCF (OTC) and NPSCY (Pink Sheets) both resolved on Yahoo when probed
    2026-09-16 -- no suffix, no exchange prefix, no special casing."""
    assert provider_symbol(**_equity("DEMCF")) == "DEMCF"
    assert provider_symbol(**_equity("NPSCY")) == "NPSCY"


def test_surrounding_whitespace_is_stripped_and_case_normalised():
    assert provider_symbol(**_equity("  gme  ")) == "GME"


def test_an_option_is_built_into_occ_form():
    """The ledger stores Fidelity's spelling (-PYPL280121C100). Yahoo wants
    OCC: root + YYMMDD + C/P + strike x1000 padded to 8. Verified live on
    2026-09-16 -- PYPL280121C00100000 quoted 0.53."""
    assert provider_symbol(
        asset_class="option", symbol="-PYPL280121C100", underlying="PYPL",
        expiry=date(2028, 1, 21), strike=Decimal("100"), option_right="call",
    ) == "PYPL280121C00100000"


def test_a_fractional_strike_survives_the_occ_encoding():
    """Strike 12.50 is 00012500 -- not 00012.50, not 0001250. An off-by-one
    names a different contract that may well exist and quote."""
    assert provider_symbol(
        asset_class="option", symbol="x", underlying="AAPL",
        expiry=date(2026, 6, 19), strike=Decimal("12.50"), option_right="put",
    ) == "AAPL260619P00012500"


def test_a_put_and_a_call_differ_only_in_the_right():
    """Pins that the right is read at all. A hardcoded 'C' passes every other
    option test in this file."""
    kw = dict(asset_class="option", symbol="x", underlying="AAPL",
              expiry=date(2026, 6, 19), strike=Decimal("100"))
    call = provider_symbol(**kw, option_right="call")
    put = provider_symbol(**kw, option_right="put")
    assert call == "AAPL260619C00100000"
    assert put == "AAPL260619P00100000"


def test_a_high_strike_does_not_overflow_the_field():
    """8 digits holds strikes up to 99,999.999. A 5-figure strike is real on
    index options."""
    assert provider_symbol(
        asset_class="option", symbol="x", underlying="SPX",
        expiry=date(2026, 12, 18), strike=Decimal("6500"), option_right="call",
    ) == "SPX261218C06500000"


def test_a_blank_symbol_is_unmappable_with_a_reason():
    """The merged blank-symbol instrument (known-gap #77) is still in the
    live ledger and appears on the marks screen."""
    with pytest.raises(UnmappableInstrument, match="no symbol"):
        provider_symbol(**_equity(""))
    with pytest.raises(UnmappableInstrument, match="no symbol"):
        provider_symbol(**_equity("   "))


def test_an_option_missing_its_parts_is_unmappable_not_guessed():
    """Falling back to the Fidelity spelling would send -PYPL280121C100 to a
    provider that has never heard of it -- or worse, one that resolves it to
    something else."""
    with pytest.raises(UnmappableInstrument, match="option"):
        provider_symbol(
            asset_class="option", symbol="-PYPL280121C100", underlying=None,
            expiry=None, strike=None, option_right=None,
        )


def test_an_unsupported_asset_class_is_unmappable():
    with pytest.raises(UnmappableInstrument, match="crypto_perp"):
        provider_symbol(
            asset_class="crypto_perp", symbol="BTC-PERP", underlying=None,
            expiry=None, strike=None, option_right=None,
        )
