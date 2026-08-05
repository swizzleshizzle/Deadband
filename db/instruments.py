"""Instrument repository. Identity is the natural key, not the caller's spelling."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

import asyncpg

from ledger.types import Instrument, instrument_natural_key


async def upsert_instrument(conn: asyncpg.Connection, instrument: Instrument) -> UUID:
    """Insert or fetch by natural key. Two spellings of one contract collapse to one row."""
    key = instrument_natural_key(instrument)
    return await conn.fetchval(
        """
        INSERT INTO instrument (
            natural_key, asset_class, symbol, quote_currency, underlying, strike,
            expiry, option_right, root, contract_multiplier, chain, contract_address
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
        ON CONFLICT (natural_key) DO UPDATE SET symbol = EXCLUDED.symbol
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
