"""Instrument repository. Identity is the natural key, not the caller's spelling."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from uuid import UUID

import asyncpg

from ledger.types import Instrument, instrument_natural_key


async def upsert_instrument(conn: asyncpg.Connection, instrument: Instrument) -> UUID:
    """Insert or fetch by natural key. Two spellings of one contract collapse to one row.

    Which columns sit inside vs. outside the natural key is NOT fixed --
    instrument_natural_key() varies it by asset_class (see ledger/types.py).
    Whatever is inside the key cannot drift on an existing row by
    construction: a different value there hashes to a different key, so it
    lands as a different row entirely rather than a conflict on this one.

    For the shapes this codebase currently mints -- option, equity, crypto
    spot -- the key is asset_class, underlying, expiry, strike, option_right,
    quote_currency (options) or asset_class, symbol/contract_address/chain,
    quote_currency (equity, crypto spot). The columns repainted below
    (symbol, root, chain, contract_address, contract_multiplier) are exactly
    the ones outside that key, and were previously frozen at whatever the
    first insert wrote -- so a wrong value was permanent. Of these,
    contract_multiplier is the one that costs money: it silently scales
    every option P&L on the instrument. All five are therefore repainted
    from EXCLUDED on every upsert, not just symbol.

    This scoping does NOT hold for every asset_class, and whoever adds the
    next one must re-check it, not assume it:
    - FUTURE puts `root` inside the key (see instrument_natural_key), so
      repainting `root` there is a no-op, not a correction -- harmless, but
      worth knowing before reading it as evidence root can drift.
    - No asset_class currently repaints `underlying`, `strike`, or
      `option_right` -- for OPTION rows that's correct, because those three
      are inside the key and cannot drift. But if a future asset_class ever
      puts any of them outside its key, they would freeze exactly the way
      contract_multiplier used to, silently, with no test to catch it.
    Today nothing mints FUTURE or CRYPTO_PERP instruments, so none of this
    is live risk yet -- but it will be the first thing a futures importer
    needs to get right.

    This restates history: repainting contract_multiplier changes how every
    existing fill on this instrument is valued, with no record that the
    multiplier ever changed. That is deliberate -- a wrong multiplier that
    can never be corrected is worse -- but it is a real effect, tracked as
    a gap for a later task (an audit trail / migration is a separate,
    larger design question, not taken here).

    That framing is one-directional and understates the risk: the same
    repaint also runs when the caller got it right the first time and wrong
    the second. contract_multiplier is now last-write-wins, and
    Instrument.contract_multiplier defaults to Decimal(1) -- correct for
    equity and crypto spot, silently 100x wrong for an option. Any future
    caller that mints an AssetClass.OPTION instrument without explicitly
    passing the multiplier will overwrite a correct 100 with 1 on this very
    upsert, retroactively revaluing every fill on the instrument, with no
    error and no log line. `Instrument.__post_init__` (ledger/types.py)
    guards exactly this by requiring an explicit contract_multiplier for
    OPTION instruments -- but that guard only fires at construction, so it
    protects a future Instrument(...) call site, not a hand-built row or a
    caller that constructs the object some other way.
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
