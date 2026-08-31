"""Shared request-value parsing (api/validation.py). All values invented.

No `requires_db` marker: nothing here touches a database, so these belong to
the pure lane and must run even when TEST_PG_DSN is unset."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException

from api.validation import parse_as_of, parse_decimal, parse_instant, refuse_future
from db.marks import MARK_FUTURE_TOLERANCE


def test_parse_decimal_accepts_an_exact_string():
    assert parse_decimal("238.90", "price") == Decimal("238.90")


def test_parse_decimal_refuses_a_non_number():
    # Decimal("abc") raises InvalidOperation, which does NOT descend from
    # ValueError -- a bare `except ValueError` would let it crash through as
    # a 500 instead of becoming a clean 422.
    with pytest.raises(HTTPException) as exc:
        parse_decimal("abc", "price")
    assert exc.value.status_code == 422
    assert "price" in exc.value.detail


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity"])
def test_parse_decimal_refuses_non_finite(raw):
    # These CONSTRUCT successfully and slip past the InvalidOperation catch
    # entirely. account_snapshot has no CHECK constraints, so nothing below
    # this line would stop a stored NaN.
    with pytest.raises(HTTPException) as exc:
        parse_decimal(raw, "cash_balance")
    assert exc.value.status_code == 422


def test_parse_instant_requires_an_offset():
    with pytest.raises(HTTPException) as exc:
        parse_instant("2026-08-28T14:02:00", "as_of")
    assert exc.value.status_code == 422
    assert "offset" in exc.value.detail


def test_parse_instant_accepts_zulu():
    assert parse_instant("2026-08-28T14:02:00Z", "as_of") == datetime(
        2026, 8, 28, 14, 2, tzinfo=UTC
    )


def test_parse_as_of_turns_a_bare_date_into_midnight_utc():
    # Matches cli.py's _parse_as_of exactly: `snapshot add` accepts the bare
    # date the README's worked example passes.
    assert parse_as_of("2026-07-31", "as_of") == datetime(2026, 7, 31, tzinfo=UTC)


def test_parse_as_of_refuses_a_timestamp_without_an_offset():
    # date.fromisoformat rejects anything carrying a time component, so this
    # falls through to the timestamp branch and hits the tz guard rather than
    # being silently swallowed as a date.
    with pytest.raises(HTTPException) as exc:
        parse_as_of("2026-07-31T12:00", "as_of")
    assert exc.value.status_code == 422


def test_refuse_future_allows_now():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    refuse_future(now, now, "as_of")


def test_refuse_future_allows_within_tolerance():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    refuse_future(now + MARK_FUTURE_TOLERANCE, now, "as_of")


def test_refuse_future_rejects_beyond_tolerance():
    now = datetime(2026, 8, 28, 14, 0, tzinfo=UTC)
    with pytest.raises(HTTPException) as exc:
        refuse_future(now + MARK_FUTURE_TOLERANCE + timedelta(seconds=1), now, "as_of")
    assert exc.value.status_code == 422


def test_the_tolerance_is_two_minutes():
    """Pinned because two copies of this value drifting is the exact failure
    _parse_as_of's docstring records for its own near-identical copies."""
    assert MARK_FUTURE_TOLERANCE == timedelta(minutes=2)
