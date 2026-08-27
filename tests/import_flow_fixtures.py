"""Batch builders shared by the import-flow tests either side of the DB line
(tests/test_import_flow.py runs in the pure lane, tests/db/test_import_flow.py
in the database lane). Defined once here so the two can never drift into
describing different batches.

ZZI and every number below are invented."""

from datetime import UTC, datetime
from decimal import Decimal

from importers.base import CanonicalFill
from ledger.types import AssetClass, Instrument, Side


def fill_with_ref(ref: str | None, *, day: int = 0) -> CanonicalFill:
    """One equity buy, optionally carrying the account ref routing turns on.
    `day` only spaces fills apart so two in one batch are not identical rows.
    """
    return CanonicalFill(
        instrument=Instrument(
            id=None, asset_class=AssetClass.EQUITY, symbol="ZZI", quote_currency="USD"
        ),
        executed_at=datetime(2026, 3, 2 + day, 14, 30, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("2"),
        price=Decimal("11"),
        fee=Decimal("0"),
        fee_currency="USD",
        external_ref=ref,
    )
