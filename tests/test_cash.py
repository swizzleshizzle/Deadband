# tests/test_cash.py
from decimal import Decimal

import pytest

from ledger.cash import CashFillRow, CashMovementRow, net_cash
from ledger.types import Side


def mv(kind, amount):
    return CashMovementRow(kind=kind, amount=Decimal(amount))


def fl(side, qty, price, mult="1", fee="0"):
    return CashFillRow(
        side=side, quantity=Decimal(qty), price=Decimal(price),
        multiplier=Decimal(mult), fee=Decimal(fee),
    )


def test_no_rows_is_zero_cash():
    assert net_cash([], []) == Decimal(0)


@pytest.mark.parametrize(
    "kind,expected",
    [("deposit", "100"), ("dividend", "100"), ("interest", "100"), ("rebate", "100"),
     ("withdrawal", "-100"), ("fee", "-100"), ("tax", "-100")],
)
def test_each_movement_kind_carries_the_right_sign(kind, expected):
    """`amount` is always positive by convention (importers.base.OUTFLOW_KINDS);
    direction lives entirely in `kind`. A kind that subtracts when it should add
    is a 2x error in the wrong direction, not a rounding difference."""
    assert net_cash([mv(kind, "100")], []) == Decimal(expected)


def test_a_buy_spends_cash_and_a_sell_produces_it():
    assert net_cash([], [fl(Side.BUY, "10", "20")]) == Decimal("-200")
    assert net_cash([], [fl(Side.SELL, "10", "20")]) == Decimal("200")


def test_the_contract_multiplier_scales_a_fill_s_cash_effect():
    """Two option contracts at 3.50 with a x100 multiplier cost 700, not 7.
    Dropping the multiplier makes every option trade wrong by a hundredfold, and
    the resulting equity figure reads as a plausible drift rather than a bug."""
    assert net_cash([], [fl(Side.BUY, "2", "3.50", mult="100")]) == Decimal("-700")


def test_fees_reduce_proceeds_and_increase_cost():
    assert net_cash([], [fl(Side.BUY, "1", "100", fee="1.50")]) == Decimal("-101.50")
    assert net_cash([], [fl(Side.SELL, "1", "100", fee="1.50")]) == Decimal("98.50")


def test_a_drip_nets_to_its_residual_not_to_zero_and_not_to_double():
    """A dividend arrives as a CASH movement and the reinvestment spends it as a
    FILL with funding_source='reinvestment'. Both legs are recorded, so they
    cancel to the small residual that genuinely stayed in cash. Do NOT special-case
    reinvestment fills -- a special case here would double-count."""
    got = net_cash([mv("dividend", "11.30")], [fl(Side.BUY, "0.197", "57.25")])
    assert got == Decimal("11.30") - Decimal("0.197") * Decimal("57.25")
    assert got != Decimal(0)


def test_movements_and_fills_combine():
    got = net_cash(
        [mv("deposit", "1000"), mv("fee", "25")],
        [fl(Side.BUY, "10", "20"), fl(Side.SELL, "4", "30")],
    )
    assert got == Decimal("1000") - Decimal("25") - Decimal("200") + Decimal("120")
