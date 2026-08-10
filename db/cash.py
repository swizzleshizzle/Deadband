# db/cash.py
"""Account cash balance: fetch movements and fills from Postgres, map them to
the pure ledger row types, and delegate the arithmetic to ledger.cash.net_cash.

No arithmetic lives here -- fetch, map, delegate."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import asyncpg

from ledger.cash import CashFillRow, CashMovementRow, net_cash
from ledger.types import Side


class MixedCurrencyError(RuntimeError):
    """Raised when an account's cash movements and/or fills span more than one
    currency (spec §7/R7). v1 does not model FX, and summing across
    currencies would produce a confident wrong number."""


async def account_cash(conn: asyncpg.Connection, account_id: UUID) -> Decimal:
    """The account's cash balance implied by everything the ledger holds for
    it: its cash movements plus the cash effect of its fills (see
    ledger.cash.net_cash for why fills, not just movements, are required).

    Refuses a mixed-currency account: checks both cash_movement.currency and
    the fills' instruments' quote_currency -- an account can be single-currency
    in one and not the other -- and raises MixedCurrencyError naming every
    currency found if more than one is present.
    """
    movement_rows = await conn.fetch(
        "SELECT kind, amount, currency FROM cash_movement WHERE account_id = $1",
        account_id,
    )
    fill_rows = await conn.fetch(
        """
        SELECT f.side, f.quantity, f.price, f.fee, i.contract_multiplier, i.quote_currency
          FROM fill f
          JOIN instrument i ON i.id = f.instrument_id
         WHERE f.account_id = $1
        """,
        account_id,
    )

    currencies = {r["currency"] for r in movement_rows} | {r["quote_currency"] for r in fill_rows}
    if len(currencies) > 1:
        raise MixedCurrencyError(
            f"account {account_id} has cash movements/fills in more than one "
            f"currency: {', '.join(sorted(currencies))}"
        )

    movements = [CashMovementRow(kind=r["kind"], amount=r["amount"]) for r in movement_rows]
    fills = [
        CashFillRow(
            side=Side(r["side"]),
            quantity=r["quantity"],
            price=r["price"],
            multiplier=r["contract_multiplier"],
            fee=r["fee"],
        )
        for r in fill_rows
    ]
    return net_cash(movements, fills)
