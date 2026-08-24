"""GET /api/accounts and /api/accounts/{id}: the Accounts screen.

Scoped deliberately. Design section 8 specs this screen as "funded-account
rules and headroom, snapshot history, drift", but headroom is milestone C
(analytics/funded.py, which does not exist) and every one of those three needs
current equity, which needs marks. This endpoint returns what the ledger
actually holds -- config, cash, trade counts, realized P&L, open positions, and
the funded rule as recorded -- and computes nothing it cannot back.
"""

from __future__ import annotations

from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_conn
from api.serialization import DeadbandJSONResponse
from db.accounts import account_rollups, funded_rule, get_account, list_accounts
from db.cash import MixedCurrencyError, account_cash
from db.positions import open_positions

router = APIRouter()

# external_ref is excluded from both responses on purpose. It carries the real
# venue account number, and while this API binds localhost, it is served to a
# browser over a tailnet shared with other people. The screen has no use for it.
_ACCOUNT_FIELDS = (
    "id",
    "name",
    "venue",
    "account_type",
    "base_currency",
    "default_intent",
    "is_active",
    "ignore_on_import",
    "opened_at",
    "closed_at",
)


async def _summary(conn: asyncpg.Connection, account: asyncpg.Record, rollup) -> dict:
    """One account's list-row payload: its own columns plus the ledger-derived
    figures both responses share."""
    try:
        cash = await account_cash(conn, account["id"])
    except MixedCurrencyError:
        # v1 does not model FX, so there is no single number to show. Null,
        # not zero and not a partial sum -- /api/dashboard makes the same
        # choice for the same reason.
        cash = None

    payload = {k: account[k] for k in _ACCOUNT_FIELDS}
    payload.update(
        cash=cash,
        open_trades=rollup["open_trades"] if rollup else 0,
        closed_trades=rollup["closed_trades"] if rollup else 0,
        realized_pnl=rollup["realized_pnl"] if rollup else None,
        has_rule=rollup["has_rule"] if rollup else False,
    )
    return payload


@router.get("/api/accounts")
async def accounts(conn: asyncpg.Connection = Depends(get_conn)) -> DeadbandJSONResponse:
    rows = await list_accounts(conn)
    rollups = await account_rollups(conn)
    return DeadbandJSONResponse(
        {"accounts": [await _summary(conn, a, rollups.get(a["id"])) for a in rows]}
    )


@router.get("/api/accounts/{account_id}")
async def account_detail(
    account_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> DeadbandJSONResponse:
    account = await get_account(conn, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account not found")

    rollups = await account_rollups(conn)
    rule = await funded_rule(conn, account_id)

    # Positions carry quantity and basis but are NOT valued here. Valuation
    # lives in /api/dashboard and only there: two endpoints pricing the same
    # position independently is two chances to disagree about what it is
    # worth. `unvaluable_reason` still rides along, because a position that
    # cannot be valued must stay visible rather than silently vanish.
    positions = [
        {
            "instrument": {"id": p.instrument_id, "symbol": p.symbol, "multiplier": p.multiplier},
            "quantity": p.quantity,
            "cost_basis": p.cost_basis,
            "is_estimated": p.is_estimated,
            "unvaluable_reason": p.unvaluable_reason,
        }
        for p in await open_positions(conn, account_id)
    ]

    return DeadbandJSONResponse(
        {
            "account": await _summary(conn, account, rollups.get(account_id)),
            "funded_rule": dict(rule) if rule is not None else None,
            "open_positions": positions,
        }
    )
