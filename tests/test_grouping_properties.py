"""Property-based tests for fill grouping invariants."""

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from uuid import UUID, uuid4

from hypothesis import given, settings
from hypothesis import strategies as st

from ledger.grouping import group_fills
from ledger.pnl import compute_pnl
from ledger.types import Direction, Fill, FillSource, Side, TradeStatus

ACC = UUID("00000000-0000-0000-0000-0000000000a1")
INST = UUID("00000000-0000-0000-0000-0000000000b1")
T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)

# Both fill strategies below emit a single instrument. A multiplier of 1 keeps
# the comparison in price terms, which is what the independent walk computes.
MULTIPLIERS = {INST: Decimal(1)}

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


# --- Spec §9: grouping must conserve VALUE, not just quantity ----------------
#
# gross_realized_from_fills below is a second, deliberately separate
# implementation of "total gross realized P&L for this set of fills". It must
# not share code with the production path, or the property it feeds is green by
# construction. Specifically it does NOT call group_fills, does NOT import
# anything from ledger.pnl, and does not reuse ledger.grouping's position walk;
# it re-derives trade boundaries itself from the signed position and computes
# value with an explicit running average cost. It also works in exact rational
# arithmetic (Fraction) rather than Decimal, so it borrows none of the
# production path's precision conventions either.


def gross_realized_from_fills(fills: list[Fill]) -> Fraction:
    """Total gross realized P&L computed straight from the fills, no grouper.

    Per (account, instrument), walk fills in time order maintaining a signed
    position and the cost of the currently-open position. Every unit that is
    closed contributes (exit_price - avg_cost) * qty, sign-flipped for shorts.
    Basis resets to zero whenever position returns to flat, which is what makes
    this comparable to a per-trade average-cost computation: the partition into
    trades is exactly the set of flat-to-flat segments, so a continuous walk
    that resets at flat is the partition-free statement of the same quantity.

    Exact: Fraction never rounds, so any disagreement with the production sum
    beyond the documented quantization is a real misattribution, not drift.
    """
    buckets: dict[tuple[UUID, UUID], list[Fill]] = defaultdict(list)
    for f in fills:
        buckets[(f.account_id, f.instrument_id)].append(f)

    total = Fraction(0)
    for key in sorted(buckets, key=lambda k: (str(k[0]), str(k[1]))):
        position = Fraction(0)  # signed: + long, - short
        basis = Fraction(0)  # cost of the open position, in price*qty terms
        for f in sorted(buckets[key], key=_sort_key):
            remaining = Fraction(f.quantity)
            price = Fraction(f.price)
            sign = 1 if f.side is Side.BUY else -1
            while remaining > 0:
                if position == 0 or (position > 0) == (sign > 0):
                    # Opening or scaling in: the whole remainder joins the basis.
                    basis += remaining * price
                    position += sign * remaining
                    remaining = Fraction(0)
                else:
                    # Reducing, possibly through zero. Only the part that fits
                    # against the open position realizes P&L; any excess re-enters
                    # the loop and opens a position the other way.
                    closed = min(remaining, abs(position))
                    avg_cost = basis / abs(position)
                    if position > 0:
                        total += (price - avg_cost) * closed
                    else:
                        total += (avg_cost - price) * closed
                    basis -= avg_cost * closed
                    position += sign * closed
                    remaining -= closed
                    if position == 0:
                        basis = Fraction(0)
    return total


# ledger.pnl quantizes each trade's gross to this scale (_QUANT) before
# returning it, so a sum over N trades can differ from the exact total by at
# most N half-quanta. Restated here rather than imported: the whole point of
# the helper above is not to share code with the thing it checks. The bound is
# ~1e-17 for the 25-fill lists this strategy produces, while the smallest value
# misattribution these strategies can express is (0.01 price) x (0.01 qty) =
# 1e-4 -- thirteen orders of magnitude of daylight.
_PNL_QUANTUM = Fraction(Decimal("1E-18"))


@given(fill_lists())
@settings(max_examples=200, deadline=None)
def test_sum_of_per_trade_realized_pnl_equals_the_total_from_fills(fills):
    """Spec §9. The only property tying GROUPING to VALUATION: every other
    property in this file checks conservation within a single trade, so an
    allocation that conserves quantity while misattributing value between two
    trades is invisible to all of them.

    Compared gross, not net: fee allocation across trades is its own convention
    and folding it in here would make a failure ambiguous between two causes.
    (The strategies emit fee=0 anyway, so gross is the whole story.)"""
    by_id = {f.id: f for f in fills}
    groups = group_fills(fills)
    per_trade = sum(
        (
            Fraction(compute_pnl(g.allocations, by_id, MULTIPLIERS, g.direction).gross_realized_pnl)
            for g in groups
        ),
        Fraction(0),
    )
    total = gross_realized_from_fills(fills)
    assert abs(per_trade - total) <= _PNL_QUANTUM * len(groups), (
        f"grouping moved {per_trade - total} of value: per-trade sum {per_trade} "
        f"!= {total} computed directly from fills across {len(groups)} trades"
    )
