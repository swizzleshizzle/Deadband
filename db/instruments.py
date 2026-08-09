"""Instrument repository. Identity is the natural key, not the caller's spelling."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

import asyncpg

from ledger.types import Instrument, instrument_natural_key


async def upsert_instrument(conn: asyncpg.Connection, instrument: Instrument) -> UUID:
    """Insert or fetch by natural key. Two spellings of one contract collapse to one row.

    `natural_key` is built from asset_class, underlying, expiry, strike,
    option_right, quote_currency (and, for non-derivative rows, symbol or
    contract_address/chain) -- see instrument_natural_key(). Those fields
    cannot drift on an existing row by construction: a different value there
    hashes to a different key, so it lands as a different row entirely
    rather than a conflict on this one.

    The remaining columns (symbol, root, chain, contract_address,
    contract_multiplier) sit outside the key and were previously frozen at
    whatever the first insert wrote -- so a wrong value was permanent. Of
    these, contract_multiplier is the one that costs money: it silently
    scales every option P&L on the instrument. All five are therefore
    repainted from EXCLUDED on every upsert, not just symbol.

    This restates history: repainting contract_multiplier changes how every
    existing fill on this instrument is valued, with no record that the
    multiplier ever changed. That is deliberate -- a wrong multiplier that
    can never be corrected is worse -- but it is a real effect, tracked as
    a gap for a later task (an audit trail / migration is a separate,
    larger design question, not taken here).
    """
    key = instrument_natural_key(instrument)
    return await conn.fetchval(
        """
        INSERT INTO instrument (
            natural_key, asset_class, symbol, quote_currency, underlying, strike,
            expiry, option_right, root, contract_multiplier, chain, contract_address
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        ON CONFLICT (natural_key) DO UPDATE SET
            symbol              = EXCLUDED.symbol,
            contract_multiplier = EXCLUDED.contract_multiplier,
            root                = EXCLUDED.root,
            chain               = EXCLUDED.chain,
            contract_address    = EXCLUDED.contract_address
        RETURNING id
        """,
        key,
        instrument.asset_class.value,
        instrument.symbol,
        instrument.quote_currency.upper(),
        instrument.underlying,
        instrument.strike,
        instrument.expiry,
        instrument.option_right,
        instrument.root,
        instrument.contract_multiplier,
        instrument.chain,
        instrument.contract_address,
    )


async def get_multipliers(
    conn: asyncpg.Connection, instrument_ids: Sequence[UUID]
) -> dict[UUID, Decimal]:
    if not instrument_ids:
        return {}
    rows = await conn.fetch(
        "SELECT id, contract_multiplier FROM instrument WHERE id = ANY($1::uuid[])",
        list(instrument_ids),
    )
    return {r["id"]: r["contract_multiplier"] for r in rows}
