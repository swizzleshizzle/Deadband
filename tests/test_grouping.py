from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from ledger.grouping import group_fills
from ledger.types import Direction, Fill, FillSource, Side, TradeStatus

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
BTC = UUID("00000000-0000-0000-0000-0000000000b1")
ETH = UUID("00000000-0000-0000-0000-0000000000b2")
T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def fill(side, qty, price, minutes=0, instrument=BTC, account=ACC) -> Fill:
    return Fill(
        id=uuid4(),
        account_id=account,
        instrument_id=instrument,
        executed_at=T0 + timedelta(minutes=minutes),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=None,
        is_estimated=False,
    )


def total(group) -> Decimal:
    return sum((a.quantity for a in group.allocations), Decimal(0))


def test_simple_round_trip_is_one_closed_trade():
    fills = [fill(Side.BUY, "1", "100", 0), fill(Side.SELL, "1", "120", 10)]
    groups = group_fills(fills)
    assert len(groups) == 1
    g = groups[0]
    assert g.status is TradeStatus.CLOSED
    assert g.direction is Direction.LONG
    assert g.opened_at == T0
    assert g.closed_at == T0 + timedelta(minutes=10)
    assert total(g) == Decimal("2")


def test_scale_in_and_partial_exit_stays_one_open_trade():
    fills = [
        fill(Side.BUY, "0.5", "61200", 0),
        fill(Side.BUY, "0.5", "60800", 10),
        fill(Side.BUY, "1.0", "60100", 20),
        fill(Side.SELL, "1.0", "63400", 30),
    ]
    groups = group_fills(fills)
    assert len(groups) == 1
    assert groups[0].status is TradeStatus.OPEN
    assert groups[0].closed_at is None
    assert len(groups[0].allocations) == 4


def test_flat_then_reopen_is_two_trades():
    fills = [
        fill(Side.BUY, "1", "100", 0),
        fill(Side.SELL, "1", "110", 10),
        fill(Side.BUY, "1", "105", 20),
        fill(Side.SELL, "1", "115", 30),
    ]
    groups = group_fills(fills)
    assert len(groups) == 2
    assert all(g.status is TradeStatus.CLOSED for g in groups)


def test_short_trade_is_detected():
    fills = [fill(Side.SELL, "2", "100", 0), fill(Side.BUY, "2", "90", 10)]
    groups = group_fills(fills)
    assert len(groups) == 1
    assert groups[0].direction is Direction.SHORT
    assert groups[0].status is TradeStatus.CLOSED


def test_fill_crossing_zero_splits_across_two_trades():
    """Long 2, sell 3 => closes the long with 2 and opens a short with 1."""
    crossing = fill(Side.SELL, "3", "110", 10)
    fills = [fill(Side.BUY, "2", "100", 0), crossing]
    groups = group_fills(fills)

    assert len(groups) == 2
    closed, opened = groups[0], groups[1]

    assert closed.direction is Direction.LONG
    assert closed.status is TradeStatus.CLOSED
    assert {a.fill_id for a in closed.allocations} == {fills[0].id, crossing.id}
    assert next(a.quantity for a in closed.allocations if a.fill_id == crossing.id) == Decimal("2")

    assert opened.direction is Direction.SHORT
    assert opened.status is TradeStatus.OPEN
    assert {a.fill_id for a in opened.allocations} == {crossing.id}
    assert total(opened) == Decimal("1")


def test_allocations_of_a_fill_always_sum_to_its_quantity():
    crossing = fill(Side.SELL, "3", "110", 10)
    fills = [fill(Side.BUY, "2", "100", 0), crossing]
    groups = group_fills(fills)
    allocated = sum(
        (a.quantity for g in groups for a in g.allocations if a.fill_id == crossing.id),
        Decimal(0),
    )
    assert allocated == crossing.quantity


def test_different_instruments_do_not_mix():
    fills = [
        fill(Side.BUY, "1", "100", 0, instrument=BTC),
        fill(Side.BUY, "1", "50", 5, instrument=ETH),
        fill(Side.SELL, "1", "110", 10, instrument=BTC),
    ]
    groups = group_fills(fills)
    assert len(groups) == 2
    btc = [g for g in groups if g.instrument_ids == (BTC,)][0]
    eth = [g for g in groups if g.instrument_ids == (ETH,)][0]
    assert btc.status is TradeStatus.CLOSED
    assert eth.status is TradeStatus.OPEN


def test_same_instrument_in_different_accounts_does_not_mix():
    other = UUID("00000000-0000-0000-0000-0000000000a2")
    fills = [
        fill(Side.BUY, "1", "100", 0, account=ACC),
        fill(Side.SELL, "1", "110", 10, account=other),
    ]
    groups = group_fills(fills)
    assert len(groups) == 2
    assert all(g.status is TradeStatus.OPEN for g in groups)


def test_input_order_does_not_matter():
    a = fill(Side.BUY, "1", "100", 0)
    b = fill(Side.SELL, "1", "120", 10)
    assert group_fills([a, b]) == group_fills([b, a])


def test_empty_input_returns_empty_list():
    assert group_fills([]) == []
