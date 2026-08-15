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
from tests.db.conftest import _split  # account_with_1800 is auto-discovered from conftest.py

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


async def test_preview_of_an_instrument_with_no_fills_is_empty(conn, an_instrument):
    """A legitimately pre-recorded future action, not an error."""
    preview = await preview_effect(conn, an_instrument, adding=_split(an_instrument))
    assert preview.fills_changed == 0
    assert preview.accounts == 0
