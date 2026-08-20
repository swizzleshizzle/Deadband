from datetime import UTC, datetime
from decimal import Decimal, getcontext
from uuid import UUID, uuid4

import pytest

from ledger.corporate import ActionType, CorporateAction, adjust_fills, adjust_transfers
from ledger.types import AssetTransfer, Fill, FillSource, Side

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
OLD = UUID("00000000-0000-0000-0000-0000000000b1")
NEW = UUID("00000000-0000-0000-0000-0000000000b2")


def fill(
    qty,
    price,
    day,
    instrument=OLD,
    side=Side.BUY,
    fill_id=None,
    venue_fill_id=None,
    content_hash=None,
) -> Fill:
    return Fill(
        id=fill_id or uuid4(),
        account_id=ACC,
        instrument_id=instrument,
        executed_at=datetime(2026, 6, day, 15, 0, tzinfo=UTC),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.CSV,
        venue_fill_id=venue_fill_id,
        is_estimated=False,
        content_hash=content_hash,
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


def test_a_corporate_action_may_not_produce_its_own_instrument():
    """resulting == instrument is nonsense: the action produces the thing it
    consumes. It terminates safely today, but by coincidence of adjust_fills'
    current shape rather than by design -- a self-referential spinoff would
    allocate basis from an instrument to itself.

    resulting_instrument_id is built as UUID(str(OLD)) rather than reused as
    the same OLD object: a value-equal but distinct UUID is the realistic
    shape (an id round-tripped through the database, JSON, or a CLI argument
    is never the same object as the one in memory), so the guard must catch
    it by value, not by identity -- an `is` comparison here would be a live
    bug, not a theoretical one.

    SPLIT is included precisely because it does NOT require a resulting id:
    the guard lives outside the MERGER/SPINOFF/SYMBOL_CHANGE conditional on
    purpose, since a self-referential id is meaningless on any action type
    that carries one at all. Without this case, moving the check inside that
    conditional -- a plausible "tidying" refactor -- would silently narrow
    its scope and this test would not notice."""
    for action_type, extra in (
        (ActionType.MERGER, {}),
        (ActionType.SPINOFF, {"basis_allocation": Decimal("0.2")}),
        (ActionType.SYMBOL_CHANGE, {}),
        (ActionType.SPLIT, {}),
    ):
        with pytest.raises(ValueError, match="itself|self"):
            CorporateAction(
                instrument_id=OLD,
                action_type=action_type,
                ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
                ratio_numerator=Decimal("1"),
                ratio_denominator=Decimal("1"),
                resulting_instrument_id=UUID(str(OLD)),
                **extra,
            )


def test_a_corporate_action_producing_a_different_instrument_is_accepted():
    """The negative control: the guard must not reject legitimate actions."""
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("1"),
        resulting_instrument_id=NEW,
    )
    assert action.resulting_instrument_id == NEW


def test_precision_pinning_produces_exact_value():
    """I5: At ctx.prec=50, 100/3 must equal '33.' + 48 threes exactly.
    Without precision pinning, this value depends on ambient precision."""
    before = fill("30", "100", 1)
    action = split(15, 3, 1)

    adjusted = adjust_fills([before], [action])
    price_str = str(adjusted[0].price)

    # Assert the exact value: 33.333...333 (48 threes after decimal)
    expected = "33." + "3" * 48
    assert price_str == expected, f"Expected price {expected}, got {price_str}"


def test_precision_pinning_produces_identical_results_across_ambient_precisions():
    """Precision pinning ensures identical output regardless of ambient precision.
    Compares full result sets using str() across three different ambient precisions."""
    before = fill("30", "100", 1)
    action = split(15, 3, 1)

    ambient_prec = getcontext().prec
    try:
        results = {}
        for test_prec in [3, 10, 28]:
            getcontext().prec = test_prec
            adjusted = adjust_fills([before], [action])
            results[test_prec] = [(str(f.quantity), str(f.price)) for f in adjusted]

        # All precisions should produce identical string representations
        result_set = {str(v) for v in results.values()}
        assert len(result_set) == 1, f"Precision pinning failed: {results}"
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


# ============================================================================
# C1: Exact integer results (reverse splits) — scaling per fill, not pre-rounding
# ============================================================================


def test_reverse_split_exact_integer_result():
    """C1: 1-for-3 reverse split of 300@10 must give exactly 100@30, not dust."""
    before = fill("300", "10", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.REVERSE_SPLIT,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(3),
    )
    adjusted = adjust_fills([before], [action])
    # Exact match with no phantom dust
    assert adjusted[0].quantity == Decimal("100")
    assert adjusted[0].price == Decimal("30")


def test_reverse_split_exact_preserves_notional_value():
    """C1: 1-for-3 reverse split preserves notional with no phantom dust
    when rounded input feeds into grouping."""
    from ledger.grouping import group_fills

    # Create a pre-split position
    fid = uuid4()
    buy_300 = fill("300", "10", 1, fill_id=fid)

    # Apply reverse split
    split_action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.REVERSE_SPLIT,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(3),
    )
    adjusted = adjust_fills([buy_300], [split_action])

    # Flatten: buy 100 @ 30 post-split
    assert len(adjusted) == 1
    assert adjusted[0].quantity == Decimal("100")
    assert adjusted[0].price == Decimal("30")

    # Now sell 100 post-split (after ex_date)
    sell_100 = fill("100", "30", 20, side=Side.SELL, fill_id=uuid4())

    # Group these two fills — should yield exactly one closed trade, no phantom short
    groups = group_fills([adjusted[0], sell_100])
    assert len(groups) == 1, f"Expected 1 closed trade, got {len(groups)}"
    assert groups[0].status.value == "closed"


# ============================================================================
# I1: Ex-date boundary — exactly at ex_date and one microsecond before
# ============================================================================


def test_fill_at_exactly_ex_date_is_untouched():
    """I1: Fill executed at exactly ex_date 00:00:00 UTC is UNTOUCHED (>= check)."""
    f = Fill(
        id=uuid4(),
        account_id=ACC,
        instrument_id=OLD,
        executed_at=datetime(2026, 6, 15, 0, 0, 0, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("100"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.CSV,
        venue_fill_id=None,
        is_estimated=False,
    )
    action = split(15, 2, 1)
    adjusted = adjust_fills([f], [action])
    # Should be untouched
    assert adjusted[0].quantity == Decimal("10")
    assert adjusted[0].price == Decimal("100")


def test_fill_on_day_before_ex_date_is_adjusted():
    """I1: Fill executed on the day before ex_date IS adjusted (< check).
    (Comparison is at day granularity, so any time on the day before is adjusted.)"""
    f = Fill(
        id=uuid4(),
        account_id=ACC,
        instrument_id=OLD,
        executed_at=datetime(2026, 6, 14, 23, 59, 59, 999999, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("100"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.CSV,
        venue_fill_id=None,
        is_estimated=False,
    )
    action = split(15, 2, 1)
    adjusted = adjust_fills([f], [action])
    # Should be adjusted
    assert adjusted[0].quantity == Decimal("20")
    assert adjusted[0].price == Decimal("50")


# ============================================================================
# I2: Action ordering — same-day split and merger, order independence
# ============================================================================


def test_same_ex_date_split_before_merger():
    """I2: Split must happen before merger on same ex_date, so split is not lost."""
    before = fill("10", "100", 1)

    # Apply split then merger on same day
    split_action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPLIT,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(2),
        ratio_denominator=Decimal(1),
    )
    merger_action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(2),
        resulting_instrument_id=NEW,
    )

    # Apply both in same call
    adjusted = adjust_fills([before], [split_action, merger_action])

    # Split: 10 qty * 2 = 20 qty, price / 2 = 50
    # Merger: 20 qty * 1/2 = 10 qty, price * 2 = 100
    parent_fills = [f for f in adjusted if f.instrument_id == NEW]
    assert len(parent_fills) == 1
    assert parent_fills[0].quantity == Decimal("10")
    assert parent_fills[0].price == Decimal("100")


def test_action_order_independence():
    """I2: Same actions in different order should produce identical output."""
    before = fill("10", "100", 1)

    split_action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPLIT,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(2),
        ratio_denominator=Decimal(1),
    )
    merger_action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(2),
        resulting_instrument_id=NEW,
    )

    # Forward order
    adjusted1 = adjust_fills([before], [split_action, merger_action])
    # Reverse order
    adjusted2 = adjust_fills([before], [merger_action, split_action])

    # Same results
    assert adjusted1 == adjusted2


# ============================================================================
# I3: Guard against id=None fills
# ============================================================================


def test_adjust_fills_rejects_fills_with_none_id():
    """I3: adjust_fills must reject fills with id=None to prevent spinoff id
    collision."""
    f = Fill(
        id=None,
        account_id=ACC,
        instrument_id=OLD,
        executed_at=datetime(2026, 6, 1, 15, 0, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("10"),
        price=Decimal("100"),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.CSV,
        venue_fill_id=None,
        is_estimated=False,
    )
    action = split(15, 2, 1)

    with pytest.raises(ValueError, match="requires persisted fills"):
        adjust_fills([f], [action])


# ============================================================================
# I4: Spinoff only applies to BUY fills
# ============================================================================


def test_spinoff_does_not_apply_to_sell_fills():
    """I4: Spinoff basis allocation should not apply to SELL fills."""
    sell_fill = fill("10", "120", 1, side=Side.SELL)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0.20"),
    )

    adjusted = adjust_fills([sell_fill], [action])

    # SELL fill is untouched, no spinoff created
    assert len(adjusted) == 1
    assert adjusted[0] == sell_fill


def test_spinoff_on_fully_closed_position_still_reduces_buy_basis():
    """I4: A fully-closed pre-ex_date position still has BUY basis reduced.
    This is a known limitation — we cannot tell if it was held at ex_date."""
    fid_buy = uuid4()
    fid_sell = uuid4()
    buy_fill = fill("10", "100", 1, fill_id=fid_buy)
    sell_fill = fill("10", "120", 5, side=Side.SELL, fill_id=fid_sell)

    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0.20"),
    )

    adjusted = adjust_fills([buy_fill, sell_fill], [action])

    # Buy is reduced (even though it was fully closed before ex_date)
    buy = [f for f in adjusted if f.id == fid_buy][0]
    assert buy.price == Decimal("80")  # 100 * (1 - 0.20)

    # Sell is untouched
    sell = [f for f in adjusted if f.id == fid_sell][0]
    assert sell.price == Decimal("120")

    # Spinoff created from BUY only
    spun = [f for f in adjusted if f.instrument_id == NEW]
    assert len(spun) == 1


# ============================================================================
# I6: basis_allocation validation
# ============================================================================


def test_spinoff_requires_basis_allocation():
    """I6: Spinoff must have basis_allocation specified."""
    with pytest.raises(ValueError, match="spinoff requires basis_allocation"):
        CorporateAction(
            instrument_id=OLD,
            action_type=ActionType.SPINOFF,
            ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
            ratio_numerator=Decimal("1"),
            ratio_denominator=Decimal("5"),
            resulting_instrument_id=NEW,
            basis_allocation=None,
        )


def test_basis_allocation_out_of_range_rejected():
    """I6: basis_allocation must be between 0 and 1."""
    with pytest.raises(ValueError, match="basis_allocation must be between 0 and 1"):
        CorporateAction(
            instrument_id=OLD,
            action_type=ActionType.SPINOFF,
            ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
            ratio_numerator=Decimal("1"),
            ratio_denominator=Decimal("5"),
            resulting_instrument_id=NEW,
            basis_allocation=Decimal("1.5"),
        )


def test_basis_allocation_zero_allowed():
    """I6: basis_allocation=0 should be allowed (no spinoff value)."""
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0"),
    )
    # Should not raise
    assert action.basis_allocation == Decimal("0")


def test_basis_allocation_one_allowed():
    """I6: basis_allocation=1 should be allowed (all basis to spinoff)."""
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("1"),
    )
    # Should not raise
    assert action.basis_allocation == Decimal("1")


# ============================================================================
# M5: Symbol-change ignores non-unit ratio
# ============================================================================


def test_symbol_change_ignores_non_unit_ratio():
    """M5: Symbol-change should ignore the ratio, apply it only to instrument."""
    before = fill("10", "50", 1)
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SYMBOL_CHANGE,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(2),
        ratio_denominator=Decimal(1),
        resulting_instrument_id=NEW,
    )
    adjusted = adjust_fills([before], [action])
    # Ratio is ignored; qty and price unchanged
    assert adjusted[0].instrument_id == NEW
    assert adjusted[0].quantity == Decimal("10")
    assert adjusted[0].price == Decimal("50")


# ============================================================================
# M1: Clear dedupe keys (venue_fill_id, content_hash) on adjusted fills
# ============================================================================


def test_split_clears_dedupe_keys():
    """M1: Split must clear venue_fill_id and content_hash since qty/price changed."""
    before = fill(
        "10",
        "500",
        1,
        venue_fill_id="V-123",
        content_hash="deadbeef",
    )
    adjusted = adjust_fills([before], [split(15, 4, 1)])
    assert adjusted[0].venue_fill_id is None
    assert adjusted[0].content_hash is None


def test_merger_clears_dedupe_keys():
    """M1: Merger must clear dedupe keys (qty/price/instrument changed)."""
    before = fill(
        "10",
        "50",
        1,
        venue_fill_id="V-123",
        content_hash="deadbeef",
    )
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(2),
        resulting_instrument_id=NEW,
    )
    adjusted = adjust_fills([before], [action])
    assert adjusted[0].venue_fill_id is None
    assert adjusted[0].content_hash is None


def test_symbol_change_clears_dedupe_keys():
    """M1: Symbol-change must clear dedupe keys (instrument changed)."""
    before = fill(
        "10",
        "50",
        1,
        venue_fill_id="V-123",
        content_hash="deadbeef",
    )
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SYMBOL_CHANGE,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(1),
        resulting_instrument_id=NEW,
    )
    adjusted = adjust_fills([before], [action])
    assert adjusted[0].venue_fill_id is None
    assert adjusted[0].content_hash is None


def test_spinoff_parent_clears_dedupe_keys():
    """M1: Spinoff parent must clear dedupe keys (price changed)."""
    before = fill(
        "10",
        "100",
        1,
        venue_fill_id="V-123",
        content_hash="deadbeef",
    )
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0.20"),
    )
    adjusted = adjust_fills([before], [action])
    parent = [f for f in adjusted if f.instrument_id == OLD][0]
    assert parent.venue_fill_id is None
    assert parent.content_hash is None


def test_spinoff_child_clears_dedupe_keys():
    """The twin of test_spinoff_parent_clears_dedupe_keys, and the more
    dangerous half. A child carrying the parent's venue_fill_id violates
    fill_venue_id_uniq on insert -- loud. A child carrying the parent's
    content_hash dedupes AGAINST the parent and vanishes, reported as a
    successful skip -- silent, and the position simply never appears."""
    before = fill("10", "100", 1, venue_fill_id="V-123", content_hash="deadbeef")
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal("1"),
        ratio_denominator=Decimal("5"),
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0.20"),
    )
    child = [f for f in adjust_fills([before], [action]) if f.instrument_id == NEW][0]
    assert child.venue_fill_id is None
    assert child.content_hash is None
    # And it is a real fill, not an empty shell -- otherwise the assertions
    # above would pass on something that carries no position either.
    assert child.quantity > 0


# ============================================================================
# Remap chain dependency: actions targeting produced instruments
# ============================================================================


def test_merger_then_split_on_produced_instrument():
    """NEW: Merger OLD→MID then split on MID must apply both, not drop split.
    MID is the result of the merger, so split depends on merger completing."""
    MID = UUID("00000000-0000-0000-0000-0000000000c1")

    before = fill("10", "100", 1)

    # Merger: OLD→MID at 1:1
    merger = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(1),
        resulting_instrument_id=MID,
    )

    # Split on MID: 2:1
    split_mid = CorporateAction(
        instrument_id=MID,
        action_type=ActionType.SPLIT,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(2),
        ratio_denominator=Decimal(1),
    )

    adjusted = adjust_fills([before], [merger, split_mid])

    # After merger: MID, 10 qty @ 100
    # After split: MID, 20 qty @ 50
    mid_fills = [f for f in adjusted if f.instrument_id == MID]
    assert len(mid_fills) == 1
    assert mid_fills[0].quantity == Decimal("20")
    assert mid_fills[0].price == Decimal("50")


def test_circular_remap_dependency_raises():
    """NEW: A→B merger and B→A merger on same day is circular and must raise."""
    B = UUID("00000000-0000-0000-0000-0000000000c1")

    before = fill("10", "100", 1)

    # Merger: OLD→B
    merger1 = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(1),
        resulting_instrument_id=B,
    )

    # Merger: B→OLD (circular!)
    merger2 = CorporateAction(
        instrument_id=B,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 15, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(1),
        resulting_instrument_id=OLD,
    )

    with pytest.raises(ValueError, match="circular"):
        adjust_fills([before], [merger1, merger2])


# --- adjust_transfers (branch B): transfers follow the same rescale/remap the
# --- fills they close out receive (spec D7), or held quantities stop
# --- reconciling. Spinoffs are deliberately skipped -- a transfer is an
# --- outflow, not a holding; minting child transfers would fabricate outflows
# --- of shares never received (recorded as a gap).


def _xfer(instrument=OLD, *, qty="1800", day=1):
    return AssetTransfer(
        id=uuid4(),
        account_id=ACC,
        instrument_id=instrument,
        occurred_at=datetime(2026, 6, day, 15, 0, tzinfo=UTC),
        quantity=Decimal(qty),
        market_value=None,
        content_hash="raw-hash",
    )


def _rsplit(ex_day=10, num="1", den="6", instrument=OLD):
    return CorporateAction(
        instrument_id=instrument,
        action_type=ActionType.REVERSE_SPLIT,
        ex_date=datetime(2026, 6, ex_day, tzinfo=UTC).date(),
        ratio_numerator=Decimal(num),
        ratio_denominator=Decimal(den),
    )


def test_pre_ex_transfer_rescales_like_a_pre_ex_fill():
    out = adjust_transfers([_xfer()], [_rsplit()])
    assert out[0].quantity == Decimal(300)
    assert out[0].content_hash is None  # adjusted copies shed raw-row identity


def test_post_ex_transfer_is_untouched():
    out = adjust_transfers([_xfer(day=15)], [_rsplit()])
    assert out[0].quantity == Decimal(1800)
    assert out[0].content_hash == "raw-hash"


def test_symbol_change_remaps_a_pre_ex_transfer_instrument():
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SYMBOL_CHANGE,
        ex_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(1),
        resulting_instrument_id=NEW,
    )
    out = adjust_transfers([_xfer()], [action])
    assert out[0].instrument_id == NEW
    assert out[0].quantity == Decimal(1800)


def test_merger_remaps_and_rescales_a_pre_ex_transfer():
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.MERGER,
        ex_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(6),
        resulting_instrument_id=NEW,
    )
    out = adjust_transfers([_xfer()], [action])
    assert out[0].instrument_id == NEW
    assert out[0].quantity == Decimal(300)


def test_spinoff_leaves_transfers_untouched_and_mints_nothing():
    action = CorporateAction(
        instrument_id=OLD,
        action_type=ActionType.SPINOFF,
        ex_date=datetime(2026, 6, 10, tzinfo=UTC).date(),
        ratio_numerator=Decimal(1),
        ratio_denominator=Decimal(10),
        resulting_instrument_id=NEW,
        basis_allocation=Decimal("0.375"),
    )
    out = adjust_transfers([_xfer()], [action])
    assert len(out) == 1
    assert out[0].quantity == Decimal(1800)
    assert out[0].instrument_id == OLD
