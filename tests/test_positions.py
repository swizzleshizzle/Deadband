# tests/test_positions.py
from decimal import Decimal
from uuid import UUID

from ledger.positions import TradeRow, aggregate_positions
from ledger.types import Direction

I1 = UUID("11111111-1111-1111-1111-111111111111")
I2 = UUID("22222222-2222-2222-2222-222222222222")


def row(instrument_id=I1, symbol="ZXCO", multiplier="1", direction=Direction.LONG,
        qty="10", basis="20", estimated=False):
    return TradeRow(
        instrument_id=instrument_id,
        symbol=symbol,
        multiplier=Decimal(multiplier),
        direction=direction,
        open_quantity=None if qty is None else Decimal(qty),
        open_cost_basis=None if basis is None else Decimal(basis),
        is_estimated=estimated,
    )


def test_a_single_open_trade_becomes_one_position():
    (p,) = aggregate_positions([row(qty="10", basis="20")])
    assert p.instrument_id == I1
    assert p.quantity == Decimal("10")
    assert p.cost_basis == Decimal("20")
    assert p.direction is Direction.LONG
    assert p.unvaluable_reason is None
    assert p.trade_count == 1


def test_cost_basis_is_weighted_by_quantity_not_a_plain_average():
    """The defect this catches: averaging 20 and 50 to 35 ignores that the
    30-unit lot dominates the 10-unit one. Correct answer is 42.5; a plain
    mean gives 35, and both are plausible-looking numbers."""
    (p,) = aggregate_positions([row(qty="10", basis="20"), row(qty="30", basis="50")])
    assert p.quantity == Decimal("40")
    assert p.cost_basis == Decimal("42.5")
    assert p.trade_count == 2


def test_a_null_open_quantity_makes_the_position_unvaluable_rather_than_vanishing():
    """A protected/orphaned trade carries NULL open_quantity. SQL SUM skips
    NULLs, so the naive aggregate silently under-reports the position and
    nothing says so. The row must appear and name the problem."""
    (p,) = aggregate_positions([row(qty="10", basis="20"), row(qty=None, basis=None)])
    assert p.unvaluable_reason is not None
    assert "unknown" in p.unvaluable_reason
    assert p.trade_count == 2


def test_a_spread_contributor_makes_the_position_unvaluable():
    (p,) = aggregate_positions([row(direction=Direction.SPREAD)])
    assert p.unvaluable_reason == "spread"
    assert p.direction is None


def test_conflicting_directions_are_not_netted():
    """Long 10 and short 4 of one instrument is not 'long 6' -- netting is a
    modelling decision nobody has made. Refuse to imply one."""
    (p,) = aggregate_positions([
        row(qty="10", direction=Direction.LONG),
        row(qty="4", direction=Direction.SHORT),
    ])
    assert p.direction is None
    assert p.unvaluable_reason == "mixed direction"


def test_estimated_rolls_up_with_any_not_all():
    (p,) = aggregate_positions([row(estimated=False), row(estimated=True)])
    assert p.is_estimated is True


def test_positions_are_grouped_by_instrument_and_stably_ordered():
    ps = aggregate_positions([
        row(instrument_id=I2, symbol="ZZZZ"),
        row(instrument_id=I1, symbol="AAAA"),
        row(instrument_id=I1, symbol="AAAA"),
    ])
    assert [p.symbol for p in ps] == ["AAAA", "ZZZZ"]
    assert [p.trade_count for p in ps] == [2, 1]


def test_no_rows_is_no_positions_not_an_error():
    assert aggregate_positions([]) == ()
