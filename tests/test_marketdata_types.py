"""The one conversion every provider goes through.

Providers return binary floats. This repo's rule is NUMERIC never float, so
the boundary where a float becomes a Decimal is load-bearing and gets its own
tests.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from marketdata.types import Quote, quantize_price


def test_a_provider_float_is_quantized_not_stored_raw():
    """Yahoo returns binary floats: 0.53 arrives as 0.5299999713897705, and
    4.44 as 4.440000057220459. Both are real values recorded from a live
    fast_info on 2026-09-16. Decimal(float) would carry every one of those
    digits into a NUMERIC column."""
    assert quantize_price(0.5299999713897705) == Decimal("0.5300")
    assert quantize_price(4.440000057220459) == Decimal("4.4400")


def test_quantization_keeps_sub_cent_precision():
    """Sub-penny prices are real -- DEMCF quoted 0.0795 on 2026-09-16.
    Rounding to 2dp turns that into 0.08, a 0.6% error on the position."""
    assert quantize_price(0.0795) == Decimal("0.0795")


def test_a_negative_quote_is_refused():
    """mark_price_chk is `price >= 0`. A negative quote is a provider fault
    or a mis-parse; it must not reach the API as a proposal."""
    with pytest.raises(ValueError, match="negative"):
        quantize_price(-1.0)


def test_a_non_finite_quote_is_refused():
    """NaN and Infinity both survive a naive `>= 0` check -- migration 002
    exists because they reached NUMERIC columns once already."""
    with pytest.raises(ValueError, match="non-finite"):
        quantize_price(float("inf"))
    with pytest.raises(ValueError, match="non-finite"):
        quantize_price(float("nan"))


def test_zero_is_a_legal_quote():
    """An expired option is worth zero, and that is a real mark rather than a
    missing one. Refusing zero here would make it un-recordable."""
    assert quantize_price(0.0) == Decimal("0.0000")


def test_quote_carries_its_source_and_time():
    q = Quote(
        symbol="GME",
        price=Decimal("21.44"),
        currency="USD",
        quoted_at=datetime(2026, 9, 16, 20, 0, tzinfo=UTC),
        source="yahoo",
    )
    assert q.source == "yahoo"
    assert q.quoted_at.tzinfo is not None
