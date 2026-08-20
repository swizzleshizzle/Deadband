from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext
from uuid import UUID, uuid4

import pytest

from ledger.grouping import FillAllocation, TransferAllocation, group_fills
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


def _allocs(*pairs):
    """Build FillAllocation tuples from (fill, qty) pairs: _allocs(f1, "4", f2, "1")."""
    items = list(pairs)
    return [
        FillAllocation(items[i].id, Decimal(items[i + 1])) for i in range(0, len(items), 2)
    ]


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


def test_entry_fee_is_amortized_across_closes_not_expensed_at_once():
    """A trade 25% closed must recognise 25% of its entry fee, not all of it.

    The old convention expensed the whole entry fee immediately, which matches
    no accounting convention and self-corrects only when the trade closes flat.
    Asserts on realized_pnl, whose value the bug moves by 225.
    """
    entry = fill(side=Side.BUY, qty="4", price="60000", minutes=0, fee="300")
    exit_ = fill(side=Side.SELL, qty="1", price="76000", minutes=10, fee="85")
    result = compute_pnl(
        _allocs(entry, "4", exit_, "1"),
        {entry.id: entry, exit_.id: exit_},
        {entry.instrument_id: Decimal(1)},
        Direction.LONG,
    )

    assert result.qty_closed == Decimal("1")
    assert result.fees_total == Decimal("385")  # unchanged meaning
    assert result.fees_realized == Decimal("160")  # 85 exit + 300 * 1/4
    assert result.realized_pnl == result.gross_realized_pnl - result.fees_realized


def test_unamortized_entry_fee_is_carried_in_open_cost_basis():
    """The 225 of entry fee not yet recognised belongs to the 3 open units.

    Per-unit, and divided by the multiplier, because open_cost_basis is
    expressed in price terms while a fee is expressed in currency.
    """
    entry = fill(side=Side.BUY, qty="4", price="60000", minutes=0, fee="300")
    exit_ = fill(side=Side.SELL, qty="1", price="76000", minutes=10, fee="85")
    result = compute_pnl(
        _allocs(entry, "4", exit_, "1"),
        {entry.id: entry, exit_.id: exit_},
        {entry.instrument_id: Decimal(1)},
        Direction.LONG,
    )
    assert result.open_quantity == Decimal("3")
    assert result.open_cost_basis == Decimal("60075")  # 60000 + 300/4


def test_option_entry_fee_capitalizes_per_contract_not_per_share():
    """open_cost_basis excludes the multiplier, so a currency fee must be
    divided by (quantity * multiplier) to land in the same units as price.

    Without the multiplier this is 100x wrong for options -- the exact silent
    failure mode that CHECK (contract_multiplier > 0) exists to bound.
    """
    entry = fill(side=Side.BUY, qty="10", price="0.40", fee="6.60")
    result = compute_pnl(
        _allocs(entry, "10"),
        {entry.id: entry},
        {entry.instrument_id: Decimal(100)},
        Direction.LONG,
    )
    assert result.open_quantity == Decimal("10")
    # 0.40 + 6.60/(10*100) = 0.40 + 0.0066 = 0.4066
    assert result.open_cost_basis == Decimal("0.4066")


def test_short_entry_fee_reduces_open_cost_basis_not_increases():
    """A SHORT's open_cost_basis is average SALE proceeds per unit, so an
    entry (opening SELL) fee reduces net proceeds and must be SUBTRACTED --
    not added, as is correct for a LONG's average purchase cost.

    Adding it (the LONG rule applied blindly) is wrong in two ways at once:
    open_cost_basis comes out too high, and unrealized_pnl for a SHORT is
    (open_cost_basis - mark_price), so the sign error doubles into the
    reported unrealized P&L. fees_realized and realized_pnl do not involve
    this sign at all and must be unaffected -- only open_cost_basis moves.
    """
    entry = fill(side=Side.SELL, qty="10", price="0.40", minutes=0, fee="6.60")
    exit_ = fill(side=Side.BUY, qty="2", price="0.30", minutes=10, fee="0")
    result = compute_pnl(
        _allocs(entry, "10", exit_, "2"),
        {entry.id: entry, exit_.id: exit_},
        {entry.instrument_id: Decimal(100)},
        Direction.SHORT,
    )
    assert result.open_quantity == Decimal("8")
    # 0.40 - 6.60/(10*100) = 0.40 - 0.0066 = 0.3934
    assert result.open_cost_basis == Decimal("0.3934")
    assert result.fees_realized == Decimal("1.32")
    assert result.realized_pnl == Decimal("18.68")


@pytest.mark.parametrize(
    "direction,mult,exit_qty",
    [
        (Direction.LONG, Decimal(1), "1"),
        (Direction.LONG, Decimal(100), "1"),
        (Direction.LONG, Decimal(1), "4"),
        (Direction.SHORT, Decimal(1), "1"),
        (Direction.SHORT, Decimal(100), "1"),
        (Direction.SHORT, Decimal(1), "4"),
    ],
)
def test_realized_plus_unrealized_conserves_gross_minus_fees(direction, mult, exit_qty):
    """realized_pnl + unrealized_pnl(mark) must equal gross-at-mark minus fees_total.

    Nothing is created or destroyed by splitting a fee between the realized
    and still-open portions of a trade: whatever amortization does to
    open_cost_basis, the two halves must still sum to the same total economic
    result as if fees were never split at all. "gross-at-mark" is computed
    independently of open_cost_basis (from avg_entry, which is pure price with
    no fee folded in) so this test cannot be fooled by a self-consistent sign
    error in open_cost_basis alone -- it is the check that would have caught
    the SHORT sign inversion this file's earlier version had.

    Deterministic fixtures, parametrized over LONG/SHORT, a contract
    multiplier, and full vs. partial close.
    """
    if direction is Direction.LONG:
        entry = fill(side=Side.BUY, qty="4", price="60000", minutes=0, fee="300")
        exit_ = fill(side=Side.SELL, qty=exit_qty, price="76000", minutes=10, fee="85")
    else:
        entry = fill(side=Side.SELL, qty="4", price="60000", minutes=0, fee="300")
        exit_ = fill(side=Side.BUY, qty=exit_qty, price="44000", minutes=10, fee="85")

    result = compute_pnl(
        _allocs(entry, "4", exit_, exit_qty),
        {entry.id: entry, exit_.id: exit_},
        {entry.instrument_id: mult},
        direction,
    )

    mark = Decimal("50000")
    unreal = unrealized_pnl(result.open_quantity, result.open_cost_basis, mark, mult, direction)
    if direction is Direction.SHORT:
        unreal_gross = (result.avg_entry - mark) * result.open_quantity * mult
    else:
        unreal_gross = (mark - result.avg_entry) * result.open_quantity * mult
    gross_at_mark = result.gross_realized_pnl + unreal_gross

    assert result.realized_pnl + unreal == gross_at_mark - result.fees_total


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
            str(result_prec3.fees_realized)
            == str(result_prec10.fees_realized)
            == str(result_prec28.fees_realized)
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
    """realized_pnl = gross_realized_pnl - fees_realized must hold exactly.

    This trade closes fully (qty_closed == qty_opened), so every entry fee
    is recognised and fees_realized == fees_total here -- asserted explicitly
    so the full-close case where the two fee figures coincide stays covered.
    """
    result = pnl_for(
        [
            fill(Side.BUY, "1", "100", 0, fee="0.50"),
            fill(Side.BUY, "2", "200", 10, fee="1.00"),
            fill(Side.SELL, "3", "250", 20, fee="1.50"),
        ]
    )
    assert result.fees_realized == result.fees_total
    assert result.realized_pnl == result.gross_realized_pnl - result.fees_realized


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
    """The short trade opened by a crossing fill must be computed correctly.

    Mutant detection: asserts both qty_opened and gross_realized_pnl, not just
    avg_entry (which is a ratio and could survive a mutant that allocates the
    whole fill quantity instead of just the allocated portion).
    """
    crossing = fill(Side.SELL, "3", "110", 10)
    fills = [
        fill(Side.BUY, "2", "100", 0),
        crossing,
        fill(Side.BUY, "1", "105", 20),  # close the short
    ]
    groups = group_fills(fills)
    by_id = {f.id: f for f in fills}
    # groups[1] should be the new short trade opened by the crossing fill
    opened = compute_pnl(groups[1].allocations, by_id, ONE, groups[1].direction)
    # avg_entry=110 alone does not catch the mutant (it's a ratio), so also check qty
    assert opened.qty_opened == Decimal("1")
    assert opened.avg_entry == Decimal("110")
    # Close the short to verify gross P&L: (110 - 105) * 1 = 5
    assert opened.gross_realized_pnl == Decimal("5")


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


def test_allocations_sorted_chronologically():
    """compute_pnl must process allocations in chronological order.

    SQL queries may return rows in any order, so this test shuffles allocations
    from a multi-fill trade into non-chronological order and verifies that
    compute_pnl still produces results identical to the chronological case.
    The sort is the only guard against database result ordering.
    """
    original_fills = [
        fill(Side.BUY, "1", "100", 0),
        fill(Side.BUY, "1", "200", 10),
        fill(Side.SELL, "1", "250", 20),
    ]
    groups = group_fills(original_fills)
    by_id = {f.id: f for f in original_fills}

    # Compute with chronological order (as returned by group_fills)
    result_chrono = compute_pnl(groups[0].allocations, by_id, ONE, groups[0].direction)

    # Shuffle allocations into reverse chronological order
    shuffled_allocs = list(reversed(groups[0].allocations))

    # Compute with shuffled allocations
    result_shuffled = compute_pnl(shuffled_allocs, by_id, ONE, groups[0].direction)

    # Results must be identical
    assert result_chrono.qty_opened == result_shuffled.qty_opened
    assert result_chrono.qty_closed == result_shuffled.qty_closed
    assert result_chrono.avg_entry == result_shuffled.avg_entry
    assert result_chrono.avg_exit == result_shuffled.avg_exit
    assert result_chrono.gross_realized_pnl == result_shuffled.gross_realized_pnl
    assert result_chrono.fees_total == result_shuffled.fees_total
    assert result_chrono.fees_realized == result_shuffled.fees_realized
    assert result_chrono.realized_pnl == result_shuffled.realized_pnl
    assert result_chrono.open_quantity == result_shuffled.open_quantity
    assert result_chrono.open_cost_basis == result_shuffled.open_cost_basis


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


# --- transfers (branch B): the position closes at running average cost, so
# --- realised P&L is untouched by construction and the basis (and its share
# --- of entry fees) leaves with the shares.


def _talloc(qty, minutes=10):
    return TransferAllocation(
        transfer_id=uuid4(), quantity=Decimal(qty), occurred_at=T0 + timedelta(minutes=minutes)
    )


def _pnl_with_transfers(fills, transfers):
    allocs = [FillAllocation(f.id, f.quantity) for f in fills]
    by_id = {f.id: f for f in fills}
    return compute_pnl(allocs, by_id, ONE, Direction.LONG, transfers=transfers)


def test_full_transfer_realises_exactly_zero():
    f = fill(Side.BUY, "40", "6.17", 0)
    pnl = _pnl_with_transfers([f], [_talloc("40")])
    assert pnl.realized_pnl == 0
    assert pnl.gross_realized_pnl == 0
    assert pnl.qty_transferred == Decimal(40)
    assert pnl.qty_closed == 0
    assert pnl.avg_exit is None  # a transfer is not an exit
    assert pnl.open_quantity == 0
    assert pnl.open_cost_basis == 0


def test_partial_transfer_keeps_per_unit_basis():
    f = fill(Side.BUY, "40", "10", 0)
    pnl = _pnl_with_transfers([f], [_talloc("15")])
    assert pnl.open_quantity == Decimal(25)
    assert pnl.open_cost_basis == Decimal(10)  # per-unit; unchanged by the exit-free reduction
    assert pnl.realized_pnl == 0
    assert pnl.qty_transferred == Decimal(15)


def test_sell_after_partial_transfer_uses_surviving_basis():
    f_buy = fill(Side.BUY, "40", "10", 0)
    f_sell = fill(Side.SELL, "25", "12", 20)
    allocs = [FillAllocation(f_buy.id, f_buy.quantity), FillAllocation(f_sell.id, f_sell.quantity)]
    by_id = {f.id: f for f in (f_buy, f_sell)}
    pnl = compute_pnl(allocs, by_id, ONE, Direction.LONG, transfers=[_talloc("15", minutes=10)])
    assert pnl.realized_pnl == Decimal(50)  # 25 * (12 - 10), fees zero
    assert pnl.qty_closed == Decimal(25)
    assert pnl.avg_exit == Decimal(12)
    assert pnl.open_quantity == 0
    assert pnl.qty_transferred == Decimal(15)


def test_entry_fees_of_transferred_quantity_stay_unrecognised():
    f = fill(Side.BUY, "40", "10", 0, fee="3")
    pnl = _pnl_with_transfers([f], [_talloc("40")])
    assert pnl.fees_total == Decimal(3)
    assert pnl.fees_realized == 0
    assert pnl.realized_pnl == 0


def test_transfer_on_a_short_trade_raises_instead_of_draining_proceeds():
    """position is an unsigned magnitude, so the in-walk guard alone cannot
    see that 'average cost' is meaningless against a short's sale proceeds --
    the misuse must refuse at the door, as the guard's comment promises."""
    f = fill(Side.SELL, "40", "10", 0)
    allocs = [FillAllocation(f.id, f.quantity)]
    with pytest.raises(ValueError, match="short"):
        compute_pnl(allocs, {f.id: f}, ONE, Direction.SHORT, transfers=[_talloc("40")])
