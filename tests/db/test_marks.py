from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from db.instruments import upsert_instrument
from db.marks import latest_marks, resolve_instrument_by_symbol, set_mark
from ledger.types import AssetClass, Instrument
from tests.conftest import requires_db

pytestmark = requires_db


def equity(symbol: str, quote_currency: str = "USD") -> Instrument:
    return Instrument(
        id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency=quote_currency
    )


@pytest_asyncio.fixture
async def an_instrument(conn):
    """A single instrument this test file owns, isolated by the rolled-back
    `conn` transaction from tests/conftest.py -- it never persists."""
    return await upsert_instrument(conn, equity("MARKTEST1"))


@pytest_asyncio.fixture
async def two_same_symbol(conn):
    """Two instruments sharing a symbol but not a natural_key -- the same
    ticker quoted in two different currencies."""
    a = await upsert_instrument(conn, equity("DUPE", "USD"))
    b = await upsert_instrument(conn, equity("DUPE", "EUR"))
    return a, b


async def test_a_mark_round_trips(conn, an_instrument):
    when = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    await set_mark(conn, an_instrument, Decimal("24.50"), when)
    got = await latest_marks(conn, [an_instrument])
    assert got[an_instrument] == (Decimal("24.50"), when)


async def test_the_latest_mark_wins_not_the_last_written(conn, an_instrument):
    """Marks are keyed (instrument_id, as_of), so a backfilled OLDER mark can
    be written after a newer one. Ordering must be by as_of, not by
    insertion."""
    newer = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    older = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    await set_mark(conn, an_instrument, Decimal("30"), newer)
    await set_mark(conn, an_instrument, Decimal("10"), older)
    assert (await latest_marks(conn, [an_instrument]))[an_instrument][0] == Decimal("30")


async def test_rewriting_the_same_timestamp_updates_rather_than_failing(conn, an_instrument):
    when = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    await set_mark(conn, an_instrument, Decimal("24.50"), when)
    await set_mark(conn, an_instrument, Decimal("25.00"), when)
    assert (await latest_marks(conn, [an_instrument]))[an_instrument][0] == Decimal("25.00")


async def test_an_unmarked_instrument_is_absent_not_zero(conn, an_instrument):
    """Absent must be distinguishable from a genuine zero price -- the mark
    table permits price = 0."""
    assert await latest_marks(conn, [an_instrument]) == {}


async def test_set_mark_rejects_a_naive_as_of(conn, an_instrument):
    naive = datetime(2026, 8, 8, 12, 0)
    with pytest.raises(ValueError):
        await set_mark(conn, an_instrument, Decimal("1"), naive)


async def test_an_id_with_no_matching_instrument_is_simply_absent(conn, an_instrument):
    """latest_marks may be handed ids that are not real instrument ids at all
    (db/positions.py sometimes uses a trade id as a grouping key). Such an id
    must not raise and must not appear in the result -- it is simply absent,
    same as any other unmarked id."""
    from uuid import uuid4

    when = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
    await set_mark(conn, an_instrument, Decimal("24.50"), when)
    bogus = uuid4()
    got = await latest_marks(conn, [an_instrument, bogus])
    assert an_instrument in got
    assert bogus not in got


async def test_an_ambiguous_symbol_is_refused_naming_the_candidates(conn, two_same_symbol):
    """symbol is not unique; only natural_key is. Marking 'the first one'
    would silently value the wrong instrument."""
    with pytest.raises(ValueError) as exc:
        await resolve_instrument_by_symbol(conn, "DUPE")
    assert "natural_key" in str(exc.value) or "natural key" in str(exc.value).lower()


async def test_an_ambiguous_symbol_error_names_both_candidates(conn, two_same_symbol):
    """The error must name every candidate's natural_key, not just report a count."""
    with pytest.raises(ValueError) as exc:
        await resolve_instrument_by_symbol(conn, "DUPE")
    message = str(exc.value)
    assert "equity:DUPE:USD" in message
    assert "equity:DUPE:EUR" in message


async def test_an_unknown_symbol_is_refused(conn):
    with pytest.raises(ValueError):
        await resolve_instrument_by_symbol(conn, "NOSUCHSYMBOL")


async def test_symbol_lookup_is_case_insensitive(conn, an_instrument):
    resolved = await resolve_instrument_by_symbol(conn, "marktest1")
    assert resolved == an_instrument
