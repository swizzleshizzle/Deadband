from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext
from uuid import UUID, uuid4

import pytest

from ledger.grouping import group_fills
from ledger.pnl import compute_pnl, r_multiple, unrealized_pnl
from ledger.types import Direction, Fill, FillSource, Side

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
INST = UUID("00000000-0000-0000-0000-0000000000b1")
T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
ONE = {INST: Decimal(1)}


def fill(side, qty, price, minutes=0, fee="0", instrument=INST) -> Fill:
    return Fill(
        id=uuid4(),
        account_id=ACC,
        instrument_id=instrument,
        executed_at=T0 + timedelta(minutes=minutes),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=None,
        is_estimated=False,
    )


def pnl_for(fills, multipliers=None):
    groups = group_fills(fills)
    assert len(groups) == 1
    g = groups[0]
    return compute_pnl(g.allocations, {f.id: f for f in fills}, multipliers or ONE, g.direction)


def test_simple_long_profit():
    result = pnl_for([fill(Side.BUY, "1", "100", 0), fill(Side.SELL, "1", "120", 10)])
    assert result.gross_realized_pnl == Decimal("20")
    assert result.avg_entry == Decimal("100")
    assert result.avg_exit == Decimal("120")
    assert result.qty_opened == Decimal("1")
    assert result.qty_closed == Decimal("1")


def test_simple_short_profit():
    result = pnl_for([fill(Side.SELL, "1", "120", 0), fill(Side.BUY, "1", "100", 10)])
    assert result.gross_realized_pnl == Decimal("20")
    assert result.avg_entry == Decimal("120")
    assert result.avg_exit == Decimal("100")


def test_fees_reduce_net_but_not_gross():
    result = pnl_for(
        [fill(Side.BUY, "1", "100", 0, fee="1.50"), fill(Side.SELL, "1", "120", 10, fee="1.50")]
    )
    assert result.gross_realized_pnl == Decimal("20")
    assert result.fees_total == Decimal("3.00")
    assert result.realized_pnl == Decimal("17.00")


def test_scale_in_uses_average_cost():
    """Buy 1@100 and 1@200, sell 1@200 => avg cost 150, realized 50."""
    result = pnl_for(
        [
            fill(Side.BUY, "1", "100", 0),
            fill(Side.BUY, "1", "200", 10),
            fill(Side.SELL, "1", "200", 20),
        ]
    )
    assert result.avg_entry == Decimal("150")
    assert result.gross_realized_pnl == Decimal("50")
    assert result.open_quantity == Decimal("1")
    assert result.open_cost_basis == Decimal("150")


def test_partial_exit_leaves_open_position():
    result = pnl_for(
        [
            fill(Side.BUY, "2", "100", 0),
            fill(Side.SELL, "1", "130", 10),
        ]
    )
    assert result.gross_realized_pnl == Decimal("30")
    assert result.open_quantity == Decimal("1")
    assert result.open_cost_basis == Decimal("100")
    assert result.qty_closed == Decimal("1")


def test_option_multiplier_scales_pnl():
    """One contract, $1.00 to $2.50, multiplier 100 => $150."""
    result = pnl_for(
        [fill(Side.BUY, "1", "1.00", 0), fill(Side.SELL, "1", "2.50", 10)],
        multipliers={INST: Decimal("100")},
    )
    assert result.gross_realized_pnl == Decimal("150.00")
    assert result.avg_entry == Decimal("1.00")


def test_fee_is_prorated_when_a_fill_is_split_across_trades():
    """A crossing fill's fee must be split by quantity, not double-counted."""
    crossing = fill(Side.SELL, "3", "110", 10, fee="3.00")
    fills = [fill(Side.BUY, "2", "100", 0, fee="2.00"), crossing]
    groups = group_fills(fills)
    by_id = {f.id: f for f in fills}
    closed = compute_pnl(groups[0].allocations, by_id, ONE, groups[0].direction)
    opened = compute_pnl(groups[1].allocations, by_id, ONE, groups[1].direction)
    # crossing fee 3.00 over 3 units => 2.00 to the closed trade, 1.00 to the new one
    assert closed.fees_total == Decimal("4.00")
    assert opened.fees_total == Decimal("1.00")
    assert closed.fees_total + opened.fees_total == Decimal("5.00")


def test_unrealized_long():
    assert unrealized_pnl(
        open_quantity=Decimal("2"),
        open_cost_basis=Decimal("100"),
        mark_price=Decimal("110"),
        multiplier=Decimal("1"),
        direction=Direction.LONG,
    ) == Decimal("20")


def test_unrealized_short():
    assert unrealized_pnl(
        open_quantity=Decimal("2"),
        open_cost_basis=Decimal("100"),
        mark_price=Decimal("90"),
        multiplier=Decimal("1"),
        direction=Direction.SHORT,
    ) == Decimal("20")


def test_r_multiple():
    assert r_multiple(Decimal("210"), Decimal("100")) == Decimal("2.1")


def test_r_multiple_is_none_without_planned_risk():
    assert r_multiple(Decimal("210"), None) is None
    assert r_multiple(Decimal("210"), Decimal("0")) is None


def test_compute_pnl_is_independent_of_ambient_decimal_precision():
    """Compute P&L under different context precisions and verify identical results.

    The compute_pnl function wraps its logic in a localcontext with prec=50,
    so the ambient precision should not affect the results. Uses a fixture
    with non-terminating average cost (1@100, 2@200, 3@250 => avg 166.666...)
    to ensure precision differences would be detected.
    """
    # BUY 1@100, BUY 2@200 => avg cost 166.666...
    # SELL 3@250 => gross 249.999...
    # These do not terminate in base 10, so precision matters.
    test_fills = [
        fill(Side.BUY, "1", "100", 0),
        fill(Side.BUY, "2", "200", 10),
        fill(Side.SELL, "3", "250", 20),
    ]

    # Save the current precision to restore later.
    original_prec = getcontext().prec

    try:
        # Compute under prec=3
        getcontext().prec = 3
        result_prec3 = pnl_for(test_fills)

        # Compute under prec=10
        getcontext().prec = 10
        result_prec10 = pnl_for(test_fills)

        # Compute under prec=28
        getcontext().prec = 28
        result_prec28 = pnl_for(test_fills)

        # Compare string representations to catch differences in rendering,
        # not just numeric equality.
        assert (
            str(result_prec3.qty_opened)
            == str(result_prec10.qty_opened)
            == str(result_prec28.qty_opened)
        )
        assert (
            str(result_prec3.qty_closed)
            == str(result_prec10.qty_closed)
            == str(result_prec28.qty_closed)
        )
        assert (
            str(result_prec3.avg_entry)
            == str(result_prec10.avg_entry)
            == str(result_prec28.avg_entry)
        )
        assert (
            str(result_prec3.avg_exit) == str(result_prec10.avg_exit) == str(result_prec28.avg_exit)
        )
        assert (
            str(result_prec3.gross_realized_pnl)
            == str(result_prec10.gross_realized_pnl)
            == str(result_prec28.gross_realized_pnl)
        )
        assert (
            str(result_prec3.fees_total)
            == str(result_prec10.fees_total)
            == str(result_prec28.fees_total)
        )
        assert (
            str(result_prec3.realized_pnl)
            == str(result_prec10.realized_pnl)
            == str(result_prec28.realized_pnl)
        )
        assert (
            str(result_prec3.open_quantity)
            == str(result_prec10.open_quantity)
            == str(result_prec28.open_quantity)
        )
        assert (
            str(result_prec3.open_cost_basis)
            == str(result_prec10.open_cost_basis)
            == str(result_prec28.open_cost_basis)
        )

    finally:
        # Restore the original precision.
        getcontext().prec = original_prec


def test_exact_pnl_on_full_close():
    """Full close returns exact value: 100@100 + 100@200 + 100@300 sold at 250."""
    result = pnl_for(
        [
            fill(Side.BUY, "100", "100", 0),
            fill(Side.BUY, "100", "200", 10),
            fill(Side.BUY, "100", "300", 20),
            fill(Side.SELL, "300", "250", 30),
        ]
    )
    # avg entry = (10000 + 20000 + 30000) / 300 = 200
    # gross = (250 - 200) * 300 = 15000
    assert result.gross_realized_pnl == Decimal("15000")
    assert result.open_quantity == Decimal("0")


def test_realized_pnl_identity_with_non_terminating_average():
    """realized_pnl = gross_realized_pnl - fees_total must hold exactly."""
    result = pnl_for(
        [
            fill(Side.BUY, "1", "100", 0, fee="0.50"),
            fill(Side.BUY, "2", "200", 10, fee="1.00"),
            fill(Side.SELL, "3", "250", 20, fee="1.50"),
        ]
    )
    assert result.realized_pnl == result.gross_realized_pnl - result.fees_total


def test_avg_exit_is_none_on_open_only_trade():
    """A trade that never closes should have avg_exit = None."""
    result = pnl_for([fill(Side.BUY, "1", "100", 0), fill(Side.BUY, "1", "200", 10)])
    assert result.avg_exit is None


def test_open_cost_basis_with_multiple_open_quantity():
    """open_cost_basis should be per-unit, not total, when open_quantity > 1."""
    result = pnl_for(
        [
            fill(Side.BUY, "3", "100", 0),
            fill(Side.SELL, "1", "130", 10),
        ]
    )
    assert result.open_quantity == Decimal("2")
    assert result.open_cost_basis == Decimal("100")


def test_avg_entry_on_trade_opened_by_split_crossing_fill():
    """The short trade opened by a crossing fill should have the correct avg_entry."""
    crossing = fill(Side.SELL, "3", "110", 10)
    fills = [fill(Side.BUY, "2", "100", 0), crossing]
    groups = group_fills(fills)
    by_id = {f.id: f for f in fills}
    # groups[1] should be the new short trade opened by the crossing fill
    opened = compute_pnl(groups[1].allocations, by_id, ONE, groups[1].direction)
    assert opened.avg_entry == Decimal("110")


def test_unrealized_pnl_with_contract_multiplier():
    """unrealized_pnl must apply the multiplier."""
    result = unrealized_pnl(
        open_quantity=Decimal("1"),
        open_cost_basis=Decimal("1.00"),
        mark_price=Decimal("2.50"),
        multiplier=Decimal("100"),
        direction=Direction.LONG,
    )
    assert result == Decimal("150")


def test_missing_multiplier_raises_key_error():
    """compute_pnl should raise KeyError if a multiplier is not supplied."""
    inst2 = UUID("00000000-0000-0000-0000-0000000000c1")
    fills = [fill(Side.BUY, "1", "100", 0, instrument=inst2)]
    groups = group_fills(fills)
    by_id = {f.id: f for f in fills}

    with pytest.raises(KeyError, match="no contract multiplier supplied"):
        compute_pnl(groups[0].allocations, by_id, ONE, groups[0].direction)


def test_spread_direction_raises_not_implemented():
    """compute_pnl should reject SPREAD direction."""
    with pytest.raises(NotImplementedError, match="multi-leg SPREAD trades"):
        compute_pnl([], {}, ONE, Direction.SPREAD)


def test_unrealized_pnl_spread_direction_raises_not_implemented():
    """unrealized_pnl should reject SPREAD direction."""
    with pytest.raises(NotImplementedError, match="multi-leg SPREAD trades"):
        unrealized_pnl(
            open_quantity=Decimal("1"),
            open_cost_basis=Decimal("100"),
            mark_price=Decimal("110"),
            multiplier=Decimal("1"),
            direction=Direction.SPREAD,
        )
