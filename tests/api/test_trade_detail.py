"""GET /api/trades/{id}: everything Trade detail renders (spec §6)."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.marks import set_mark
from db.trades import regroup_account
from db.transfers import insert_transfers
from ledger.types import AssetClass, AssetTransfer, Fill, FillSource, Instrument, Side
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db

_T = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)


def _fill(acc, inst, *, side, qty, price, minutes, ref):
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=_T.replace(minute=minutes),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=ref,
        is_estimated=False,
    )


async def _seed(conn):
    acc = await create_account(conn, name="D", venue="manual", account_type="cash")
    zza = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZA", quote_currency="USD")
    )
    zzb = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZB", quote_currency="USD")
    )
    await insert_fills(
        conn,
        [
            _fill(acc, zza, side=Side.BUY, qty="5", price="100", minutes=0, ref="a1"),
            _fill(acc, zza, side=Side.SELL, qty="5", price="110", minutes=10, ref="a2"),
            _fill(acc, zzb, side=Side.BUY, qty="2", price="50", minutes=20, ref="b1"),
        ],
    )
    await regroup_account(conn, acc)
    rows = await conn.fetch(
        "SELECT id, status FROM trade WHERE account_id = $1 ORDER BY status", acc
    )
    by_status = {r["status"]: r["id"] for r in rows}
    return acc, zzb, by_status


async def test_closed_trade_detail_shape(client, conn):
    _acc, _zzb, trades = await _seed(conn)
    r = await client.get(f"/api/trades/{trades['closed']}")
    assert r.status_code == 200
    body = r.json()
    assert body["trade"]["status"] == "closed"
    assert body["instrument"]["symbol"] == "ZZA"
    assert len(body["fills"]) == 2
    assert {f["source"] for f in body["fills"]} == {"fill"}
    assert all(isinstance(f["allocated_quantity"], str) for f in body["fills"])
    kinds = [e["type"] for e in body["timeline"]]
    assert kinds[0] == "opened"
    assert kinds[-1] == "closed"
    assert kinds.count("fill") == 2
    assert body["pnl"]["realized"] == "50.000000000000000000"
    assert body["pnl"]["unrealized"] is None
    assert_no_json_floats(body)


async def test_open_trade_unrealized_uses_latest_mark(client, conn):
    _acc, zzb, trades = await _seed(conn)
    await set_mark(conn, zzb, Decimal("57"), as_of=_T.replace(minute=40))
    r = await client.get(f"/api/trades/{trades['open']}")
    body = r.json()
    # 2 shares, basis 50, mark 57 -> unrealized 14
    assert Decimal(body["pnl"]["unrealized"]) == Decimal("14")
    assert body["pnl"]["mark"]["price"] == "57"
    assert body["trade"]["status"] == "open"


async def test_transfer_closed_trade_timeline_carries_the_transfer(client, conn):
    acc, zzb, trades = await _seed(conn)
    await insert_transfers(
        conn,
        [
            AssetTransfer(
                id=uuid4(),
                account_id=acc,
                instrument_id=zzb,
                occurred_at=_T.replace(minute=50),
                quantity=Decimal("2"),
                market_value=Decimal("115.10"),
            )
        ],
    )
    await regroup_account(conn, acc)
    r = await client.get(f"/api/trades/{trades['open']}")
    body = r.json()
    assert body["trade"]["status"] == "closed"
    assert body["trade"]["qty_transferred"] == "2"
    kinds = [e["type"] for e in body["timeline"]]
    assert "transfer" in kinds
    assert body["pnl"]["realized"] == "0.000000000000000000"


async def test_unknown_trade_is_404(client):
    r = await client.get(f"/api/trades/{uuid4()}")
    assert r.status_code == 404
    assert r.json()["detail"] == "trade not found"
