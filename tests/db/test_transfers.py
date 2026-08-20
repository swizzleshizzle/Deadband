"""asset_transfer round-trip and dedupe. All values invented."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.instruments import upsert_instrument
from db.transfers import fetch_transfers, insert_transfers
from ledger.types import AssetClass, AssetTransfer, Instrument
from tests.conftest import requires_db

pytestmark = requires_db

_T = datetime(2026, 3, 11, 0, 0, tzinfo=UTC)


async def _setup(conn):
    acc = await create_account(conn, name="T", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    return acc, inst


def _transfer(acc, inst, *, hash_=None):
    return AssetTransfer(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        occurred_at=_T,
        quantity=Decimal("40"),
        market_value=Decimal("259.20"),
        content_hash=hash_,
    )


async def test_insert_and_fetch_round_trip(conn):
    acc, inst = await _setup(conn)
    result = await insert_transfers(conn, [_transfer(acc, inst)])
    assert (result.inserted, result.skipped) == (1, 0)
    got = await fetch_transfers(conn, acc)
    assert len(got) == 1
    assert got[0].quantity == Decimal("40")
    assert got[0].market_value == Decimal("259.20")
    assert got[0].instrument_id == inst


async def test_content_hash_dedupes_reimports(conn):
    acc, inst = await _setup(conn)
    await insert_transfers(conn, [_transfer(acc, inst, hash_="h1")])
    result = await insert_transfers(conn, [_transfer(acc, inst, hash_="h1")])
    assert (result.inserted, result.skipped) == (0, 1)
    assert len(await fetch_transfers(conn, acc)) == 1
