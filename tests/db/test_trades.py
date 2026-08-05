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
    """IMPORTANT 2 fix: the original version of this test flipped the account's
    only trade to manual, which empties `fills` and makes `regroup_account` hit
    its early-exit path without the auto pass ever running — the assertion
    passed for the wrong reason. A second instrument with its own live fill
    forces the auto pass to actually execute alongside the untouched manual
    trade."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    await conn.execute(
        "UPDATE trade SET grouping_mode = 'manual', notes = 'keep me' WHERE account_id = $1",
        acc,
    )

    other_inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="QQQ", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=other_inst,
                executed_at=T0 + timedelta(hours=5),
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("50"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="q1",
                is_estimated=False,
            )
        ],
    )

    written = await regroup_account(conn, acc)
    assert written == 1  # proves the auto pass actually ran, on the QQQ fill

    trades = await list_trades(conn, acc)
    assert len(trades) == 2
    manual = [t for t in trades if t["grouping_mode"] == "manual"]
    assert len(manual) == 1
    assert manual[0]["notes"] == "keep me"


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


# --- Fix round 1 additions -------------------------------------------------
#
# CRITICAL 1: manual_fill_ids used to be computed BEFORE the protect-UPDATE ran
# (at the very end), so an orphaned trade's surviving fill got double-allocated
# to both the old (now-manual) trade and a freshly-grouped auto trade. Fixed by
# splitting the protection into Pass A (runs before manual_fill_ids is computed)
# and Pass B (runs after grouping, and drops stale allocations it protects).
#
# CRITICAL 2: three mutants of the amendment-2 protection survived the original
# suite with no test able to catch them. The tests below were each verified, in
# a scratch copy of db/trades.py, to fail under the corresponding mutant:
#
#   Mutant A — delete the Pass A and Pass B protect-UPDATEs entirely, leaving
#              only the final DELETE.
#   Mutant B — swap the order so DELETE runs before the protect-UPDATEs.
#   Mutant C — add `intent = EXCLUDED.intent` to the trade upsert's DO UPDATE SET.
#
# See task-10-report.md "Fix round 1" section for the exact diffs and captured
# failure output.


async def test_orphaned_trade_with_notes_becomes_manual_and_survives_regroup(conn):
    """Kills mutant A: without Pass A protection, this trade (orphaned by a
    deleted opening fill, carrying notes) is deleted instead of preserved."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    trade = (await list_trades(conn, acc))[0]
    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", trade["id"])
    await conn.execute("DELETE FROM fill WHERE id = $1", trade["opening_fill_id"])

    await regroup_account(conn, acc)
    trades = await list_trades(conn, acc)
    assert len(trades) == 1
    assert trades[0]["id"] == trade["id"]
    assert trades[0]["notes"] == "keep me"
    assert trades[0]["grouping_mode"] == "manual"

    # Idempotent under repeated regroup: no duplicate ever appears for it.
    await regroup_account(conn, acc)
    await regroup_account(conn, acc)
    trades = await list_trades(conn, acc)
    assert len(trades) == 1
    assert trades[0]["id"] == trade["id"]


async def test_orphaned_trade_without_user_content_is_deleted(conn):
    """The counterpart to the test above: a trade with nothing to protect is
    genuinely stale and must be reaped, not converted to manual."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    original = (await list_trades(conn, acc))[0]
    await conn.execute("DELETE FROM fill WHERE id = $1", original["opening_fill_id"])

    await regroup_account(conn, acc)

    ids = {t["id"] for t in await list_trades(conn, acc)}
    assert original["id"] not in ids


async def test_pass_b_protects_a_trade_orphaned_by_a_regroup_before_the_final_delete(conn):
    """Kills mutant B. Unlike the Pass A tests above (opening_fill_id IS NULL,
    protected before grouping even runs), this exercises Pass B: a backdated
    fill changes which fill opens the trade, so the *old* trade's
    opening_fill_id is still non-NULL but no longer matches any group's
    opening. If the final DELETE ever runs before Pass B's protect-UPDATE, this
    trade — despite carrying notes — is deleted before Pass B gets a chance to
    save it."""
    acc = await create_account(conn, name="T2", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"),
    )
    f1 = Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0 + timedelta(minutes=10),
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id="v1",
        is_estimated=False,
    )
    sell = Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0 + timedelta(minutes=20),
        side=Side.SELL,
        quantity=Decimal("1"),
        price=Decimal("120"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id="v2",
        is_estimated=False,
    )
    await insert_fills(conn, [f1, sell])
    await regroup_account(conn, acc)
    original = (await list_trades(conn, acc))[0]
    assert original["opening_fill_id"] == f1.id
    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", original["id"])

    # A fill executed BEFORE f1 rewrites the grouping: the trade never returns to
    # flat (BUY 1 + BUY 1 - SELL 1 = open long 1), so f0/f1/sell merge into one
    # open trade whose opening allocation is now f0, not f1.
    f0 = Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0,
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("90"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id="v0",
        is_estimated=False,
    )
    await insert_fills(conn, [f0])
    await regroup_account(conn, acc)

    trades = {t["id"]: t for t in await list_trades(conn, acc)}
    assert original["id"] in trades, "the notes-carrying trade must survive, not be deleted"
    protected = trades[original["id"]]
    assert protected["grouping_mode"] == "manual"
    assert protected["notes"] == "keep me"

    new_trade = next(t for t in trades.values() if t["id"] != original["id"])
    assert new_trade["opening_fill_id"] == f0.id
    assert new_trade["qty_opened"] == Decimal("2")

    # The protected trade's stale allocations must be gone — f1 and sell now
    # belong only to the new trade, never both.
    rows = await conn.fetch(
        """
        SELECT f.id, f.quantity AS fill_qty, COALESCE(SUM(tf.quantity), 0) AS allocated
          FROM fill f
          LEFT JOIN trade_fill tf ON tf.fill_id = f.id
         WHERE f.account_id = $1
         GROUP BY f.id, f.quantity
        """,
        acc,
    )
    for r in rows:
        assert r["allocated"] == r["fill_qty"]


async def test_intent_override_survives_regroup(conn):
    """Kills mutant C: if the trade upsert's DO UPDATE SET ever writes
    `intent = EXCLUDED.intent` unconditionally, a user's override is silently
    clobbered back to the account's default on the very next regroup."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    trade = (await list_trades(conn, acc))[0]
    await conn.execute("UPDATE trade SET intent = 'investment' WHERE id = $1", trade["id"])

    await regroup_account(conn, acc)

    updated = (await list_trades(conn, acc))[0]
    assert updated["intent"] == "investment"


async def test_pass_a_protection_does_not_double_allocate_fills(conn):
    """CRITICAL 1's exact reproduction: after an orphaned trade with notes is
    protected, the fill it still shares with a new auto trade must be allocated
    exactly once — never zero, never twice — across repeated regroups."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    trade = (await list_trades(conn, acc))[0]
    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", trade["id"])
    await conn.execute("DELETE FROM fill WHERE id = $1", trade["opening_fill_id"])

    for i in range(4):
        await regroup_account(conn, acc)
        rows = await conn.fetch(
            """
            SELECT f.id, f.quantity AS fill_qty, COALESCE(SUM(tf.quantity), 0) AS allocated
              FROM fill f
              LEFT JOIN trade_fill tf ON tf.fill_id = f.id
             WHERE f.account_id = $1
             GROUP BY f.id, f.quantity
            """,
            acc,
        )
        for r in rows:
            assert r["allocated"] == r["fill_qty"], (
                f"regroup #{i + 1}: fill {r['id']} fill_qty={r['fill_qty']} "
                f"allocated={r['allocated']} INVARIANT VIOLATED"
            )


async def test_regroup_removes_stale_auto_trades_when_all_fills_are_deleted(conn):
    """IMPORTANT 1: the old early `if not fills: return 0` skipped cleanup
    entirely, so an account whose fills were all deleted kept reporting phantom
    auto trades with stale P&L forever."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    assert len(await list_trades(conn, acc)) == 1

    await conn.execute("DELETE FROM fill WHERE account_id = $1", acc)
    await regroup_account(conn, acc)

    assert await list_trades(conn, acc) == []


async def test_primary_underlying_rolls_options_up_to_their_stock(conn):
    """IMPORTANT 3: primary_underlying must store COALESCE(underlying, symbol),
    not the bare instrument symbol — otherwise an option's contract string
    ('SPY 260821C00500000') never rolls up with its stock's own trades."""
    acc = await create_account(conn, name="Opt", venue="manual", account_type="cash")
    opt = await upsert_instrument(
        conn,
        Instrument(
            id=None,
            asset_class=AssetClass.OPTION,
            symbol="SPY 260821C00500000",
            quote_currency="USD",
            underlying="SPY",
            strike=Decimal("500"),
            expiry=datetime(2026, 8, 21, tzinfo=UTC).date(),
            option_right="call",
            contract_multiplier=Decimal("100"),
        ),
    )
    await insert_fills(
        conn,
        [
            Fill(
                id=uuid4(),
                account_id=acc,
                instrument_id=opt,
                executed_at=T0,
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("5"),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id="o1",
                is_estimated=False,
            )
        ],
    )
    await regroup_account(conn, acc)
    trade = (await list_trades(conn, acc))[0]
    assert trade["primary_underlying"] == "SPY"
