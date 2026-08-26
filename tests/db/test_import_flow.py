"""The extracted import decision layer. These assert on RETURNED DATA, never on
printed output -- that separation is the whole point of the extraction, and it
is what lets the API reuse these decisions instead of restating them.

All values invented."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from db.accounts import create_account
from db.import_flow import ImportCommitReport, PreviewReport, UnroutableRowsError, commit, preview
from importers.base import CanonicalFill, ImportBatch
from ledger.types import AssetClass, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db


def _batch(*, ref: str | None = None, n: int = 1, refs_seen: tuple[str, ...] = ()) -> ImportBatch:
    """Modelled on tests/db/test_importing.py's batch_of, plus the external_ref
    that routing turns on. ZZI is invented, as is every number here."""
    return ImportBatch(
        fills=tuple(
            CanonicalFill(
                instrument=Instrument(
                    id=None, asset_class=AssetClass.EQUITY, symbol="ZZI", quote_currency="USD"
                ),
                executed_at=datetime(2026, 3, 2 + i, 14, 30, tzinfo=UTC),
                side=Side.BUY,
                quantity=Decimal("2"),
                price=Decimal("11"),
                fee=Decimal("0"),
                fee_currency="USD",
                external_ref=ref,
            )
            for i in range(n)
        ),
        refs_seen=refs_seen or ((ref,) if ref else ()),
    )


async def test_preview_opens_no_connection_when_duplicates_are_not_requested():
    """A deliberate guarantee, pinned for the CLI by
    test_preview_import_never_opens_a_database_connection. conn=None must be a
    supported call, not an accident that happens to work."""
    batch = ImportBatch(warnings=("w1",), unmapped_rows=("r1",), refs_seen=("A",))
    report = await preview(batch, venue="fidelity", conn=None)
    assert isinstance(report, PreviewReport)
    assert report.warnings == ("w1",)
    assert report.unmapped_row_count == 1
    assert report.duplicates is None


async def test_preview_reports_every_ref_seen_including_wholly_unmapped_accounts(conn):
    """refs_seen is a strict superset of the refs reachable from fills/cash. An
    account whose rows are ALL unmapped contributes nothing to either, and is
    exactly the account this report most needs to surface."""
    await create_account(
        conn, name="Known", venue="fidelity", account_type="cash", external_ref="ZREF1"
    )
    report = await preview(
        _batch(ref="ZREF1", refs_seen=("ZREF1", "ZGHOST")), venue="fidelity", conn=conn
    )
    assert "ZGHOST" in report.unknown_refs
    assert "ZREF1" not in report.unknown_refs


async def test_commit_writes_and_regroups_and_reports_both(conn):
    acc = await create_account(
        conn, name="Flow", venue="fidelity", account_type="cash", external_ref="ZREF2"
    )
    report = await commit(
        conn, venue="fidelity", batch=_batch(ref="ZREF2"), account_id=None, source="csv"
    )
    assert isinstance(report, ImportCommitReport)
    assert report.fills_inserted == 1
    assert report.trades_regrouped >= 1
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 1


async def test_commit_refuses_unrouted_rows_when_no_account_is_given(conn):
    """A venue with no per-row account ref (the History dialect) has nothing to
    route on. Committing it without an explicit account would silently drop
    every row, so it must refuse instead."""
    with pytest.raises(UnroutableRowsError):
        await commit(
            conn, venue="fidelity", batch=_batch(ref=None), account_id=None, source="csv"
        )


async def test_commit_routes_everything_to_the_given_account_when_one_is_supplied(conn):
    acc = await create_account(conn, name="Whole", venue="fidelity", account_type="cash")
    report = await commit(
        conn, venue="fidelity", batch=_batch(ref=None), account_id=acc, source="csv"
    )
    assert report.fills_inserted == 1
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 1
