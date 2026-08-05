"""Property-based tests for fill grouping invariants."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from uuid import UUID, uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from ledger.grouping import group_fills
from ledger.types import Direction, Fill, FillSource, Side, TradeStatus

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
INST = UUID("00000000-0000-0000-0000-0000000000b1")
T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)

quantities = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("1000"), places=2, allow_nan=False
)
prices = st.decimals(
    min_value=Decimal("0.01"), max_value=Decimal("100000"), places=2, allow_nan=False
)


@st.composite
def wide_quantities(draw):
    """Quantities ranging from ~1e-18 to ~1e30."""
    mantissa = draw(st.integers(min_value=1, max_value=999999))
    exponent = draw(st.integers(min_value=-18, max_value=30))
    return Decimal(f"{mantissa}E{exponent}")


@st.composite
def wide_prices(draw):
    """Prices ranging from ~1e-18 to ~1e30."""
    mantissa = draw(st.integers(min_value=1, max_value=999999))
    exponent = draw(st.integers(min_value=-18, max_value=30))
    return Decimal(f"{mantissa}E{exponent}")


@st.composite
def fill_lists(draw):
    """Standard fill lists with typical magnitudes."""
    n = draw(st.integers(min_value=1, max_value=25))
    out = []
    for i in range(n):
        out.append(
            Fill(
                id=uuid4(),
                account_id=ACC,
                instrument_id=INST,
                executed_at=T0 + timedelta(minutes=i),
                side=draw(st.sampled_from([Side.BUY, Side.SELL])),
                quantity=draw(quantities),
                price=draw(prices),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id=None,
                is_estimated=False,
            )
        )
    return out


@st.composite
def wide_magnitude_fill_lists(draw):
    """Fill lists with quantities and prices spanning ~1e-18 to ~1e30."""
    n = draw(st.integers(min_value=2, max_value=8))
    out = []
    for i in range(n):
        out.append(
            Fill(
                id=uuid4(),
                account_id=ACC,
                instrument_id=INST,
                executed_at=T0 + timedelta(minutes=i),
                side=draw(st.sampled_from([Side.BUY, Side.SELL])),
                quantity=draw(wide_quantities()),
                price=draw(wide_prices()),
                fee=Decimal("0"),
                fee_currency="USD",
                source=FillSource.MANUAL,
                venue_fill_id=None,
                is_estimated=False,
            )
        )
    return out


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_every_fill_is_fully_allocated(fills):
    """No quantity may be lost or duplicated by grouping."""
    groups = group_fills(fills)
    allocated: dict[UUID, Fraction] = {}
    for g in groups:
        for a in g.allocations:
            allocated[a.fill_id] = allocated.get(a.fill_id, Fraction(0)) + Fraction(a.quantity)
    for f in fills:
        assert allocated.get(f.id, Fraction(0)) == Fraction(f.quantity)


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_closed_trades_net_to_flat(fills):
    """A closed trade's allocations must net exactly to zero position."""
    by_id = {f.id: f for f in fills}
    for g in group_fills(fills):
        if g.status is not TradeStatus.CLOSED:
            continue
        net = sum(
            (
                Fraction(a.quantity) if by_id[a.fill_id].side is Side.BUY else -Fraction(a.quantity)
                for a in g.allocations
            ),
            Fraction(0),
        )
        assert net == Fraction(0)


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_at_most_one_open_trade_per_instrument(fills):
    """At most one open trade per (account, instrument) pair."""
    groups = group_fills(fills)
    assert sum(1 for g in groups if g.status is TradeStatus.OPEN) <= 1


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_grouping_is_idempotent_under_reordering(fills):
    """Order of input must not change the result."""
    assert group_fills(fills) == group_fills(list(reversed(fills)))


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_allocations_are_always_positive(fills):
    """All allocations have strictly positive quantity."""
    for g in group_fills(fills):
        for a in g.allocations:
            assert a.quantity > 0


@given(wide_magnitude_fill_lists())
@settings(max_examples=200, deadline=None)
def test_every_fill_is_fully_allocated_wide_magnitude(fills):
    """No quantity may be lost or duplicated by grouping (wide magnitude test)."""
    groups = group_fills(fills)
    allocated: dict[UUID, Fraction] = {}
    for g in groups:
        for a in g.allocations:
            allocated[a.fill_id] = allocated.get(a.fill_id, Fraction(0)) + Fraction(a.quantity)
    for f in fills:
        assert allocated.get(f.id, Fraction(0)) == Fraction(f.quantity)


@given(wide_magnitude_fill_lists())
@settings(max_examples=200, deadline=None)
def test_closed_trades_net_to_flat_wide_magnitude(fills):
    """A closed trade's allocations must net exactly to zero position (wide magnitude test)."""
    by_id = {f.id: f for f in fills}
    for g in group_fills(fills):
        if g.status is not TradeStatus.CLOSED:
            continue
        net = sum(
            (
                Fraction(a.quantity) if by_id[a.fill_id].side is Side.BUY else -Fraction(a.quantity)
                for a in g.allocations
            ),
            Fraction(0),
        )
        assert net == Fraction(0)


def _sort_key(f: Fill) -> tuple[datetime, str]:
    """Match the grouper's own sort key for determining opening fills."""
    return (f.executed_at, str(f.id))


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_direction_matches_opening_fill(fills):
    """Direction must match the side of the fill that opened the trade."""
    by_id = {f.id: f for f in fills}
    groups = group_fills(fills)

    # Direction.SPREAD must never be produced by the auto-grouper
    for g in groups:
        assert g.direction is not Direction.SPREAD

    # For each group, find the opening fill (earliest by sort key)
    for g in groups:
        # All allocations must reference valid fills
        allocation_fills = [by_id[a.fill_id] for a in g.allocations]

        # Find the earliest fill by the grouper's own ordering
        opening_fill = min(allocation_fills, key=_sort_key)

        # Assert direction matches opening fill's side
        if opening_fill.side is Side.BUY:
            assert g.direction is Direction.LONG
        else:  # Side.SELL
            assert g.direction is Direction.SHORT


@given(wide_magnitude_fill_lists())
@settings(max_examples=200, deadline=None)
def test_direction_matches_opening_fill_wide_magnitude(fills):
    """Direction must match the side of the fill that opened the trade (wide magnitude test)."""
    by_id = {f.id: f for f in fills}
    groups = group_fills(fills)

    # Direction.SPREAD must never be produced by the auto-grouper
    for g in groups:
        assert g.direction is not Direction.SPREAD

    # For each group, find the opening fill (earliest by sort key)
    for g in groups:
        # All allocations must reference valid fills
        allocation_fills = [by_id[a.fill_id] for a in g.allocations]

        # Find the earliest fill by the grouper's own ordering
        opening_fill = min(allocation_fills, key=_sort_key)

        # Assert direction matches opening fill's side
        if opening_fill.side is Side.BUY:
            assert g.direction is Direction.LONG
        else:  # Side.SELL
            assert g.direction is Direction.SHORT
