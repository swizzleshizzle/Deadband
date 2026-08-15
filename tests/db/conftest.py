"""Shared fixtures for tests/db/*. `_split` and `account_with_1800` live here
so tasks 2 and 3 import them rather than each defining their own copy --
triplicated fixtures are a review finding waiting to happen."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest_asyncio

from db.accounts import create_account
from db.fills import insert_fills
from db.instruments import upsert_instrument
from ledger.corporate import ActionType, CorporateAction
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side

# Deliberately earlier than tests/db/test_positions.py's T0 (2026-08-01), which
# is AFTER the 2026-03-02 ex-date used throughout this file's fixtures. A BUY
# fill executed on T0 would fall on the wrong side of the split and every
# split-dependent test here would pass vacuously.
_T0 = datetime(2026, 2, 1, 9, 0, tzinfo=UTC)


def _split(
    instrument_id,
    *,
    num="1",
    den="6",
    ex_date=date(2026, 3, 2),
    action_type=ActionType.REVERSE_SPLIT,
):
    return CorporateAction(
        instrument_id=instrument_id,
        action_type=action_type,
        ex_date=ex_date,
        ratio_numerator=Decimal(num),
        ratio_denominator=Decimal(den),
    )


def _fill(acc, inst, *, side, quantity, price, ref):
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=_T0,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=ref,
        is_estimated=False,
    )


@pytest_asyncio.fixture
async def account_with_1800(conn):
    """One account holding a single BUY fill of 1800 at 0.05 on a fabricated
    ZXCO equity, executed before the 2026-03-02 ex-date used throughout this
    file, so a reverse split actually applies to it."""
    acc = await create_account(conn, name="Corp", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, quantity="1800", price="0.05", ref="zx1800")]
    )
    return acc, inst
