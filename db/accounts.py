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
) -> UUID:
    return await conn.fetchval(
        """
        INSERT INTO account (name, venue, external_ref, account_type,
                             default_intent, base_currency)
        VALUES ($1,$2,$3,$4,$5,$6)
        RETURNING id
        """,
        name,
        venue,
        external_ref,
        account_type,
        default_intent,
        base_currency,
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
