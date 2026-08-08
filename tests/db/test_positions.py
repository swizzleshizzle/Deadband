from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest_asyncio

from db.accounts import create_account
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.positions import open_positions
from db.trades import list_trades, regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _fill(acc, inst, *, side, quantity, price, ref, estimated=False):
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=ref,
        is_estimated=estimated,
    )


@pytest_asyncio.fixture
async def seeded_account(conn):
    """One account, one instrument (ZXCO), one open long of 10 -- seeded via
    the same commit-fills-then-regroup path production uses, so this fixture
    breaks if persistence of open_quantity regresses."""
    acc = await create_account(conn, name="Seeded", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, quantity="10", price="50", ref="zx1")]
    )
    await regroup_account(conn, acc)
    return acc


@pytest_asyncio.fixture
async def closed_trade_account(conn):
    """One account whose only trade has been fully closed -- no open position."""
    acc = await create_account(conn, name="Closed", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="CLSD", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            _fill(acc, inst, side=Side.BUY, quantity="1", price="100", ref="cl1"),
            _fill(acc, inst, side=Side.SELL, quantity="1", price="120", ref="cl2"),
        ],
    )
    await regroup_account(conn, acc)
    return acc


async def _make_orphaned_trade(conn, acc, *, symbol, ref) -> object:
    """Create one open trade in `acc` on its own fresh instrument, then
    protect it (per db/trades.py's protection path): give it notes so regroup
    preserves it as manual, delete its opening fill, and regroup again. The
    composite FK nulls opening_fill_id, and the protection UPDATE nulls
    open_quantity/open_cost_basis alongside it, while leaving status='open'
    untouched -- exactly the "unreachable instrument, still holds exposure"
    case this query exists to catch. Returns the trade's id, since two
    orphaned trades must never collapse into one row and the id is the key
    that proves it. Parameterized (rather than baked into a single fixture)
    so a test can create more than one of these, in the same account or in
    different ones."""
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency="USD"),
    )
    fill = _fill(acc, inst, side=Side.BUY, quantity="5", price="10", ref=ref)
    await insert_fills(conn, [fill])
    await regroup_account(conn, acc)
    trade = next(t for t in await list_trades(conn, acc) if t["opening_fill_id"] == fill.id)
    assert trade["status"] == "open"

    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", trade["id"])
    await conn.execute("DELETE FROM fill WHERE id = $1", fill.id)
    await regroup_account(conn, acc)

    protected = next(t for t in await list_trades(conn, acc) if t["id"] == trade["id"])
    assert protected["opening_fill_id"] is None
    assert protected["status"] == "open"
    return trade["id"]


@pytest_asyncio.fixture
async def orphaned_trade_account(conn):
    acc = await create_account(conn, name="Orphan", venue="manual", account_type="cash")
    await _make_orphaned_trade(conn, acc, symbol="ORPH", ref="or1")
    return acc


@pytest_asyncio.fixture
async def two_accounts(conn):
    """Two accounts, each with its own open position in a distinct symbol."""
    acc_a = await create_account(conn, name="A", venue="manual", account_type="cash")
    inst_a = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="AONE", quote_currency="USD"),
    )
    await insert_fills(
        conn, [_fill(acc_a, inst_a, side=Side.BUY, quantity="1", price="10", ref="a1")]
    )
    await regroup_account(conn, acc_a)

    acc_b = await create_account(conn, name="B", venue="manual", account_type="cash")
    inst_b = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="BTWO", quote_currency="USD"),
    )
    await insert_fills(
        conn, [_fill(acc_b, inst_b, side=Side.BUY, quantity="1", price="10", ref="b1")]
    )
    await regroup_account(conn, acc_b)

    return acc_a, acc_b


async def test_an_open_trade_appears_as_a_position(conn, seeded_account):
    """Seed via the same path production uses -- commit fills, regroup --
    so this test breaks if the persistence of open_quantity regresses."""
    ps = await open_positions(conn, seeded_account)
    assert [p.symbol for p in ps] == ["ZXCO"]
    assert ps[0].quantity == Decimal("10")


async def test_a_closed_trade_is_not_a_position(conn, closed_trade_account):
    assert await open_positions(conn, closed_trade_account) == ()


async def test_a_trade_whose_opening_fill_was_deleted_is_reported_not_dropped(
    conn, orphaned_trade_account
):
    """A protected trade has opening_fill_id NULL, so it cannot be joined to
    an instrument. Dropping it would understate the account's exposure with
    nothing saying so."""
    ps = await open_positions(conn, orphaned_trade_account)
    assert len(ps) == 1
    assert ps[0].unvaluable_reason is not None


async def test_positions_are_scoped_to_the_account_asked_for(conn, two_accounts):
    a, b = two_accounts
    assert {p.symbol for p in await open_positions(conn, a)} != {
        p.symbol for p in await open_positions(conn, b)
    }


async def test_two_orphaned_trades_in_the_same_account_are_two_rows(conn):
    """A shared sentinel for 'unknown instrument' would merge these into one
    row with a summed quantity and a cost basis averaged across two
    instruments that have nothing to do with each other -- meaningless, and
    worse than dropping the trade because the row looks like real
    information. Each orphaned trade must keep its own identity."""
    acc = await create_account(conn, name="TwoOrphans", venue="manual", account_type="cash")
    id1 = await _make_orphaned_trade(conn, acc, symbol="ORP1", ref="oa1")
    id2 = await _make_orphaned_trade(conn, acc, symbol="ORP2", ref="oa2")

    ps = await open_positions(conn, acc)

    assert len(ps) == 2
    assert {p.instrument_id for p in ps} == {id1, id2}
    assert all(p.trade_count == 1 for p in ps)
    assert all(p.unvaluable_reason is not None for p in ps)


async def test_two_orphaned_trades_in_different_accounts_do_not_merge_when_unscoped(conn):
    """The case Task 5's unscoped `deadband positions` (no --account) will
    actually hit: two orphaned trades that belong to different accounts must
    still not merge just because open_positions(conn, None) has no account
    filter to separate them. Scoped to the two trade ids this test itself
    created -- an unscoped call can see other committed data in this shared
    database, so this does not assert on the total row count."""
    acc_a = await create_account(conn, name="OrphanA", venue="manual", account_type="cash")
    acc_b = await create_account(conn, name="OrphanB", venue="manual", account_type="cash")
    id1 = await _make_orphaned_trade(conn, acc_a, symbol="ORPA", ref="ob1")
    id2 = await _make_orphaned_trade(conn, acc_b, symbol="ORPB", ref="ob2")

    by_id = {p.instrument_id: p for p in await open_positions(conn, None)}

    assert id1 in by_id
    assert id2 in by_id
    assert by_id[id1] is not by_id[id2]
    assert by_id[id1].trade_count == 1
    assert by_id[id2].trade_count == 1
