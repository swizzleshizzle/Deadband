"""Account repository."""

from __future__ import annotations

from uuid import UUID

import asyncpg


class UnknownAccountError(LookupError):
    """Raised when an operation is given an account id with no matching row.

    Distinguished from a bare ValueError/LookupError so a caller (the CLI) can
    catch this one specifically and print a clean, account-naming message,
    while every other domain invariant violation still surfaces as a full
    traceback.
    """

    def __init__(self, account_id: UUID):
        super().__init__(f"no account with id {account_id}")
        self.account_id = account_id


async def create_account(
    conn: asyncpg.Connection,
    *,
    name: str,
    venue: str,
    account_type: str,
    default_intent: str = "trade",
    external_ref: str | None = None,
    base_currency: str = "USD",
    ignore_on_import: bool = False,
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO account (name, venue, external_ref, account_type,
                             default_intent, base_currency, ignore_on_import)
        VALUES ($1,$2,$3,$4,$5,$6,$7)
        RETURNING id
        """,
        name,
        venue,
        external_ref,
        account_type,
        default_intent,
        base_currency,
        ignore_on_import,
    )


async def get_account(conn: asyncpg.Connection, account_id: UUID) -> asyncpg.Record | None:
    return await conn.fetchrow("SELECT * FROM account WHERE id = $1", account_id)


async def find_by_external_ref(
    conn: asyncpg.Connection, venue: str, external_ref: str
) -> UUID | None:
    """Route imported rows to the right account when a venue has several."""
    return await conn.fetchval(
        "SELECT id FROM account WHERE venue = $1 AND external_ref = $2",
        venue,
        external_ref,
    )


async def list_accounts(conn: asyncpg.Connection) -> list[asyncpg.Record]:
    return await conn.fetch("SELECT * FROM account ORDER BY name")


async def account_rollups(conn: asyncpg.Connection) -> dict[UUID, asyncpg.Record]:
    """Per-account trade counts, total realized P&L, and whether a funded rule
    exists, keyed by account id.

    One grouped query rather than three per account: the Accounts list renders
    every account at once, and the per-account shape would be N+1 round trips
    against a screen whose whole job is the overview.

    LEFT JOINs, so an account with no trades still appears -- with zeroes. An
    INNER JOIN here would drop exactly the accounts a new user has, which is
    the case where an empty screen is least informative.
    """
    rows = await conn.fetch(
        """
        SELECT a.id,
               count(t.id) FILTER (WHERE t.status = 'open')   AS open_trades,
               count(t.id) FILTER (WHERE t.status = 'closed') AS closed_trades,
               coalesce(sum(t.realized_pnl), 0)               AS realized_pnl,
               (r.account_id IS NOT NULL)                     AS has_rule
          FROM account a
          LEFT JOIN trade t ON t.account_id = a.id
          LEFT JOIN funded_account_rule r ON r.account_id = a.id
         GROUP BY a.id, r.account_id
        """
    )
    return {r["id"]: r for r in rows}


async def funded_rule(conn: asyncpg.Connection, account_id: UUID) -> asyncpg.Record | None:
    """The account's funded-account rule row, or None. Returned exactly as
    recorded -- nothing here computes headroom or distance to breach, which
    belong to milestone C (analytics/funded.py) and need marks."""
    return await conn.fetchrow(
        "SELECT * FROM funded_account_rule WHERE account_id = $1", account_id
    )
