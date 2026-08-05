from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

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
#
# FIX ROUND 2 UPDATE: the two-pass split above (Pass A before grouping, Pass B
# after) was itself a bug. Pass A excluded a protected trade's fills WHOLE via
# manual_fill_ids, but a zero-crossing fill can be only PARTLY that trade's —
# the rest belongs to a different trade that opens on the same fill. Excluding
# it whole starved that other trade of its share, which was then silently
# reaped by the final DELETE (0 over-allocations, 16/200 fuzz cases with an
# under-allocation — an open position disappeared). db/trades.py now uses a
# single protection step, AFTER grouping and BEFORE the final DELETE: every
# live fill is regrouped in full first (manual_fill_ids only ever excludes
# fills that were *already* manual going into this call), so there is nothing
# left to starve by the time a stale trade is converted to manual. A protected
# trade also has opening_fill_id and all derived P&L columns nulled — it owns
# zero fills, so leaving stale numbers on it would double-count against
# whatever trade its fills now belong to. Most of the Pass A/B tests above
# still pass unchanged against the unified code (the underlying behaviour they
# assert — a trade with content survives as manual, one without is deleted,
# protection runs before the final DELETE — did not change, only the
# mechanism); one (`test_orphaned_trade_with_notes_becomes_manual_and_survives_regroup`)
# had its expectations corrected below, because its old assertion that the
# surviving fill simply vanished was itself a symptom of the two-pass bug. See
# task-10-report.md "Fix round 2" for the zero-crossing repro and the fuzz run.


async def test_orphaned_trade_with_notes_becomes_manual_and_survives_regroup(conn):
    """Kills mutant A: without protection, this trade (orphaned by a deleted
    opening fill, carrying notes) is deleted instead of preserved.

    NOTE (fix round 2): the surviving SELL fill is NOT the deleted trade's alone
    — after its BUY opening fill is gone, that SELL fill must still be
    regrouped in full, forming its own new trade. (An earlier version of this
    test asserted `len(trades) == 1`, i.e. that the surviving fill vanished
    entirely — that was itself a symptom of the two-pass bug fix round 2
    corrected, not correct behaviour.)"""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    trade = (await list_trades(conn, acc))[0]
    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", trade["id"])
    await conn.execute("DELETE FROM fill WHERE id = $1", trade["opening_fill_id"])

    await regroup_account(conn, acc)
    trades = await list_trades(conn, acc)
    assert len(trades) == 2
    protected = next(t for t in trades if t["id"] == trade["id"])
    assert protected["notes"] == "keep me"
    assert protected["grouping_mode"] == "manual"
    reformed = next(t for t in trades if t["id"] != trade["id"])
    assert reformed["grouping_mode"] == "auto"
    assert reformed["qty_opened"] == Decimal("1")  # the surviving SELL, fully accounted for

    # Idempotent under repeated regroup: no duplicate ever appears for either.
    await regroup_account(conn, acc)
    await regroup_account(conn, acc)
    trades = await list_trades(conn, acc)
    assert len(trades) == 2
    assert {t["id"] for t in trades} == {protected["id"], reformed["id"]}


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


# --- Fix round 2 additions -------------------------------------------------


async def test_protection_does_not_starve_the_other_side_of_a_zero_crossing_fill(conn):
    """The exact bug fix-round-2 found: a fill that both closes one trade and
    opens another is only PARTLY the closing trade's. The old two-pass version
    (Pass A before grouping) excluded such a fill WHOLE whenever the closing
    trade got protected, starving the opening trade of its own share — which
    was then silently reaped by the final DELETE. SELL 1 @100 opens a short of
    1; BUY 5 @90 closes that short (1) and opens a long of 4 on the very same
    fill. Verified to fail against the two-pass code from fix round 1 (see
    task-10-report.md "Fix round 2" for the captured failure)."""
    acc = await create_account(conn, name="ZeroCross", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"),
    )
    sell = Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0,
        side=Side.SELL,
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id="s1",
        is_estimated=False,
    )
    buy = Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0 + timedelta(minutes=10),
        side=Side.BUY,
        quantity=Decimal("5"),
        price=Decimal("90"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id="b1",
        is_estimated=False,
    )
    await insert_fills(conn, [sell, buy])
    await regroup_account(conn, acc)

    trades = await list_trades(conn, acc)
    assert len(trades) == 2
    closed = next(t for t in trades if t["status"] == "closed")
    opened = next(t for t in trades if t["status"] == "open")
    assert opened["qty_opened"] == Decimal("4")

    # Protect the closed trade: give it notes, then delete ITS opening fill
    # (the SELL). The BUY fill — shared between both trades — stays live.
    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", closed["id"])
    await conn.execute("DELETE FROM fill WHERE id = $1", closed["opening_fill_id"])

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
            f"fill {r['id']} fill_qty={r['fill_qty']} allocated={r['allocated']} INVARIANT VIOLATED"
        )
    # The full net position from the one remaining fill (BUY 5) must still be
    # accounted for somewhere — not silently reduced to the stale 1 the closed
    # trade used to hold.
    assert sum(r["allocated"] for r in rows) == Decimal("5")


async def test_protected_trade_contributes_no_pnl(conn):
    """A protected trade owns zero fills after protection; leaving stale P&L on
    it would double-count against whatever trade its fills now belong to. Its
    own derived columns must all be NULL, and the account-wide sum of
    realized_pnl must reflect only the live trades."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    trade = (await list_trades(conn, acc))[0]
    assert trade["realized_pnl"] == Decimal("20")

    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", trade["id"])
    await conn.execute("DELETE FROM fill WHERE id = $1", trade["opening_fill_id"])
    await regroup_account(conn, acc)

    trades = await list_trades(conn, acc)
    protected = next(t for t in trades if t["id"] == trade["id"])
    assert protected["grouping_mode"] == "manual"
    assert protected["realized_pnl"] is None
    assert protected["gross_realized_pnl"] is None
    assert protected["qty_opened"] is None
    assert protected["qty_closed"] is None
    assert protected["avg_entry"] is None
    assert protected["avg_exit"] is None
    assert protected["fees_total"] is None
    assert protected["r_multiple"] is None

    # The surviving fill (the SELL) forms a brand-new open short with no
    # realized P&L yet, so the account-wide total must be exactly zero — not
    # the stale 20 the protected row used to hold.
    live_total = sum(
        (t["realized_pnl"] for t in trades if t["realized_pnl"] is not None), Decimal("0")
    )
    assert live_total == Decimal("0")


async def test_protected_trades_opening_fill_id_is_released(conn):
    """opening_fill_id must be freed on protection so a later regroup that
    happens to re-derive the same fill as an opening allocation creates a
    fresh trade rather than colliding with, and silently mutating, the
    protected manual row via ON CONFLICT."""
    acc = await seed(conn, [(Side.BUY, "1", "100"), (Side.SELL, "1", "120")])
    await regroup_account(conn, acc)
    trade = (await list_trades(conn, acc))[0]
    opening = trade["opening_fill_id"]
    inst = await conn.fetchval("SELECT instrument_id FROM fill WHERE id = $1", opening)

    await conn.execute("UPDATE trade SET notes = 'keep me' WHERE id = $1", trade["id"])

    # A backdated fill changes the grouping: opening_fill_id stays live but no
    # longer opens anything, so the trade gets protected via the "no longer
    # matches any group's opening" branch.
    earlier = Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0 - timedelta(minutes=30),
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("80"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id="earlier",
        is_estimated=False,
    )
    await insert_fills(conn, [earlier])
    await regroup_account(conn, acc)

    protected = next(t for t in await list_trades(conn, acc) if t["id"] == trade["id"])
    assert protected["grouping_mode"] == "manual"
    assert protected["opening_fill_id"] is None

    # Remove the backdated fill so the original opening fill would, on its
    # own, re-derive the exact same opening allocation as before.
    await conn.execute("DELETE FROM fill WHERE id = $1", earlier.id)
    await regroup_account(conn, acc)

    still_protected = next(t for t in await list_trades(conn, acc) if t["id"] == trade["id"])
    assert still_protected["grouping_mode"] == "manual"
    assert still_protected["notes"] == "keep me"
    assert still_protected["opening_fill_id"] is None
    assert still_protected["realized_pnl"] is None

    # A brand-new trade formed instead, entirely separate from the protected one.
    new_trade = next(t for t in await list_trades(conn, acc) if t["opening_fill_id"] == opening)
    assert new_trade["id"] != trade["id"]


# --- Fix round 3 addition ---------------------------------------------------


async def test_regroup_refuses_a_manual_trade_holding_a_partial_fill(conn):
    """The same bug shape as round 2's Pass A failure, reached via a different
    path: a manual trade holding only PART of a fill (not the whole thing) would
    make manual_fill_ids exclude that fill WHOLE from the auto pass, stranding
    the rest of its quantity. Nothing in db/, ledger/, or importers/ creates this
    state today — the only writer of grouping_mode='manual' is the protection
    step, which drops its allocations first — but a hand-marked manual trade
    (exactly what a future "group these fills manually" UI would do, and exactly
    what test_regroup_does_not_touch_manual_trades does via a plain UPDATE)
    could. regroup_account must fail loudly instead of silently losing an open
    position.

    SELL 1 @100 opens a short of 1; BUY 5 @90 closes that short (quantity 1,
    partial) and opens a long of 4 on the same fill. Hand-marking the closed
    trade manual leaves it holding only 1 of the BUY fill's 5 units."""
    acc = await create_account(conn, name="PartialManual", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"),
    )
    sell = Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0,
        side=Side.SELL,
        quantity=Decimal("1"),
        price=Decimal("100"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id="s1",
        is_estimated=False,
    )
    buy = Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0 + timedelta(minutes=10),
        side=Side.BUY,
        quantity=Decimal("5"),
        price=Decimal("90"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id="b1",
        is_estimated=False,
    )
    await insert_fills(conn, [sell, buy])
    await regroup_account(conn, acc)

    closed = next(t for t in await list_trades(conn, acc) if t["status"] == "closed")
    # Hand-mark it manual with a plain UPDATE, exactly as
    # test_regroup_does_not_touch_manual_trades does — this is what a future
    # manual-grouping UI would do, and it leaves the trade holding only 1 of the
    # BUY fill's 5 units.
    await conn.execute("UPDATE trade SET grouping_mode = 'manual' WHERE id = $1", closed["id"])

    with pytest.raises(NotImplementedError, match=str(buy.id)):
        await regroup_account(conn, acc)
