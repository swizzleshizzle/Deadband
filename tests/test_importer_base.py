import hashlib
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from importers.base import content_hash, zero_amount_warning
from importers.registry import get_importer, list_importers

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


def test_hash_escapes_delimiter_in_symbol():
    """Symbol and side containing | must not create collisions.

    Without escaping, these two inputs would join to the same payload:
      symbol="AAA|1", side="23"  -> "AAA|1|23|..."
      symbol="AAA", side="1|23"  -> "AAA|1|23|..."
    This silently drops one trade on import.
    """
    h1 = content_hash(ACC, T, "AAA|1", "23", Decimal("10"), Decimal("500"))
    h2 = content_hash(ACC, T, "AAA", "1|23", Decimal("10"), Decimal("500"))
    assert h1 != h2, "delimiter in symbol must not collide with one in side"


def test_hash_escapes_percent_in_symbol():
    """Escaping must be injective: %7C (escaped |) vs a literal | must differ.

    This ensures that a symbol containing a literal "%7C" (which becomes "%%7C"
    in JSON exports) does not collide with one containing a real "|".
    """
    # Symbol with a literal | that becomes %7C when escaped
    h1 = content_hash(ACC, T, "A|B", "buy", Decimal("10"), Decimal("500"))
    # Symbol with the literal string that results from the above escaping
    # (this would be a pathological real-world symbol, but tests the injectivity)
    h2 = content_hash(ACC, T, "A%7CB", "buy", Decimal("10"), Decimal("500"))
    assert h1 != h2, "escaped pipe must not collide with literal %7C in symbol"


def test_hash_escapes_delimiter_in_side():
    """The twin of test_hash_escapes_delimiter_in_symbol. `side` is escaped
    in the implementation, and a gap note records the escaping as currently
    non-exploitable -- but that proof rests on `side` being followed only by
    numeric fields (quantity, price, occurrence), none of which can ever
    render a literal '|'. Because of that, no pair of content_hash() calls
    that vary only `side` can be made to collide today: the escaped symbol
    to its left has no '|' either, so the boundary on both sides of `side`
    is unambiguous regardless of what `side` contains, escaped or not. A
    black-box collision test (the shape used for symbol) would therefore
    stay green even with escaping removed entirely, proving nothing.

    So this test instead pins the actual payload shape: it reconstructs the
    expected pre-hash string by hand, with `side` escaped the way
    importers/base.py documents doing it, and asserts content_hash() matches
    that digest exactly. That makes the escaping load-bearing on its own
    terms rather than on the accident of field order that currently masks
    its absence -- if a future field lands after `side` with free-text
    values, the escaping (or its removal) will already be under test here.

    The hardcoded `payload` below is a deliberate STRUCTURAL PIN, not just
    a convenient expected value. Because it is a full 7-field literal
    compared as a whole hash, this test is sensitive to any change in the
    payload's shape -- a field inserted anywhere, a reorder, a different
    join character, a different digest -- not only to a regression in
    escaping. If this test fails for a reason unrelated to `side`, that is
    the signal, not a false alarm: it means the field layout has changed,
    which is exactly the precondition under which `side`'s escaping stops
    being safe (see the gap note this test closes). Re-derive whether
    `side` can still collide with its neighbours under the new layout
    BEFORE updating the expected string -- do not just patch `payload`
    until this goes green again, or the guard this test exists to provide
    is silently discarded. Relatedly, `"buy%7Cx"` below is hand-typed
    rather than produced by calling the real `_escape()` -- using
    production's own escaper to build the expected value would make this
    test tautological against a bug in `_escape` itself.
    """
    payload = "|".join(
        [
            str(ACC),
            T.astimezone(UTC).isoformat(),
            "ZXCO",
            "buy%7Cx",  # "buy|x" with '|' escaped to '%7C', as _escape does
            "1",
            "1",
            "0",
        ]
    )
    expected = hashlib.sha256(payload.encode()).hexdigest()
    actual = content_hash(ACC, T, "ZXCO", "buy|x", Decimal("1"), Decimal("1"))
    assert actual == expected


def test_hash_normalizes_timezones_to_utc():
    """Same instant in different timezone offsets must produce the same hash.

    Two exports of the same trade with different offset conventions should
    dedupe, not import twice. Without UTC normalization:
      2026-08-01 14:30:00+00:00  ->  hash_A
      2026-08-01 09:30:00-05:00  ->  hash_B  (the same instant!)
    """
    utc_time = datetime(2026, 8, 1, 14, 30, tzinfo=UTC)
    eastern_tz = timezone(timedelta(hours=-5))
    eastern_time = datetime(2026, 8, 1, 9, 30, tzinfo=eastern_tz)

    h_utc = content_hash(ACC, utc_time, "SPY", "buy", Decimal("10"), Decimal("500"))
    h_eastern = content_hash(ACC, eastern_time, "SPY", "buy", Decimal("10"), Decimal("500"))
    assert h_utc == h_eastern, "same instant in different timezones must hash identically"


def test_hash_occurrence_distinguishes_otherwise_identical_rows():
    """Two rows with the same account/time/symbol/side/quantity/price — e.g. two
    genuine same-day trades from a venue whose export has no time component —
    must hash differently when given different occurrence indices, or the
    second is silently deduped away as a "duplicate" of the first. Fails if
    occurrence is ignored (payload construction drops it) or not distinguishing."""
    base = (ACC, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    assert content_hash(*base, 0) != content_hash(*base, 1)
    assert content_hash(*base, 1) != content_hash(*base, 2)


def test_hash_occurrence_defaults_to_zero():
    """The default keeps every pre-existing 6-arg call site (and its hash value)
    unchanged. Fails if the default were anything other than 0, or if omitting
    the argument produced a different hash than passing 0 explicitly."""
    base = (ACC, T, "SPY", "buy", Decimal("10"), Decimal("500"))
    assert content_hash(*base) == content_hash(*base, 0)


def test_hash_rejects_naive_datetime():
    """Naive datetimes (no timezone) must raise ValueError.

    A naive datetime is silently interpreted as local machine time, which
    violates the "pure" contract and makes hashing host-dependent.
    """
    naive_time = datetime(2026, 8, 1, 14, 30)
    with pytest.raises(ValueError, match="timezone-aware"):
        content_hash(ACC, naive_time, "SPY", "buy", Decimal("10"), Decimal("500"))


# --- Registry (deferred from Task 11 — needed both importers to exist) -----


def test_registry_rejects_unknown_venue():
    with pytest.raises(KeyError, match="unknown importer"):
        get_importer("etrade")


def test_registry_lists_available_importers():
    assert set(list_importers()) >= {"coinbase", "fidelity"}


# --- C2: cash rows had no zero-amount guard ---------------------------------


def test_zero_amount_warning_fires_on_a_zero_amount_cash_movement():
    warn = zero_amount_warning(7, "dividend", Decimal("0"))
    assert warn is not None
    assert "line 7" in warn
    assert "dividend" in warn


def test_zero_amount_warning_is_silent_on_a_real_amount():
    assert zero_amount_warning(7, "dividend", Decimal("42.15")) is None
