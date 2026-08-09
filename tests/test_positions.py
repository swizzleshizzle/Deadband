# tests/test_positions.py
from decimal import Decimal
from uuid import UUID

from ledger.positions import TradeRow, aggregate_positions
from ledger.types import Direction

I1 = UUID("11111111-1111-1111-1111-111111111111")
I2 = UUID("22222222-2222-2222-2222-222222222222")
A1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
A2 = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def row(instrument_id=I1, symbol="ZXCO", multiplier="1", direction=Direction.LONG,
        qty="10", basis="20", estimated=False, account_id=A1, account_name="Acct1"):
    return TradeRow(
        account_id=account_id,
        account_name=account_name,
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
    nothing says so. The row must appear and name the problem.

    Also pins a deliberate asymmetry: both contributing trades agree on
    LONG, so `direction` is still `Direction.LONG` even though the position
    is unvaluable. `unvaluable_reason`, not `direction`, is the valuability
    gate -- a future caller must not treat "direction is set" as "safe to
    price." If direction resolution ever gets tied to whether a reason was
    recorded, this assertion is what catches it.
    """
    (p,) = aggregate_positions([row(qty="10", basis="20"), row(qty=None, basis=None)])
    assert p.direction is Direction.LONG
    assert p.unvaluable_reason is not None
    assert "unknown" in p.unvaluable_reason
    assert p.trade_count == 2


def test_all_trades_null_still_emits_a_position_not_a_vanished_row():
    """The sharpest case of 'a zero that looks like a real number': when
    every contributing trade has NULL quantity, `priced` is empty and the
    naive path would report a real-looking quantity=0, cost_basis=0 with no
    indication anything is wrong. The row must still appear, still carry a
    reason, and the zeros must be recognizable as placeholders rather than
    a genuinely flat position -- callers are expected to check
    `unvaluable_reason`, not assume zero quantity means "no position"."""
    (p,) = aggregate_positions([row(qty=None, basis=None), row(qty=None, basis=None)])
    assert p.quantity == Decimal("0")
    assert p.cost_basis == Decimal("0")
    assert p.unvaluable_reason is not None
    assert "unknown" in p.unvaluable_reason
    assert p.trade_count == 2


def test_multiple_reasons_are_joined_not_truncated_to_one():
    """No prior test trips two reasons on the same position, so a
    regression that keeps only the last (or first) reason -- e.g.
    `reasons[-1]` in place of `'; '.join(reasons)` -- passes every other
    test in this file. Trip both the NULL check and mixed-direction at once
    and pin the full compound string, in the order the checks run: NULL
    first, then direction."""
    (p,) = aggregate_positions([
        row(qty=None, basis=None, direction=Direction.LONG),
        row(qty="4", basis="20", direction=Direction.SHORT),
    ])
    assert p.unvaluable_reason == "open quantity unknown on at least one trade; mixed direction"
    assert p.direction is None


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


def test_the_contract_multiplier_is_carried_onto_the_position():
    """Final-review finding (Important 3): every fixture in this file
    defaulted `multiplier="1"`, no test varied it, and no test asserted
    `p.multiplier` -- so replacing `multiplier=first.multiplier` in
    aggregate_positions with a hardcoded `Decimal(1)` was green across the
    whole suite. On a 100-multiplier option contract that mutant is a 100x
    understatement of unrealized P&L, the single largest silent error this
    module can produce.

    Two contributors, so this also pins that the multiplier is taken from the
    group rather than invented: they share an instrument, and an instrument
    has exactly one contract multiplier."""
    (p,) = aggregate_positions([
        row(multiplier="100", qty="2", basis="2.50"),
        row(multiplier="100", qty="3", basis="3.00"),
    ])
    assert p.multiplier == Decimal("100")
    assert p.quantity == Decimal("5")
    assert p.unvaluable_reason is None


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


def test_symbol_collision_is_broken_by_instrument_id_not_insertion_order():
    """`instrument.symbol` is NOT unique in this schema -- only
    `instrument.natural_key` is (see ledger/types.py:instrument_natural_key,
    e.g. the same ticker on two chains, or a delisted-and-relisted equity).
    Two instruments can legitimately share a symbol, and when they do, the
    `str(instrument_id)` tiebreaker in the sort key is the only thing making
    the output order deterministic. Without it, sort falls back to whatever
    order plain dict iteration happened to produce, so `deadband positions`
    could print the two rows in a different order on two runs over identical
    data -- a spurious diff for the user, and an intermittent failure for any
    test that isn't careful, both miserable to track down.

    The input below inserts I2 before I1, the OPPOSITE of the expected
    (sorted-by-instrument_id) output order, so insertion order and correct
    order genuinely disagree. A test that inserted them in id order could
    pass by accident even with the tiebreaker removed.
    """
    ps = aggregate_positions([
        row(instrument_id=I2, symbol="DUPE"),
        row(instrument_id=I1, symbol="DUPE"),
    ])
    assert [p.instrument_id for p in ps] == [I1, I2]


def test_no_rows_is_no_positions_not_an_error():
    assert aggregate_positions([]) == ()


def test_two_accounts_holding_one_instrument_produce_two_rows_not_one():
    """The behaviour this whole change exists for. Before, grouping by
    instrument alone merged a taxable account's 10 @ 20 with a retirement
    account's 30 @ 50 into one blended row (quantity 40, basis 42.5) -- a
    cost basis that answers no question anyone actually has, because the two
    accounts' bases have different tax consequences. Now each account's
    holding is its own row, each individually valuable."""
    ps = aggregate_positions([
        row(account_id=A1, account_name="Taxable", qty="10", basis="20"),
        row(account_id=A2, account_name="Retirement", qty="30", basis="50"),
    ])
    assert len(ps) == 2
    by_account = {p.account_id: p for p in ps}
    assert by_account[A1].quantity == Decimal("10")
    assert by_account[A1].cost_basis == Decimal("20")
    assert by_account[A1].trade_count == 1
    assert by_account[A1].unvaluable_reason is None
    assert by_account[A2].quantity == Decimal("30")
    assert by_account[A2].cost_basis == Decimal("50")
    assert by_account[A2].trade_count == 1
    assert by_account[A2].unvaluable_reason is None


def test_long_in_one_account_and_short_in_another_is_not_mixed_direction():
    """The manufactured position the old grouping produced: long in a
    taxable account and short in a retirement account is two ordinary,
    single-direction positions in reality, not one unvaluable 'mixed
    direction' row. Grouping by (account, instrument) must let both price
    normally."""
    ps = aggregate_positions([
        row(account_id=A1, direction=Direction.LONG, qty="10", basis="20"),
        row(account_id=A2, direction=Direction.SHORT, qty="4", basis="50"),
    ])
    assert len(ps) == 2
    for p in ps:
        assert p.unvaluable_reason is None
    directions = {p.account_id: p.direction for p in ps}
    assert directions[A1] is Direction.LONG
    assert directions[A2] is Direction.SHORT


def test_ordering_falls_back_to_account_id_when_symbol_and_account_name_tie():
    """Account names are not unique either (nothing stops two accounts
    sharing a name), so a symbol-then-account-name sort key alone still has
    ties. `str(account_id)` is the tiebreaker that makes the order a total
    one when both the symbol and the account name collide.

    A1 sorts before A2 as a UUID string, and this inserts A2 first -- the
    opposite of the expected output order -- so insertion order and the
    correct (account_id-sorted) order genuinely disagree."""
    assert str(A1) < str(A2)
    ps = aggregate_positions([
        row(account_id=A2, account_name="Same", instrument_id=I1, symbol="DUPE"),
        row(account_id=A1, account_name="Same", instrument_id=I1, symbol="DUPE"),
    ])
    assert [p.account_id for p in ps] == [A1, A2]
