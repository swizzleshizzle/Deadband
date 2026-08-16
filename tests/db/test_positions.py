from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest_asyncio

from db.accounts import create_account
from db.corporate import add_action
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.positions import open_positions
from db.trades import list_trades, regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db
from tests.db.conftest import _merger, _split, _symbol_change

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


@pytest_asyncio.fixture
async def option_account(conn):
    """One open long of 2 option contracts, contract_multiplier 100.

    Every other fixture in this file (and in tests/db/test_cli.py) uses
    AssetClass.EQUITY, whose multiplier is 1 -- so nothing distinguished
    "read the column" from "assume 1"."""
    acc = await create_account(conn, name="Option", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(
            id=None,
            asset_class=AssetClass.OPTION,
            symbol="ZXCO  261218C00050000",
            quote_currency="USD",
            underlying="ZXCO",
            strike=Decimal("50"),
            expiry=datetime(2026, 12, 18, tzinfo=UTC).date(),
            option_right="call",
            contract_multiplier=Decimal("100"),
        ),
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, quantity="2", price="2.50", ref="opt1")]
    )
    await regroup_account(conn, acc)
    return acc


@pytest_asyncio.fixture
async def estimated_fill_account(conn):
    """One open position whose only fill is flagged estimated.

    `estimated=False` is this file's default and was never overridden, so
    db/positions.py's `is_estimated=r["is_estimated"]` could be replaced with
    a hardcoded False and stay green."""
    acc = await create_account(conn, name="Estimated", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ESTM", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [_fill(acc, inst, side=Side.BUY, quantity="6", price="11", ref="es1", estimated=True)],
    )
    await regroup_account(conn, acc)
    return acc


async def test_an_open_trade_appears_as_a_position(conn, seeded_account):
    """Seed via the same path production uses -- commit fills, regroup --
    so this test breaks if the persistence of open_quantity regresses."""
    ps = await open_positions(conn, seeded_account)
    assert [p.symbol for p in ps] == ["ZXCO"]
    assert ps[0].quantity == Decimal("10")


async def test_the_instruments_contract_multiplier_reaches_the_position(conn, option_account):
    """Final-review finding (Important 3): db/positions.py reads
    `i.contract_multiplier`, but every fixture on this branch was an equity,
    where that column is 1 -- so `multiplier=Decimal(1)` in place of the read
    survived all 464 tests. On this option it is the difference between an
    unrealized P&L of 120 and one of 1.20."""
    ps = await open_positions(conn, option_account)
    assert len(ps) == 1
    assert ps[0].multiplier == Decimal("100")
    assert ps[0].quantity == Decimal("2")
    assert ps[0].cost_basis == Decimal("2.50")
    assert ps[0].unvaluable_reason is None


async def test_an_estimated_fill_marks_the_position_estimated(conn, estimated_fill_account):
    """Final-review finding (Important 4): `is_estimated` was False in every
    fixture below the pure layer, so the read in db/positions.py was ungated.
    It is the only signal separating a P&L computed from a real fill from one
    computed against a guessed price."""
    ps = await open_positions(conn, estimated_fill_account)
    assert len(ps) == 1
    assert ps[0].is_estimated is True


async def test_a_closed_trade_is_not_a_position(conn, closed_trade_account):
    assert await open_positions(conn, closed_trade_account) == ()


async def test_a_trade_whose_opening_fill_was_deleted_is_reported_not_dropped(
    conn, orphaned_trade_account
):
    """A protected trade has opening_fill_id NULL, so it cannot be joined to
    an instrument. Dropping it would understate the account's exposure with
    nothing saying so.

    Also pins the seam this task touched: the instrument join failing (hence
    `unvaluable_reason`) must not take the account join down with it. That
    join is a plain, non-orphaning INNER JOIN on trade.account_id, which is
    NOT NULL and always reachable -- unlike the instrument join, there is no
    "protected" state for it to fail into."""
    ps = await open_positions(conn, orphaned_trade_account)
    assert len(ps) == 1
    assert ps[0].unvaluable_reason is not None
    assert ps[0].account_id == orphaned_trade_account
    assert ps[0].account_name == "Orphan"


async def test_an_empty_symbol_is_not_labelled_an_unknown_instrument(conn):
    """Final-review finding (M5): the fallback used to be
    `r["symbol"] or "(unknown instrument)"`, keyed off the symbol's own
    truthiness. `instrument.symbol` is TEXT NOT NULL with no non-empty check,
    so a reachable instrument with an empty symbol -- a thin importer or a
    hand-inserted row -- got labelled "(unknown instrument)" while its
    quantity, basis and mark were still priced normally: a row that
    contradicts itself. Reachability is what decides the label."""
    acc = await create_account(conn, name="EmptySym", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="", quote_currency="USD"),
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, quantity="2", price="9", ref="es1")]
    )
    await regroup_account(conn, acc)

    ps = await open_positions(conn, acc)

    assert len(ps) == 1
    assert ps[0].symbol == ""
    assert ps[0].instrument_id == inst  # reachable, so not keyed on the trade id
    assert ps[0].unvaluable_reason is None  # and still perfectly valuable


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
    # The instrument join failing on both rows must not take the account
    # join down with it -- both still carry the one real account they share.
    assert all(p.account_id == acc for p in ps)
    assert all(p.account_name == "TwoOrphans" for p in ps)


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
    # trade_count == 1 on each, not the "is not" identity check the two `in`
    # lookups above would already make trivially true, is what actually
    # proves they did not merge into a single combined row.
    assert by_id[id1].trade_count == 1
    assert by_id[id2].trade_count == 1


async def test_two_accounts_holding_the_same_instrument_are_two_rows(conn):
    """The behaviour this task exists for: grouping is now (account_id,
    instrument_id), not instrument_id alone. Before, one open long in a
    taxable account and one open short in a retirement account -- both on
    the SAME instrument -- merged into a single row: a blended cost basis
    that has no meaning across two accounts with different tax treatment,
    and a manufactured 'mixed direction' that exists nowhere in reality
    (each leg is an ordinary, individually valuable position on its own).

    Same instrument via upsert_instrument's natural-key idempotency (see
    db/instruments.py) -- calling it twice with the same equity symbol
    returns the same instrument id, so this exercises a genuine shared
    instrument rather than two coincidentally-identical ones."""
    acc_a = await create_account(conn, name="Taxable", venue="manual", account_type="cash")
    acc_b = await create_account(conn, name="Retirement", venue="manual", account_type="cash")
    inst_a = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SHRD", quote_currency="USD"),
    )
    inst_b = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SHRD", quote_currency="USD"),
    )
    assert inst_a == inst_b  # same natural key -> same row, not a coincidence

    await insert_fills(
        conn, [_fill(acc_a, inst_a, side=Side.BUY, quantity="10", price="20", ref="sh-a")]
    )
    await regroup_account(conn, acc_a)
    await insert_fills(
        conn, [_fill(acc_b, inst_b, side=Side.SELL, quantity="4", price="50", ref="sh-b")]
    )
    await regroup_account(conn, acc_b)

    ps = await open_positions(conn, None)
    by_account = {p.account_id: p for p in ps if p.instrument_id == inst_a}

    assert len(by_account) == 2
    assert by_account[acc_a].account_name == "Taxable"
    assert by_account[acc_a].quantity == Decimal("10")
    assert by_account[acc_a].cost_basis == Decimal("20")
    assert by_account[acc_a].unvaluable_reason is None  # not "mixed direction"
    assert by_account[acc_b].account_name == "Retirement"
    assert by_account[acc_b].quantity == Decimal("4")
    assert by_account[acc_b].cost_basis == Decimal("50")
    assert by_account[acc_b].unvaluable_reason is None  # not "mixed direction"


async def test_an_orphaned_trade_with_a_real_quantity_is_still_unvaluable(conn):
    """A trade can lose its opening fill (the composite FK's
    ON DELETE SET NULL) without ever going through regroup_account's
    protection step -- e.g. a future delete-a-fill action with no immediate
    regroup. In that state open_quantity is NOT NULL: nothing in the schema
    ties nulling opening_fill_id to nulling open_quantity, and today's other
    fixtures never exercise it because they always regroup afterward. If
    open_positions forwarded that real quantity, the resulting row would
    show a real, priceable-looking number under a trade-id standing in for
    an instrument id -- a wrong number presented as a real one. It must be
    unvaluable with no quantity instead."""
    acc = await create_account(conn, name="RealQty", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="RLQT", quote_currency="USD"),
    )
    fill = _fill(acc, inst, side=Side.BUY, quantity="7", price="10", ref="rq1")
    await insert_fills(conn, [fill])
    await regroup_account(conn, acc)
    trade = (await list_trades(conn, acc))[0]
    assert trade["open_quantity"] == Decimal("7")

    # No notes, no second regroup -- just the FK firing on its own, which is
    # the exact state db/trades.py's protection step never gets a chance to
    # touch.
    await conn.execute("DELETE FROM fill WHERE id = $1", fill.id)

    orphaned = (await list_trades(conn, acc))[0]
    assert orphaned["opening_fill_id"] is None
    assert orphaned["open_quantity"] == Decimal("7")  # confirms this is real, not already NULL
    assert orphaned["status"] == "open"

    ps = await open_positions(conn, acc)

    assert len(ps) == 1
    assert ps[0].unvaluable_reason is not None
    assert ps[0].quantity == Decimal(0)  # the real 7 must NOT be reported


async def test_a_symbol_change_reports_the_position_under_the_new_instrument(
    conn, account_with_1800, zxcb
):
    """Before this, open_positions resolved the instrument from the RAW opening
    fill, which the adjustment never rewrites -- so the position kept reporting
    under the old symbol while `deadband trades` reported the new one, and a mark
    on the new symbol never priced it.

    Regrouped once BEFORE the action exists (so the trade row is INSERTed with
    effective_instrument_id pointing at the raw instrument) and once AFTER (so
    the same row is hit by the UPSERT's DO UPDATE branch, not another INSERT).
    A single regroup only ever exercises the INSERT branch, which would leave
    an UPDATE-only regression -- e.g. effective_instrument_id dropped from the
    DO UPDATE SET list -- undetected."""
    account_id, instrument_id = account_with_1800
    await regroup_account(conn, account_id)
    await add_action(conn, _symbol_change(instrument_id, zxcb))
    await regroup_account(conn, account_id)
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == zxcb
    assert position.symbol == "ZXCB"
    assert position.quantity == Decimal(1800)


async def test_a_merger_reports_the_new_instrument_and_the_rescaled_quantity(
    conn, account_with_1800, zxcb
):
    """A merger changes instrument AND magnitude. 1800 at 1:6 -> 300 of ZXCB."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _merger(instrument_id, zxcb))
    await regroup_account(conn, account_id)
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == zxcb
    assert position.quantity == Decimal(300)


async def test_a_reverse_split_leaves_the_instrument_alone(conn, account_with_1800):
    """effective_instrument_id is written for every trade, not only relabelled
    ones. A split must record the instrument it already had, not NULL and not
    something else."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _split(instrument_id))
    await regroup_account(conn, account_id)
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == instrument_id
    assert position.quantity == Decimal(300)


async def test_a_trade_predating_the_column_still_reports_its_fills_instrument(
    conn, account_with_1800
):
    """effective_instrument_id is nullable and pre-existing rows are not
    backfilled, so the COALESCE fallback is load-bearing rather than defensive."""
    account_id, instrument_id = account_with_1800
    await regroup_account(conn, account_id)
    await conn.execute(
        "UPDATE trade SET effective_instrument_id = NULL WHERE account_id = $1", account_id
    )
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == instrument_id


async def test_a_trade_with_no_opening_fill_but_an_effective_instrument_still_resolves(
    conn, account_with_1800
):
    """Pins the shape Task 3's spinoff-derived trades will have: opening_fill_id
    NULL (a spinoff's opening allocation is a synthetic row in `derived_fill`,
    which has no `fill` counterpart -- that is the entire reason `derived_fill`
    exists), but opening_derived_fill_id AND effective_instrument_id both set.

    This branch first gated db/positions.py's instrument join on
    `t.opening_fill_id IS NOT NULL` alone, reasoning that opening_fill_id
    nullity meant "orphaned, so unreachable." That gate would have made every
    spinoff-derived position resolve no instrument and no symbol at all --
    wrong for a trade that isn't orphaned, just fillless by construction. The
    gate must accept EITHER opening column: opening_fill_id for a normal
    trade, opening_derived_fill_id for a spinoff-derived one.

    Task 3 does not exist yet, so a derived_fill row is constructed by hand
    here purely to give opening_derived_fill_id (a real FK) something valid
    to reference -- its content is not semantically a spinoff, only a row
    the constraint will accept."""
    account_id, instrument_id = account_with_1800
    await regroup_account(conn, account_id)
    action_id = await add_action(conn, _split(instrument_id))
    fill_id = await conn.fetchval("SELECT id FROM fill WHERE account_id = $1", account_id)
    derived_id = uuid4()
    await conn.execute(
        """
        INSERT INTO derived_fill (
            id, account_id, instrument_id, executed_at, side, quantity, price,
            derived_from_fill_id, corporate_action_id
        ) VALUES ($1, $2, $3, now(), 'buy', 1, 1, $4, $5)
        """,
        derived_id, account_id, instrument_id, fill_id, action_id,
    )
    await conn.execute(
        "UPDATE trade SET opening_fill_id = NULL, opening_derived_fill_id = $2 "
        "WHERE account_id = $1",
        account_id, derived_id,
    )
    (position,) = await open_positions(conn, account_id)
    assert position.instrument_id == instrument_id
    assert position.symbol == "ZXCO"
    assert position.quantity == Decimal(1800)


async def test_a_trade_with_both_openings_null_resolves_no_instrument(
    conn, account_with_1800
):
    """The genuine orphan case, exactly what regroup_account's protection step
    leaves behind: opening_fill_id AND effective_instrument_id both NULL. This
    must stay unresolvable -- proving the previous test's resolution comes
    specifically from effective_instrument_id being set, not from some other
    leniency in the query."""
    account_id, instrument_id = account_with_1800
    await regroup_account(conn, account_id)
    await conn.execute(
        "UPDATE trade SET opening_fill_id = NULL, effective_instrument_id = NULL "
        "WHERE account_id = $1",
        account_id,
    )
    (position,) = await open_positions(conn, account_id)
    assert position.unvaluable_reason is not None
    assert position.quantity == Decimal(0)
