from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest_asyncio

from db.corporate import (
    actions_for_instruments,
    add_action,
    find_duplicate,
    list_actions,
    preview_effect,
    remove_action,
)
from db.instruments import upsert_instrument
from ledger.corporate import ActionType, CorporateAction
from ledger.types import AssetClass, Instrument
from tests.conftest import requires_db

# account_with_1800 and zxcb are fixtures, auto-discovered from conftest.py.
from tests.db.conftest import _spinoff, _split

pytestmark = requires_db


@pytest_asyncio.fixture
async def an_instrument(conn):
    return await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )


@pytest_asyncio.fixture
async def two_instruments(conn):
    a = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    b = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCB", quote_currency="USD"),
    )
    return a, b


async def test_an_action_round_trips(conn, an_instrument):
    action_id = await add_action(conn, _split(an_instrument))
    (row,) = await list_actions(conn, an_instrument)
    assert row["id"] == action_id
    assert row["action_type"] == "reverse_split"
    assert row["ratio_numerator"] == Decimal(1)
    assert row["ratio_denominator"] == Decimal(6)


async def test_actions_for_instruments_returns_the_pure_dataclass(conn, an_instrument):
    """regroup_account hands this straight to adjust_fills, which takes
    CorporateAction -- not a Record. Returning rows would push the TEXT-to-enum
    and NUMERIC-to-Decimal conversion into the caller, where it would be done
    once per call site and eventually wrong in one of them."""
    await add_action(conn, _split(an_instrument))
    (action,) = await actions_for_instruments(conn, [an_instrument])
    assert isinstance(action, CorporateAction)
    assert action.action_type is ActionType.REVERSE_SPLIT
    assert action.ratio_denominator == Decimal(6)


async def test_actions_are_scoped_to_the_instruments_asked_for(conn, two_instruments):
    a, b = two_instruments
    await add_action(conn, _split(a))
    assert await actions_for_instruments(conn, [b]) == []
    (action,) = await actions_for_instruments(conn, [a])
    assert action.instrument_id == a


async def test_find_duplicate_matches_on_instrument_ex_date_and_type(conn, an_instrument):
    """Entering the same 1:6 reverse split twice applies it twice -- a 1:36
    restatement that looks plausible at every individual step."""
    action_id = await add_action(conn, _split(an_instrument))
    assert await find_duplicate(
        conn, an_instrument, date(2026, 3, 2), ActionType.REVERSE_SPLIT
    ) == action_id


async def test_find_duplicate_ignores_a_different_ex_date(conn, an_instrument):
    await add_action(conn, _split(an_instrument))
    assert await find_duplicate(
        conn, an_instrument, date(2026, 4, 2), ActionType.REVERSE_SPLIT
    ) is None


async def test_remove_action_deletes_and_reports(conn, an_instrument):
    action_id = await add_action(conn, _split(an_instrument))
    assert await remove_action(conn, action_id) is True
    assert await list_actions(conn, an_instrument) == []


async def test_remove_action_reports_false_for_an_unknown_id(conn):
    assert await remove_action(conn, uuid4()) is False


async def test_preview_counts_the_fills_a_new_action_would_change(conn, account_with_1800):
    """1800 shares at 0.05, reverse split 1:6 -> 300 at 0.30."""
    account_id, instrument_id = account_with_1800
    preview = await preview_effect(conn, instrument_id, adding=_split(instrument_id))
    assert preview.fills_changed == 1
    assert preview.accounts == 1
    (before, after), = preview.samples
    assert before.quantity == Decimal(1800)
    assert after.quantity == Decimal(300)


async def test_preview_is_cumulative_not_against_raw_fills(conn, account_with_1800):
    """With one 1:6 split already stored, previewing a SECOND action must show
    the incremental change from 300, not another 1800 -> 300. An isolated
    preview would render a duplicate entry indistinguishable from a first."""
    account_id, instrument_id = account_with_1800
    await add_action(conn, _split(instrument_id))
    second = _split(instrument_id, ex_date=date(2026, 4, 2))
    preview = await preview_effect(conn, instrument_id, adding=second)
    (before, after), = preview.samples
    assert before.quantity == Decimal(300)
    assert after.quantity == Decimal(50)


async def test_preview_of_a_removal_shows_the_reverse(conn, account_with_1800):
    account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _split(instrument_id))
    preview = await preview_effect(conn, instrument_id, removing=action_id)
    (before, after), = preview.samples
    assert before.quantity == Decimal(300)
    assert after.quantity == Decimal(1800)


async def test_preview_of_an_added_spinoff_reports_the_child_it_creates(
    conn, account_with_1800, zxcb
):
    """A spinoff MINTS a fill for the resulting instrument. preview_effect used
    to iterate `before` and look each id up in `after`, so a fill present only in
    `after` was invisible: `corporate add --type spinoff` previewed the parent's
    basis reduction (0.05 -> 0.03125 at 37.5% allocated) and never mentioned the
    180 ZXCB shares it was about to create. `add` previews by default, so that is
    the preview omitting the most visible thing the action does.

    The input that would make this fail: this exact one -- 1800 ZXCO at 0.05,
    1:10 spinoff to ZXCB with 37.5% of basis allocated. Under the old
    before-keyed loop `fills_changed` is 1 (the parent) and `created` is empty;
    both assertions below move.

    Asserts the child's quantity and price, not just a count: a fix that
    incremented the counter without carrying the fill through would leave the
    rendering with nothing to print and this test green.
    """
    _account_id, instrument_id = account_with_1800
    preview = await preview_effect(conn, instrument_id, adding=_spinoff(instrument_id, zxcb))

    assert preview.fills_changed == 2  # the parent's basis, AND the new child
    assert preview.accounts == 1
    (child,) = preview.created
    assert child.instrument_id == zxcb
    assert child.quantity == Decimal(180)          # 1800 * 1/10
    assert child.price == Decimal("0.1875")        # (1800 * 0.05 * 0.375) / 180
    # The parent is still reported in `samples`, unchanged in quantity and
    # reduced in basis -- the new counting must add to the old, not replace it.
    ((before, after),) = preview.samples
    assert before.quantity == after.quantity == Decimal(1800)
    assert after.price == Decimal("0.03125")       # 0.05 * (1 - 0.375)


async def test_preview_of_a_removed_spinoff_reports_the_child_disappearing(
    conn, account_with_1800, zxcb
):
    """The mirror of the test above, and the reason the asymmetry was worth
    fixing rather than working around: removal was ALREADY counted (the loop
    over `before` sees the child vanish as `a is None`), so before this change
    adding a spinoff and removing it reported different fill counts for the same
    one action. `created` stays empty here -- nothing is minted by a removal.

    The input that would make this fail: making creations count by dropping the
    `a is None` branch instead of adding a second loop -- this would then report
    1 rather than 2.
    """
    _account_id, instrument_id = account_with_1800
    action_id = await add_action(conn, _spinoff(instrument_id, zxcb))
    preview = await preview_effect(conn, instrument_id, removing=action_id)
    assert preview.fills_changed == 2
    assert preview.created == ()


async def test_preview_of_an_instrument_with_no_fills_is_empty(conn, an_instrument):
    """A legitimately pre-recorded future action, not an error."""
    preview = await preview_effect(conn, an_instrument, adding=_split(an_instrument))
    assert preview.fills_changed == 0
    assert preview.accounts == 0
