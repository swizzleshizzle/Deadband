"""The History dialect: no Account/Account Number columns, a Cash Balance
column instead. Every existing fixture before this file was Activity & Orders
-- the only dialect the multi-year exports (and every corporate action) use.

See tests/fixtures/fidelity/real_shape_history.csv for the fixture's shape:
a BOM, two blank preamble lines, the header on line 3, a trailing legal
disclaimer block -- and, unique to this file, the corporate-action row
shapes: a reverse split and a name change as FROM/TO pairs sharing a #REOR
reference, a three-row merger, a single-row spinoff distribution, and a
cash-in-lieu-of-fractional-shares row. All values are fabricated.
"""

import pathlib
from decimal import Decimal

from importers.fidelity import FidelityImporter
from ledger.types import Side

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "fidelity" / "real_shape_history.csv"


def _batch():
    return FidelityImporter().parse(FIXTURE.read_text(encoding="utf-8"))


def test_the_history_dialect_parses_at_all():
    """The multi-year history export is a different dialect from every existing
    fixture -- no Account/Account Number columns, a Cash Balance column instead.
    Nothing covered it before this file, which is why external_ref=None went
    unnoticed."""
    batch = _batch()
    assert batch.fills
    assert batch.cash


def test_history_rows_carry_no_account_ref():
    """Not a defect -- the account is in the filename, not the file. Pinned
    because `import --account` is the only way these rows route, and a future
    change that started inventing a ref would silently route them somewhere."""
    batch = _batch()
    assert {f.external_ref for f in batch.fills} == {None}
    assert batch.refs_seen == ()


def test_corporate_action_rows_do_not_block_the_import():
    """Gap #33's actual acceptance test. These rows carry a nonzero quantity or
    amount and matched no rule before this task, so the money-carrying-unmapped
    policy blocked the whole import -- which is why two accounts could never be
    imported."""
    assert _batch().blocking == ()


def test_corporate_action_rows_produce_exactly_the_ordinary_rows_worth_of_output():
    """Ruling B: the brief's original version of this test asserted that no
    fill's venue_fill_id contains "REOR" -- which passes trivially, since
    corporate-action rows produce no fills at all (this importer never sets
    venue_fill_id anywhere), and would keep passing even if a future change
    started turning them into fills, which is exactly the regression this test
    exists to catch.

    Asserted instead on the EXACT count the fixture's two ordinary rows (one
    BUY, one dividend) produce: one fill, one cash movement. The other nine
    rows -- the reverse split pair, the name-change pair, the three-row
    merger, the spinoff, and the cash-in-lieu row -- are recognised and
    deferred, not recorded. A count is falsifiable in both directions: it
    fails if a corporate-action row starts producing a fill or cash movement,
    and it fails if the ordinary rows stop producing theirs."""
    batch = _batch()
    assert len(batch.fills) == 1
    assert len(batch.cash) == 1

    fill = batch.fills[0]
    assert fill.instrument.symbol == "ZXCO"
    assert fill.side is Side.BUY
    assert fill.quantity == Decimal("50")

    cash = batch.cash[0]
    assert cash.kind == "dividend"
    assert cash.amount == Decimal("6.25")
