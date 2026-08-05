from datetime import UTC, datetime
from decimal import Decimal, getcontext
from uuid import UUID, uuid4

import pytest

from ledger.corporate import ActionType, CorporateAction, adjust_fills
from ledger.types import Fill, FillSource, Side

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
OLD = UUID("00000000-0000-0000-0000-0000000000b1")
NEW = UUID("00000000-0000-0000-0000-0000000000b2")


def fill(qty, price, day, instrument=OLD) -> Fill:
    return Fill(
        id=uuid4(),
        account_id=ACC,
        instrument_id=instrument,
        executed_at=datetime(2026, 6, day, 15, 0, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.CSV,
        venue_fill_id=None,
        is_estimated=False,
    )


def split(day, num, den, instrument=OLD) -> CorporateAction:
    return CorporateAction(
        instrument_id=instrument,
        action_type=ActionType.SPLIT,
        ex_date=datetime(2026, 6, day, tzinfo=UTC).date(),
        ratio_numerator=Decimal(num),
        ratio_denominator=Decimal(den),
    )


def test_forward_split_multiplies_quantity_and_divides_price():
    before = fill("10", "500", 1)
    adjusted = adjust_fills([before], [split(15, 4, 1)])
    assert adjusted[0].quantity == Decimal("40")
    assert adjusted[0].price == Decimal("125")


def test_split_leaves_notional_value_unchanged():
    before = fill("10", "500", 1)
    adjusted = adjust_fills([before], [split(15, 4, 1)])
    assert adjusted[0].quantity * adjusted[0].price == before.quantity * before.price


def test_fills_after_ex_date_are_untouched():
    after = fill("10", "125", 20)
    adjusted = adjust_fills([after], [split(15, 4, 1)])
    assert adjusted[0].quantity == Decimal("10")
    assert adjusted[0].price == Decimal("125")


def test_reverse_split_divides_quantity_and_multiplies_price():
    before = fill("100", "2", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.REVERSE_SPLIT,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(10),
    )
    adjusted = adjust_fills([before], [action])
    assert adjusted[0].quantity == Decimal("10")
    assert adjusted[0].price == Decimal("20")


def test_two_sequential_splits_compound():
    before = fill("10", "400", 1)
    adjusted = adjust_fills([before], [split(10, 2, 1), split(20, 2, 1)])
    assert adjusted[0].quantity == Decimal("40")
    assert adjusted[0].price == Decimal("100")


def test_symbol_change_remaps_instrument_without_touching_quantity():
    before = fill("10", "50", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SYMBOL_CHANGE,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(1),
        resulting_instrument_id=NEW,
    )
    adjusted = adjust_fills([before], [action])
    assert adjusted[0].instrument_id == NEW
    assert adjusted[0].quantity == Decimal("10")
    assert adjusted[0].price == Decimal("50")


def test_merger_remaps_and_applies_exchange_ratio():
    """0.5 shares of NEW per share of OLD."""
    before = fill("10", "50", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(2),
        resulting_instrument_id=NEW,
    )
    adjusted = adjust_fills([before], [action])
    assert adjusted[0].instrument_id == NEW
    assert adjusted[0].quantity == Decimal("5")
    assert adjusted[0].price == Decimal("100")


def test_spinoff_allocates_cost_basis_and_adds_a_position():
    """20% of basis moves to the spun-off instrument."""
    before = fill("10", "100", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),  # 1 new share per 5 held
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0.20"),
    )
    adjusted = adjust_fills([before], [action])
    parent = [f for f in adjusted if f.instrument_id == OLD][0]
    spun = [f for f in adjusted if f.instrument_id == NEW][0]
    assert parent.quantity == Decimal("10")
    assert parent.price == Decimal("80")  # 80% of basis retained
    assert spun.quantity == Decimal("2")  # 10 / 5
    assert spun.price == Decimal("100")  # 20% of 1000 over 2 shares
    assert spun.is_estimated is True


def test_actions_never_mutate_the_input():
    before = fill("10", "500", 1)
    adjust_fills([before], [split(15, 4, 1)])
    assert before.quantity == Decimal("10")
    assert before.price == Decimal("500")


def test_unrelated_instrument_is_untouched():
    other = fill("10", "500", 1, instrument=NEW)
    adjusted = adjust_fills([other], [split(15, 4, 1)])
    assert adjusted[0].quantity == Decimal("10")


def test_zero_ratio_is_rejected():
    with pytest.raises(ValueError, match="ratio"):
        CorporateAction(
            instrument_id=OLD,
            action_type=ActionType.SPLIT,
            ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
            ratio_numerator=Decimal(0),
            ratio_denominator=Decimal(1),
        )


def test_precision_pinning_with_non_terminating_ratio():
    """3:1 split on price 100 produces non-terminating decimal 33.333...,
    demonstrating precision pinning is essential. Compare str() not == since
    Decimal("30") and Decimal("30.00") compare equal but str() differs."""
    # Test with a non-terminating ratio: 3:1 split, price 100 -> 100/3
    before = fill("30", "100", 1)
    action = split(15, 3, 1)

    # Save and restore ambient precision
    ambient_prec = getcontext().prec
    try:
        # Test under different ambient precisions
        for test_prec in [3, 10, 28]:
            getcontext().prec = test_prec
            adjusted = adjust_fills([before], [action])
            result_qty_str = str(adjusted[0].quantity)
            result_price_str = str(adjusted[0].price)
            # All should be identical regardless of ambient precision
            assert result_qty_str == "90", (
                f"Expected quantity 90, got {result_qty_str} at prec={test_prec}"
            )
            assert result_price_str.startswith("33.33"), (
                f"Expected price ~33.33..., got {result_price_str} at prec={test_prec}"
            )
    finally:
        getcontext().prec = ambient_prec


def test_spinoff_fill_ids_are_deterministic():
    """Repeated calls to adjust_fills with spinoff produce identical ids."""
    before = fill("10", "100", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0.20"),
    )

    # Adjust twice with identical input
    adjusted1 = adjust_fills([before], [action])
    adjusted2 = adjust_fills([before], [action])

    # Extract spinoff fills
    spun1 = [f for f in adjusted1 if f.instrument_id == NEW][0]
    spun2 = [f for f in adjusted2 if f.instrument_id == NEW][0]

    # ids should be identical
    assert spun1.id == spun2.id, f"Spinoff ids should be deterministic: {spun1.id} != {spun2.id}"
    # Verify full equality including all fields
    assert spun1 == spun2, "Repeated adjustment should produce identical fills"
