"""GET /api/trades: the filterable log (spec §5). All values invented."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.trades import regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db

_T = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)


def _fill(acc, inst, *, side, qty, price, minutes, ref):
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=_T.replace(minute=minutes % 60, hour=14 + minutes // 60),
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
    """One account, two instruments: a CLOSED round trip on ZZA (tagged) and
    an OPEN long on ZZB."""
    acc = await create_account(conn, name="A", venue="manual", account_type="cash")
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
    await conn.execute(
        "UPDATE trade SET strategy_tag = 'swing' WHERE account_id = $1 AND status = 'closed'",
        acc,
    )
    return acc


async def test_lists_newest_opened_first_with_total(client, conn):
    acc = await _seed(conn)
    r = await client.get("/api/trades", params={"account": str(acc)})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert [t["status"] for t in body["trades"]] == ["open", "closed"]
    assert body["trades"][0]["instrument_symbol"] == "ZZB"
    assert_no_json_floats(body)
    closed = body["trades"][1]
    assert closed["realized_pnl"] == "50.000000000000000000"
    assert isinstance(closed["qty_opened"], str)


async def test_filters_combine(client, conn):
    acc = await _seed(conn)
    r = await client.get(
        "/api/trades",
        params={"account": str(acc), "status": "closed", "instrument": "zza", "tag": "SWING"},
    )
    body = r.json()
    assert body["total"] == 1
    assert body["trades"][0]["primary_underlying"] == "ZZA"

    r = await client.get(
        "/api/trades", params={"account": str(acc), "status": "closed", "instrument": "zzb"}
    )
    assert r.json()["total"] == 0


async def test_date_window_is_inclusive(client, conn):
    acc = await _seed(conn)
    r = await client.get(
        "/api/trades",
        params={"account": str(acc), "from": "2026-05-04", "to": "2026-05-04"},
    )
    assert r.json()["total"] == 2
    r = await client.get(
        "/api/trades", params={"account": str(acc), "to": "2026-05-03"}
    )
    assert r.json()["total"] == 0


async def test_paging_keeps_total(client, conn):
    acc = await _seed(conn)
    r = await client.get(
        "/api/trades", params={"account": str(acc), "limit": 1, "offset": 1}
    )
    body = r.json()
    assert body["total"] == 2
    assert len(body["trades"]) == 1
    assert body["trades"][0]["status"] == "closed"
    assert (body["limit"], body["offset"]) == (1, 1)


async def test_bad_params_are_422(client):
    assert (await client.get("/api/trades", params={"account": "not-a-uuid"})).status_code == 422
    assert (await client.get("/api/trades", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/trades", params={"status": "sideways"})).status_code == 422
