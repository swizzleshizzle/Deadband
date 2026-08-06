"""Constraint tests for migration 001 (A-2 ledger completion): the three
expanded/added CHECK constraints must actually reject bad data, and the
cash_movement.kind expansion must actually accept the new values.
"""

import asyncpg
import pytest

from db.accounts import create_account
from tests.conftest import requires_db

pytestmark = requires_db


async def test_zero_contract_multiplier_is_rejected(conn):
    """A zero multiplier silently zeroes option P&L; the DB must refuse it."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            """INSERT INTO instrument (natural_key, asset_class, symbol,
                                       quote_currency, contract_multiplier)
               VALUES ('x:zero', 'option', 'ZERO', 'USD', 0)"""
        )


async def test_unknown_funding_source_is_rejected(conn):
    account_id = await create_account(
        conn, name="t", venue="fidelity", account_type="cash"
    )
    instrument_id = await conn.fetchval(
        """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
           VALUES ('x:fundingsrc', 'equity', 'FS', 'USD') RETURNING id"""
    )
    fill_id = await conn.fetchval(
        """INSERT INTO fill (account_id, instrument_id, executed_at, side,
                             quantity, price, source)
           VALUES ($1, $2, now(), 'buy', 1, 1, 'manual') RETURNING id""",
        account_id,
        instrument_id,
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            "UPDATE fill SET funding_source = 'nonsense' WHERE id = $1", fill_id
        )


async def test_negative_mark_price_is_rejected(conn):
    inst = await conn.fetchval(
        """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
           VALUES ('x:negmark', 'equity', 'NEG', 'USD') RETURNING id"""
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            "INSERT INTO mark (instrument_id, as_of, price) VALUES ($1, now(), -1)",
            inst,
        )


async def test_nan_contract_multiplier_is_rejected(conn):
    """NUMERIC accepts the literal 'NaN', and NaN compares greater than every
    finite value in Postgres -- `> 0` alone lets it through."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            """INSERT INTO instrument (natural_key, asset_class, symbol,
                                       quote_currency, contract_multiplier)
               VALUES ('x:nanmult', 'option', 'NANMULT', 'USD', 'NaN')"""
        )


async def test_infinite_contract_multiplier_is_rejected(conn):
    """NUMERIC also accepts the literal 'Infinity', which is also > 0."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            """INSERT INTO instrument (natural_key, asset_class, symbol,
                                       quote_currency, contract_multiplier)
               VALUES ('x:infmult', 'option', 'INFMULT', 'USD', 'Infinity')"""
        )


async def test_normal_contract_multiplier_is_accepted(conn):
    """Proves the tightened constraint didn't also reject legitimate values."""
    await conn.execute(
        """INSERT INTO instrument (natural_key, asset_class, symbol,
                                   quote_currency, contract_multiplier)
           VALUES ('x:normalmult', 'option', 'NORMALMULT', 'USD', 100)"""
    )


async def test_nan_mark_price_is_rejected(conn):
    inst = await conn.fetchval(
        """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
           VALUES ('x:nanmark', 'equity', 'NANMARK', 'USD') RETURNING id"""
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            "INSERT INTO mark (instrument_id, as_of, price) VALUES ($1, now(), 'NaN')",
            inst,
        )


async def test_infinite_mark_price_is_rejected(conn):
    inst = await conn.fetchval(
        """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
           VALUES ('x:infmark', 'equity', 'INFMARK', 'USD') RETURNING id"""
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            "INSERT INTO mark (instrument_id, as_of, price) VALUES ($1, now(), 'Infinity')",
            inst,
        )


async def test_zero_mark_price_is_accepted(conn):
    """Proves the tightened constraint didn't also reject a legitimate zero price."""
    inst = await conn.fetchval(
        """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
           VALUES ('x:zeromark', 'equity', 'ZEROMARK', 'USD') RETURNING id"""
    )
    await conn.execute(
        "INSERT INTO mark (instrument_id, as_of, price) VALUES ($1, now(), 0)",
        inst,
    )


async def test_return_of_capital_is_an_accepted_cash_kind(conn):
    """Guards the CHECK expansion: without it this raises and Part 2's rule
    table cannot record a return of capital at all."""
    account_id = await create_account(
        conn, name="t", venue="fidelity", account_type="cash"
    )
    await conn.execute(
        """INSERT INTO cash_movement (account_id, occurred_at, kind, amount)
           VALUES ($1, now(), 'return_of_capital', 10)""",
        account_id,
    )


async def test_tax_is_an_accepted_cash_kind(conn):
    account_id = await create_account(
        conn, name="t", venue="fidelity", account_type="cash"
    )
    await conn.execute(
        """INSERT INTO cash_movement (account_id, occurred_at, kind, amount)
           VALUES ($1, now(), 'tax', -5)""",
        account_id,
    )
