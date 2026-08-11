from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio

from db.accounts import create_account
from db.snapshots import add_snapshot, latest_snapshot
from tests.conftest import requires_db

pytestmark = requires_db


@pytest_asyncio.fixture
async def an_account(conn):
    return await create_account(conn, name="Snap", venue="manual", account_type="cash")


@pytest_asyncio.fixture
async def two_accounts(conn):
    a = await create_account(conn, name="SnapA", venue="manual", account_type="cash")
    b = await create_account(conn, name="SnapB", venue="manual", account_type="cash")
    return a, b


async def test_a_snapshot_round_trips(conn, an_account):
    when = datetime(2026, 7, 31, tzinfo=UTC)
    await add_snapshot(
        conn, an_account, when,
        cash_balance=Decimal("2110.00"), total_equity=Decimal("41203.18"),
    )
    row = await latest_snapshot(conn, an_account)
    assert row["cash_balance"] == Decimal("2110.00")
    assert row["total_equity"] == Decimal("41203.18")
    assert row["as_of"] == when


async def test_the_latest_by_date_wins_not_the_last_written(conn, an_account):
    """Same hazard as latest_marks: a correction entered today for last month's
    statement must not become 'the latest'. Ordering is by as_of, not insertion."""
    newer = datetime(2026, 7, 31, tzinfo=UTC)
    older = datetime(2026, 6, 30, tzinfo=UTC)
    await add_snapshot(conn, an_account, newer,
                       cash_balance=Decimal("10"), total_equity=Decimal("100"))
    await add_snapshot(conn, an_account, older,
                       cash_balance=Decimal("20"), total_equity=Decimal("200"))
    assert (await latest_snapshot(conn, an_account))["total_equity"] == Decimal("100")


async def test_as_of_selects_the_most_recent_on_or_before(conn, an_account):
    await add_snapshot(conn, an_account, datetime(2026, 6, 30, tzinfo=UTC),
                       cash_balance=Decimal("20"), total_equity=Decimal("200"))
    await add_snapshot(conn, an_account, datetime(2026, 7, 31, tzinfo=UTC),
                       cash_balance=Decimal("10"), total_equity=Decimal("100"))
    row = await latest_snapshot(conn, an_account, datetime(2026, 7, 1, tzinfo=UTC))
    assert row["total_equity"] == Decimal("200")


async def test_rewriting_the_same_as_of_updates_rather_than_failing(conn, an_account):
    """Correcting a mistyped figure is the point of the ON CONFLICT DO UPDATE
    (gap #21 -- there is no delete and no history), so BOTH figures have to be
    repainted, not just the one this test used to check. Pinned on equity
    alone, dropping `cash_balance = EXCLUDED.cash_balance` from the SET list
    reddened nothing: the correction silently kept the stale cash figure
    against the corrected equity, and `reconcile` would then report drift on
    the cash line for a snapshot the operator believes they just fixed."""
    when = datetime(2026, 7, 31, tzinfo=UTC)
    await add_snapshot(conn, an_account, when,
                       cash_balance=Decimal("10"), total_equity=Decimal("100"))
    await add_snapshot(conn, an_account, when,
                       cash_balance=Decimal("11"), total_equity=Decimal("111"))
    row = await latest_snapshot(conn, an_account)
    assert row["total_equity"] == Decimal("111")
    assert row["cash_balance"] == Decimal("11")


async def test_an_account_with_no_snapshot_returns_none(conn, an_account):
    """Absent must be distinguishable from a zero-equity snapshot."""
    assert await latest_snapshot(conn, an_account) is None


async def test_snapshots_are_scoped_to_their_account(conn, two_accounts):
    a, b = two_accounts
    await add_snapshot(conn, a, datetime(2026, 7, 31, tzinfo=UTC),
                       cash_balance=Decimal("10"), total_equity=Decimal("100"))
    assert await latest_snapshot(conn, b) is None


async def test_add_snapshot_rejects_a_naive_as_of(conn, an_account):
    naive = datetime(2026, 7, 31, 12, 0)
    with pytest.raises(ValueError):
        await add_snapshot(conn, an_account, naive,
                           cash_balance=Decimal("1"), total_equity=Decimal("1"))
