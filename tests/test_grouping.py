from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext
from fractions import Fraction
from uuid import UUID, uuid4

import pytest

from ledger.grouping import TransferError, group_fills
from ledger.types import AssetTransfer, Direction, Fill, FillSource, Side, TradeStatus

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


def test_rejects_fills_with_none_id():
    """Fills without persisted IDs cannot be grouped."""
    from dataclasses import replace

    f = fill(Side.BUY, "1", "100")
    f_no_id = replace(f, id=None)
    with pytest.raises(ValueError, match="group_fills requires persisted fills"):
        group_fills([f_no_id])


def test_large_magnitude_counterexample_1e28():
    """Buy 1e28, buy 1, sell 1e28, sell 1 should be exactly one closed long trade."""
    fills = [
        fill(Side.BUY, "1e28", "1", 0),
        fill(Side.BUY, "1", "1", 10),
        fill(Side.SELL, "1e28", "1", 20),
        fill(Side.SELL, "1", "1", 30),
    ]
    groups = group_fills(fills)

    # Should be exactly one closed LONG trade
    assert len(groups) == 1
    assert groups[0].direction is Direction.LONG
    assert groups[0].status is TradeStatus.CLOSED

    # All four fills should be allocated exactly to their quantities.
    #
    # Summed with Fraction, NOT Decimal: at ambient precision (28 digits),
    # Decimal("1e28") + Decimal("1") rounds the "1" away identically on BOTH
    # sides of a Decimal-summed comparison (`total_allocated == total_fills`),
    # so the old assertion held even if the grouper silently dropped both unit
    # fills — it could never fail. test_dust_allocation_precision nearby
    # already uses Fraction for exact arithmetic for the same reason; this
    # test now does too, so the two 1e28-magnitude unit fills are actually
    # checked rather than rounded into invisibility on both sides at once.
    total_allocated = sum((Fraction(a.quantity) for a in groups[0].allocations), Fraction(0))
    total_fills = sum((Fraction(f.quantity) for f in fills), Fraction(0))
    assert total_allocated == total_fills
    assert total_fills == Fraction(2 * 10**28 + 2)  # sanity: not a tautological 0 == 0


def test_dust_allocation_precision():
    """Large buy after dust sell does not over-allocate under Decimal rounding."""
    # Sell 0.000000000000000001, buy 1000000000000
    # Pre-fix bug: large_buy over-allocated to 1000000000000.000000000000000001
    tiny_sell = fill(Side.SELL, "0.000000000000000001", "100", 0)
    large_buy = fill(Side.BUY, "1000000000000", "100", 10)
    fills = [large_buy, tiny_sell]
    groups = group_fills(fills)

    # The large_buy must be allocated exactly to its quantity across all trades
    # Use Fraction for exact arithmetic (Decimal could still round)
    allocated = sum(
        (Fraction(a.quantity) for g in groups for a in g.allocations if a.fill_id == large_buy.id),
        Fraction(0),
    )
    assert allocated == Fraction(large_buy.quantity)


def test_ambient_context_independence():
    """Computation is independent of ambient Decimal precision via high-prec context."""
    # Pre-fix bug: at prec 3, this diverged to 2 trades + spurious 0.005 short
    # at prec 10/28, one closed long trade
    a = fill(Side.BUY, "1.005", "100", 0)
    b = fill(Side.SELL, "0.001", "100", 10)
    c = fill(Side.SELL, "1.004", "100", 20)

    # Save original context
    original_prec = getcontext().prec

    try:
        results_by_prec = {}
        for prec in [3, 10, 28]:
            getcontext().prec = prec
            result = group_fills([a, b, c])
            results_by_prec[prec] = result

        # All three should produce identical results (1 closed long trade)
        # Compare by allocations structure as strings, not Decimal values
        def allocations_to_strs(trade_groups):
            strs = []
            for g in trade_groups:
                for alloc in g.allocations:
                    strs.append((str(alloc.fill_id), str(alloc.quantity)))
            return sorted(strs)

        expected_strs = allocations_to_strs(results_by_prec[28])
        for prec in [3, 10]:
            result = results_by_prec[prec]
            assert len(result) == 1, f"prec {prec}: expected 1 trade, got {len(result)}"
            actual_strs = allocations_to_strs(result)
            assert actual_strs == expected_strs, (
                f"prec {prec} diverged: {actual_strs} != {expected_strs}"
            )
    finally:
        # Restore original precision
        getcontext().prec = original_prec


def test_multi_bucket_order_independence():
    """Grouping is invariant to input order even across multiple (account, instrument) buckets."""
    acc2 = UUID("00000000-0000-0000-0000-0000000000a2")

    # Fills from two different accounts
    a1 = fill(Side.BUY, "1", "100", 0, account=ACC)
    a1_sell = fill(Side.SELL, "1", "110", 10, account=ACC)
    a2 = fill(Side.BUY, "1", "100", 0, account=acc2)
    a2_sell = fill(Side.SELL, "1", "110", 10, account=acc2)

    # Forward order
    result_forward = group_fills([a1, a1_sell, a2, a2_sell])

    # Reversed order
    result_reversed = group_fills([a2_sell, a2, a1_sell, a1])

    assert result_forward == result_reversed


def test_simultaneous_fills_deterministic_by_id():
    """Fills with identical timestamps are sorted by str(id) for determinism."""
    id_a = UUID("00000000-0000-0000-0000-000000000001")
    id_b = UUID("00000000-0000-0000-0000-000000000002")

    # Create fills with known IDs
    from dataclasses import replace

    buy_a = replace(fill(Side.BUY, "1", "100", 0), id=id_a)
    sell_b = replace(fill(Side.SELL, "1", "110", 0), id=id_b)

    # Forward order
    result_forward = group_fills([buy_a, sell_b])

    # Reversed order
    result_reversed = group_fills([sell_b, buy_a])

    # Results should be identical because buy_a's id sorts before sell_b's id
    assert result_forward == result_reversed
    assert len(result_forward) == 1
    assert result_forward[0].status is TradeStatus.CLOSED


# --- transfers (branch B): reduce-only closing events. Loud or not at all --
# --- never clamped, never against a short, never with nothing held.


def xfer(qty, minutes=0, instrument=BTC, account=ACC) -> AssetTransfer:
    return AssetTransfer(
        id=uuid4(),
        account_id=account,
        instrument_id=instrument,
        occurred_at=T0 + timedelta(minutes=minutes),
        quantity=Decimal(qty),
        market_value=None,
    )


def test_transfer_out_closes_the_position():
    fills = [fill(Side.BUY, "40", "6", 0)]
    groups = group_fills(fills, [xfer("40", minutes=10)])
    assert len(groups) == 1
    g = groups[0]
    assert g.status is TradeStatus.CLOSED
    assert g.closed_at == T0 + timedelta(minutes=10)
    assert len(g.transfers) == 1
    assert g.transfers[0].quantity == Decimal(40)
    assert total(g) == Decimal(40)  # allocations hold only the BUY


def test_partial_transfer_leaves_the_trade_open():
    fills = [fill(Side.BUY, "40", "6", 0)]
    groups = group_fills(fills, [xfer("15", minutes=10)])
    assert len(groups) == 1
    g = groups[0]
    assert g.status is TradeStatus.OPEN
    assert g.closed_at is None
    assert g.transfers[0].quantity == Decimal(15)


def test_transfer_exceeding_position_raises():
    fills = [fill(Side.BUY, "10", "6", 0)]
    with pytest.raises(TransferError):
        group_fills(fills, [xfer("45", minutes=10)])


def test_transfer_against_a_short_position_raises():
    fills = [fill(Side.SELL, "10", "6", 0)]
    with pytest.raises(TransferError):
        group_fills(fills, [xfer("5", minutes=10)])


def test_transfer_with_no_open_position_raises():
    with pytest.raises(TransferError):
        group_fills([], [xfer("5")])


def test_same_timestamp_fill_processes_before_transfer():
    # BUY and transfer share one timestamp: the buy opens, the transfer closes.
    fills = [fill(Side.BUY, "40", "6", 0)]
    groups = group_fills(fills, [xfer("40", minutes=0)])
    assert len(groups) == 1
    assert groups[0].status is TradeStatus.CLOSED


def test_transfer_only_touches_its_own_instrument():
    fills = [fill(Side.BUY, "40", "6", 0), fill(Side.BUY, "7", "9", 0, instrument=ETH)]
    groups = group_fills(fills, [xfer("40", minutes=10)])
    by_inst = {g.instrument_ids[0]: g for g in groups}
    assert by_inst[BTC].status is TradeStatus.CLOSED
    assert by_inst[ETH].status is TradeStatus.OPEN
    assert by_inst[ETH].transfers == ()


def test_midnight_stamped_transfer_processes_after_same_day_intraday_fills():
    """Fidelity stamps date-only rows at midnight while manual fills can carry
    intraday times, so a same-day transfer must sort after that DAY's fills --
    a broker's executions precede its end-of-day ACAT snapshot -- or a
    perfectly ordinary buy-then-transfer day raises a spurious TransferError."""
    buy = fill(Side.BUY, "40", "6", minutes=6 * 60)  # 15:00 UTC, same day as T0 (09:00)
    midnight = AssetTransfer(
        id=uuid4(),
        account_id=ACC,
        instrument_id=BTC,
        occurred_at=T0.replace(hour=0, minute=0),
        quantity=Decimal("40"),
        market_value=None,
    )
    groups = group_fills([buy], [midnight])
    assert len(groups) == 1
    assert groups[0].status is TradeStatus.CLOSED
