from datetime import UTC, datetime
from decimal import Decimal, getcontext
from uuid import UUID

from ledger.reconcile import Position, Snapshot, reconcile

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
SPY = UUID("00000000-0000-0000-0000-0000000000b1")
AAPL = UUID("00000000-0000-0000-0000-0000000000b2")
AS_OF = datetime(2026, 7, 31, 20, 0, tzinfo=UTC)


def snapshot(cash, equity) -> Snapshot:
    return Snapshot(
        account_id=ACC, as_of=AS_OF, cash_balance=Decimal(cash), total_equity=Decimal(equity)
    )


def test_matching_account_reports_no_drift():
    positions = [Position(SPY, Decimal("10"), Decimal("500"), Decimal("1"))]
    drift = reconcile(
        snapshot=snapshot("1000", "6000"),
        positions=positions,
        marks={SPY: Decimal("500")},
        computed_cash=Decimal("1000"),
    )
    assert drift.computed_equity == Decimal("6000")
    assert drift.equity_difference == Decimal("0")
    assert drift.is_within_tolerance is True


def test_equity_drift_is_reported_with_sign():
    positions = [Position(SPY, Decimal("10"), Decimal("500"), Decimal("1"))]
    drift = reconcile(
        snapshot=snapshot("1000", "6312"),
        positions=positions,
        marks={SPY: Decimal("500")},
        computed_cash=Decimal("1000"),
    )
    assert drift.computed_equity == Decimal("6000")
    assert drift.reported_equity == Decimal("6312")
    assert drift.equity_difference == Decimal("-312")
    assert drift.is_within_tolerance is False


def test_cash_drift_is_reported_separately():
    drift = reconcile(
        snapshot=snapshot("900", "900"),
        positions=[],
        marks={},
        computed_cash=Decimal("1000"),
    )
    assert drift.cash_difference == Decimal("100")


def test_multiplier_is_applied_to_position_value():
    positions = [Position(SPY, Decimal("2"), Decimal("1.50"), Decimal("100"))]
    drift = reconcile(
        snapshot=snapshot("0", "500"),
        positions=positions,
        marks={SPY: Decimal("2.50")},
        computed_cash=Decimal("0"),
    )
    assert drift.computed_equity == Decimal("500")


def test_position_without_a_mark_falls_back_to_cost_basis():
    """An unmarked position must not silently value at zero."""
    positions = [Position(AAPL, Decimal("5"), Decimal("200"), Decimal("1"))]
    drift = reconcile(
        snapshot=snapshot("0", "1000"), positions=positions, marks={}, computed_cash=Decimal("0")
    )
    assert drift.computed_equity == Decimal("1000")
    assert drift.unmarked_instruments == (AAPL,)


def test_tolerance_is_configurable():
    drift = reconcile(
        snapshot=snapshot("1000", "1005"),
        positions=[],
        marks={},
        computed_cash=Decimal("1000"),
        tolerance=Decimal("10"),
    )
    assert drift.equity_difference == Decimal("-5")
    assert drift.is_within_tolerance is True


def test_both_differences_agree_in_sign():
    """Both equity and cash differences must share the same sign convention.

    A snapshot with both cash and equity below computed values should yield
    positive differences for both fields, confirming both use computed - reported.
    """
    positions = [Position(SPY, Decimal("10"), Decimal("500"), Decimal("1"))]
    drift = reconcile(
        snapshot=snapshot("900", "5500"),  # reported less than computed
        positions=positions,
        marks={SPY: Decimal("500")},
        computed_cash=Decimal("1000"),  # computed_cash > reported_cash (900)
    )
    # computed_equity = 1000 + (10*500*1) = 6000
    # equity_difference = 6000 - 5500 = 500 (positive: we computed more)
    # cash_difference = 1000 - 900 = 100 (positive: we computed more)
    assert drift.equity_difference == Decimal("500")
    assert drift.cash_difference == Decimal("100")
    assert drift.equity_difference > 0
    assert drift.cash_difference > 0


def test_tolerance_equality_is_within():
    """A difference exactly equal to tolerance must pass is_within_tolerance.

    Tests the boundary condition: abs(difference) <= tolerance when equal.
    """
    drift = reconcile(
        snapshot=snapshot("1000", "990"),  # reported_equity < computed_equity
        positions=[],
        marks={},
        computed_cash=Decimal("1000"),  # cash matches perfectly
        tolerance=Decimal("10"),
    )
    # equity_difference = 1000 - 990 = 10 (exactly equal to tolerance)
    # cash_difference = 1000 - 1000 = 0 (well within tolerance)
    assert drift.equity_difference == Decimal("10")
    assert drift.cash_difference == Decimal("0")
    assert abs(drift.equity_difference) == Decimal("10")
    assert drift.is_within_tolerance is True


def test_precision_pinning_produces_exact_value():
    """At ctx.prec=50, 100/3 yields a non-terminating decimal.

    Without precision pinning, this value depends on ambient precision.
    The reconcile function must pin ctx.prec=50 to produce the expected exact value.
    """
    # Create a position with a non-terminating valuation: qty=1, mark=100/3, multiplier=1
    # computed_equity = 0 + (1 * (100/3) * 1) = 33.333...333 (48 threes, 50 sig figs)
    positions = [Position(SPY, Decimal("1"), Decimal("1"), Decimal("1"))]

    # Compute the mark at prec=50 to ensure we have the full precision
    from decimal import localcontext

    with localcontext() as ctx:
        ctx.prec = 50
        mark = Decimal("100") / Decimal("3")

    drift = reconcile(
        snapshot=snapshot("0", "33.333333333333333333333333333333333333333333333333"),
        positions=positions,
        marks={SPY: mark},
        computed_cash=Decimal("0"),
    )

    # Assert the exact string value at prec=50
    expected_equity = "33.333333333333333333333333333333333333333333333333"
    assert str(drift.computed_equity) == expected_equity, (
        f"Expected computed_equity '{expected_equity}', got '{str(drift.computed_equity)}'"
    )


def test_precision_pinning_works_across_ambient_precisions():
    """Verify precision pinning makes results independent of ambient precision.

    This test compares full result sets across different ambient precisions.
    Without precision pinning inside reconcile(), results would differ.
    """
    positions = [Position(SPY, Decimal("1"), Decimal("1"), Decimal("1"))]

    # Compute mark at prec=50 for reference
    from decimal import localcontext

    with localcontext() as ctx:
        ctx.prec = 50
        mark = Decimal("100") / Decimal("3")
        expected_equity_str = "33.333333333333333333333333333333333333333333333333"

    ambient_prec = getcontext().prec
    try:
        results = {}
        for test_prec in [3, 10, 28]:
            getcontext().prec = test_prec
            drift = reconcile(
                snapshot=snapshot("0", expected_equity_str),
                positions=positions,
                marks={SPY: mark},
                computed_cash=Decimal("0"),
            )
            results[test_prec] = str(drift.computed_equity)

        # All ambient precisions should produce the same string representation
        # because reconcile() pins ctx.prec=50 internally
        result_set = set(results.values())
        assert len(result_set) == 1, f"Precision pinning failed: got different results {results}"
        assert results[3] == expected_equity_str
    finally:
        getcontext().prec = ambient_prec
