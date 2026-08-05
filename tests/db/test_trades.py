from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.trades import list_trades, regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


async def seed(conn, specs):
    acc = await create_account(conn, name="T", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"),
    )
    fills = [
        Fill(
            id=uuid4(),
            account_id=acc,
            instrument_id=inst,
            executed_at=T0 + timedelta(minutes=i * 10),
            side=side,
            quantity=Decimal(q),
            price=Decimal(p),
            fee=Decimal("0"),
            fee_currency="USD",
            source=FillSource.MANUAL,
            venue_fill_id=f"v{i}",
            is_estimated=False,
        )
        for i, (side, q, p) in enumerate(specs)
    ]
    await insert_fills(conn, fills)
    return acc


async def test_regroup_writes_one_closed_trade_with_pnl(conn):
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    assert await regroup_account(conn, acc) == 1
    trades = await list_trades(conn, acc)
    assert len(trades) == 1
    assert trades[0]["status"] == "closed"
    assert trades[0]["realized_pnl"] == Decimal("20")
    assert trades[0]["avg_entry"] == Decimal("100")


async def test_allocations_are_persisted(conn):
    acc = await seed(conn, [(Side.BUY, "2", "100"), (Side.SELL, "3", "110")])
    await regroup_account(conn, acc)
    rows = await conn.fetch(
        "SELECT tf.quantity FROM trade_fill tf "
        "JOIN trade t ON t.id = tf.trade_id WHERE t.account_id = $1 "
        "ORDER BY tf.quantity",
        acc,
    )
    quantities = sorted(r["quantity"] for r in rows)
    assert quantities == [Decimal("1"), Decimal("2"), Decimal("2")]


async def test_regroup_is_idempotent(conn):
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    await regroup_account(conn, acc)
    assert len(await list_trades(conn, acc)) == 1


async def test_regroup_preserves_user_authored_fields(conn):
    """The whole point of upserting instead of rebuilding. A routine re-import
    must never silently destroy hand-entered judgment."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)

    await conn.execute(
        """
        UPDATE trade SET notes = 'thesis: CPI hot', planned_risk = 50,
                         strategy_tag = 'orb', intent = 'trade'
         WHERE account_id = $1
        """,
        acc,
    )

    await regroup_account(conn, acc)

    t = (await list_trades(conn, acc))[0]
    assert t["notes"] == "thesis: CPI hot"
    assert t["planned_risk"] == Decimal("50")
    assert t["strategy_tag"] == "orb"
    assert t["realized_pnl"] == Decimal("20")  # derived value still refreshed
    assert t["r_multiple"] == Decimal("0.4")  # recomputed from planned_risk


async def test_regroup_keeps_the_same_trade_id_across_runs(conn):
    """A stable id is what lets subsystem B attach a thesis to a trade."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    first = (await list_trades(conn, acc))[0]["id"]
    await regroup_account(conn, acc)
    assert (await list_trades(conn, acc))[0]["id"] == first


async def test_appending_a_later_fill_updates_the_same_trade(conn):
    """Scaling into an open position must not create a second trade."""
    acc = await seed(conn, [(Side.BUY, "1", "100")])
    await regroup_account(conn, acc)
    original = (await list_trades(conn, acc))[0]["id"]

    inst = await conn.fetchval("SELECT id FROM instrument LIMIT 1")
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst,
                executed_at=T0 + timedelta(hours=2),
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("110"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="later",
                is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, acc)

    trades = await list_trades(conn, acc)
    assert len(trades) == 1
    assert trades[0]["id"] == original
    assert trades[0]["avg_entry"] == Decimal("105")


async def test_regroup_does_not_touch_manual_trades(conn):
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    await conn.execute(
        "UPDATE trade SET grouping_mode = 'manual', notes = 'keep me' WHERE account_id = $1",
        acc,
    )
    await regroup_account(conn, acc)
    trades = await list_trades(conn, acc)
    assert len(trades) == 1
    assert trades[0]["notes"] == "keep me"


async def test_intent_defaults_from_the_account(conn):
    acc = await create_account(
        conn, name="IRA", venue="manual", account_type="cash", default_intent="investment"
    )
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="VTI", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst,
                executed_at=T0,
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("250"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="x1",
                is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, acc)
    assert (await list_trades(conn, acc))[0]["intent"] == "investment"


async def test_mixed_account_leaves_intent_unassigned(conn):
    acc = await create_account(
        conn, name="Brokerage", venue="manual", account_type="cash", default_intent="mixed"
    )
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="AAPL", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=inst,
                executed_at=T0,
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("200"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="y1",
                is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, acc)
    assert (await list_trades(conn, acc))[0]["intent"] == "unassigned"
