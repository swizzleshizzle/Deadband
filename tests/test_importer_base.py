from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from importers.base import content_hash

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
T = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)


def test_hash_is_stable_across_calls():
    args = (ACC, T, "SPY", "buy", Decimal("10"), Decimal("500.00"))
    assert content_hash(*args) == content_hash(*args)


def test_hash_ignores_decimal_formatting():
    """500 and 500.00 are the same price; they must not defeat deduplication."""
    a = content_hash(ACC, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    b = content_hash(ACC, T, "SPY", "buy", Decimal("10.0"), Decimal("500.00"))
    assert a == b


def test_hash_changes_when_any_field_changes():
    base = content_hash(ACC, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    assert content_hash(ACC, T, "SPY", "sell", Decimal("10"), Decimal("500")) != base
    assert content_hash(ACC, T, "QQQ", "buy", Decimal("10"), Decimal("500")) != base
    assert content_hash(ACC, T, "SPY", "buy", Decimal("11"), Decimal("500")) != base


def test_hash_is_account_scoped():
    other = UUID("00000000-0000-0000-0000-0000000000a2")
    a = content_hash(ACC, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    b = content_hash(other, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    assert a != b


def test_hash_precision_pinning_distinguishes_quantities():
    """Two quantities that differ only beyond low precision must produce different hashes.

    Without precision pinning at 50, a low ambient precision would round these
    to the same value and cause silent data loss (one import would be dropped
    as a duplicate). This test proves the precision pinning prevents that.
    """
    # Quantities that differ only at 29+ digits
    qty1 = Decimal("1")
    qty2 = Decimal("1.00000000000000000000000000001")

    h1 = content_hash(ACC, T, "SPY", "buy", qty1, Decimal("500"))
    h2 = content_hash(ACC, T, "SPY", "buy", qty2, Decimal("500"))

    # They must produce different hashes
    assert h1 != h2, "quantities differing beyond low precision must not be deduplicated"
