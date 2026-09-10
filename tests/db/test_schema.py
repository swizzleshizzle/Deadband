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


# --- migration 005: instrument.symbol may not be blank (issue #27) ----------
#
# The constraint is added NOT VALID, so these tests carry an extra burden the
# others do not: they must show it enforces on NEW rows despite never having
# validated the old ones. A test that only checked "the constraint exists"
# would pass against a NOT VALID constraint that enforces nothing.


async def test_a_blank_instrument_symbol_is_rejected(conn):
    """Issue #27's schema-level guard. The importer stopped minting these in
    PR #35; this is what stops anything else minting one."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
               VALUES ('x:blank::USD', 'equity', '', 'USD')"""
        )


async def test_a_whitespace_only_instrument_symbol_is_rejected(conn):
    """Why the constraint is `btrim(symbol) <> ''` and not `symbol <> ''`.
    A symbol of spaces renders as a nameless row exactly like a blank one."""
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
               VALUES ('x:ws::USD', 'equity', '   ', 'USD')"""
        )


async def test_a_real_instrument_symbol_is_accepted(conn):
    """The other half: a guard that refused everything would pass both tests
    above and be indistinguishable from a correct one."""
    got = await conn.fetchval(
        """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
           VALUES ('x:good:GOOD:USD', 'equity', 'GOOD', 'USD') RETURNING symbol"""
    )
    assert got == "GOOD"


async def test_an_upsert_may_not_repaint_a_symbol_blank(conn):
    """upsert_instrument (db/instruments.py) is the ONLY write path to this
    table, and its ON CONFLICT DO UPDATE repaints `symbol` from EXCLUDED. So
    the update path needs its own gate: an INSERT guard alone would leave a
    real row blankable through the conflict branch."""
    await conn.execute(
        """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
           VALUES ('x:upsert:REAL:USD', 'equity', 'REAL', 'USD')"""
    )
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await conn.execute(
            """INSERT INTO instrument (natural_key, asset_class, symbol, quote_currency)
               VALUES ('x:upsert:REAL:USD', 'equity', '', 'USD')
               ON CONFLICT (natural_key) DO UPDATE SET symbol = EXCLUDED.symbol"""
        )


async def test_the_constraint_is_not_validated(conn):
    """The load-bearing property, and the reason this can ship before the
    legacy blank-symbol row (known-gap #77) is repaired.

    `convalidated = false` is what lets the migration apply to a database that
    still holds that row -- a validated constraint would have failed the scan,
    and since `cli.py migrate` runs on every deploy that would break deploys
    permanently rather than failing once. Pinning it here means a future
    change that "tidies" this into a plain ADD CONSTRAINT fails in CI instead
    of in production.

    When gap #77 is repaired, a later migration runs VALIDATE CONSTRAINT and
    this assertion flips to True -- deliberately, so closing the repair cannot
    be done without noticing this test."""
    validated = await conn.fetchval(
        """SELECT convalidated FROM pg_constraint
           WHERE conname = 'instrument_symbol_not_blank'
             AND conrelid = 'instrument'::regclass"""
    )
    assert validated is False, (
        "instrument_symbol_not_blank is validated -- if gap #77's data repair "
        "landed, update this test; if not, a NOT VALID constraint was turned "
        "into a validating one and the next deploy will fail"
    )
