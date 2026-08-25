"""Manual fill add/delete (spec E5). All values invented."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from db.accounts import create_account
from db.fills import add_manual_fills, delete_manual_fill, insert_fills
from db.instruments import upsert_instrument
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db

_T = datetime(2026, 6, 1, 15, 30, tzinfo=UTC)


async def _account_and_instrument(conn):
    acc = await create_account(conn, name="ManualEntry", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZE", quote_currency="USD"),
    )
    return acc, inst


def _manual_fill(acc, inst, *, qty="10", price="5"):
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=_T,
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=None,
        is_estimated=False,
    )


async def test_two_identical_manual_fills_both_land(conn):
    """Buying the same thing twice at the same instant is a real event. The
    import dedupe hashes on (time, symbol, side, qty, price), so if manual
    fills carried a content_hash the second would be silently dropped -- the
    user would type it, see success, and it would not exist."""
    acc, inst = await _account_and_instrument(conn)
    ids = await add_manual_fills(conn, [_manual_fill(acc, inst), _manual_fill(acc, inst)])
    assert len(ids) == 2
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 2


async def test_manual_fills_carry_no_dedupe_keys(conn):
    acc, inst = await _account_and_instrument(conn)
    await add_manual_fills(conn, [_manual_fill(acc, inst)])
    row = await conn.fetchrow(
        "SELECT source, venue_fill_id, content_hash FROM fill WHERE account_id = $1", acc
    )
    assert row["source"] == "manual"
    assert row["venue_fill_id"] is None
    assert row["content_hash"] is None


async def test_add_manual_fills_refuses_a_non_manual_fill(conn):
    """The function names its contract; a csv-sourced fill routed through here
    would bypass the import path's dedupe entirely."""
    acc, inst = await _account_and_instrument(conn)
    from dataclasses import replace

    with pytest.raises(ValueError, match="manual"):
        await add_manual_fills(conn, [replace(_manual_fill(acc, inst), source=FillSource.CSV)])


async def test_delete_manual_fill_removes_it(conn):
    acc, inst = await _account_and_instrument(conn)
    (fill_id,) = await add_manual_fills(conn, [_manual_fill(acc, inst)])
    assert await delete_manual_fill(conn, fill_id) is True
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE id = $1", fill_id) == 0


async def test_delete_manual_fill_refuses_an_imported_fill(conn):
    """Imported fills are reproducible from their export; deleting one invites
    divergence from the source of truth (spec E5). Enforced in SQL, so a
    caller that forgets to check cannot bypass it."""
    acc, inst = await _account_and_instrument(conn)
    from dataclasses import replace

    imported = replace(_manual_fill(acc, inst), source=FillSource.CSV, venue_fill_id="v1")
    await insert_fills(conn, [imported])
    assert await delete_manual_fill(conn, imported.id) is False
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE id = $1", imported.id) == 1
