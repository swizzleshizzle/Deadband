"""GET /api/dashboard: one call returns everything the Dashboard renders
(spec §4). All values invented."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.cash import account_cash
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.marks import set_mark
from db.snapshots import add_snapshot
from db.trades import regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
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


async def _seed_marked_account(conn):
    """One account: deposit 1000, buy 2 @ 50 (cash 900), mark 57. Snapshot
    agrees exactly with the computed equity, so drift verdict is 'ok'."""
    acc = await create_account(conn, name="M", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZM", quote_currency="USD")
    )
    await conn.execute(
        "INSERT INTO cash_movement (account_id, occurred_at, kind, amount)"
        " VALUES ($1, $2, 'deposit', 1000)",
        acc,
        _T,
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, qty="2", price="50", minutes=10, ref="m1")]
    )
    await regroup_account(conn, acc)
    await set_mark(conn, inst, Decimal("57"), as_of=_T.replace(minute=30))
    # computed: cash 900 + 2*57 = 1014
    await add_snapshot(
        conn,
        acc,
        as_of=_T.replace(minute=40),
        cash_balance=Decimal("900"),
        total_equity=Decimal("1014"),
    )
    return acc


async def test_dashboard_marked_account(client, conn):
    acc = await _seed_marked_account(conn)
    r = await client.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert_no_json_floats(body)

    tile = next(a for a in body["accounts"] if a["id"] == str(acc))
    assert tile["cash"] == "900"
    assert Decimal(tile["equity"]) == Decimal("1014")
    assert tile["snapshot"]["total_equity"] == "1014"
    assert tile["drift"]["verdict"] == "ok"

    pos = next(p for p in body["open_positions"] if p["account_id"] == str(acc))
    assert pos["instrument"]["symbol"] == "ZZM"
    assert pos["quantity"] == "2"
    assert pos["mark"]["price"] == "57"
    assert Decimal(pos["market_value"]) == Decimal("114")
    assert Decimal(pos["unrealized_pnl"]) == Decimal("14")

    kinds = {e["type"] for e in body["recent_activity"]}
    assert {"fill", "cash_movement"} <= kinds
    assert body["recent_activity"][0]["at"] >= body["recent_activity"][-1]["at"]


async def test_unmarked_position_nulls_equity_and_lists_unvaluable(client, conn):
    acc = await create_account(conn, name="U", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZU", quote_currency="USD")
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, qty="3", price="10", minutes=0, ref="u1")]
    )
    await regroup_account(conn, acc)

    r = await client.get("/api/dashboard")
    body = r.json()
    tile = next(a for a in body["accounts"] if a["id"] == str(acc))
    assert tile["cash"] == "-30"
    assert tile["equity"] is None
    assert tile["snapshot"] is None
    assert tile["drift"] is None
    assert body["equity"]["total"] is None  # a partial sum is never a total
    unval = [u for u in body["unvaluable"] if u["instrument"]["symbol"] == "ZZU"]
    assert len(unval) == 1
    assert "no mark" in unval[0]["reason"]

    pos = next(p for p in body["open_positions"] if p["account_id"] == str(acc))
    assert pos["mark"] is None
    assert pos["market_value"] is None
    assert pos["unrealized_pnl"] is None
