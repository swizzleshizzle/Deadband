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
    """Raised when an account's cash movements, its fills' instruments, or the
    fees on those fills span more than one currency (spec §7/R7). v1 does not
    model FX, and summing across currencies would produce a confident wrong
    number.

    All THREE sources are named because all three are checked. A fill carries
    two currencies -- `instrument.quote_currency` and `fill.fee_currency` --
    and this wording used to say only "movements or instruments" precisely
    because the second was unguarded (docs/known-gaps.md gap #24, struck in
    this PR's review round). It is guarded now, so the wording widens back to
    match what is actually checked. README.md words it the same way; keep the
    two in step.

    A ZERO fee is deliberately excluded from the fee-currency check below.
    `fill.fee_currency` is `TEXT NOT NULL DEFAULT 'USD'` (db/schema.sql:73), so
    a zero-fee fill on a EUR instrument carries a meaningless 'USD' that says
    nothing about the account -- refusing on it would be a false refusal of a
    genuinely single-currency account. A fee of zero adds zero to the balance
    in any currency, so its denomination cannot make the sum wrong."""


async def account_cash(conn: asyncpg.Connection, account_id: UUID) -> Decimal:
    """The account's cash balance implied by everything the ledger holds for
    it: its cash movements plus the cash effect of its fills (see
    ledger.cash.net_cash for why fills, not just movements, are required).

    Refuses a mixed-currency account: checks cash_movement.currency, the
    fills' instruments' quote_currency, and the fee_currency of every NONZERO
    fee -- an account can be single-currency in one and not the others -- and
    raises MixedCurrencyError naming every currency found if more than one is
    present. See MixedCurrencyError for why zero fees are exempt.
    """
    movement_rows = await conn.fetch(
        "SELECT kind, amount, currency FROM cash_movement WHERE account_id = $1",
        account_id,
    )
    fill_rows = await conn.fetch(
        """
        SELECT f.side, f.quantity, f.price, f.fee, f.fee_currency,
               i.contract_multiplier, i.quote_currency
          FROM fill f
          JOIN instrument i ON i.id = f.instrument_id
         WHERE f.account_id = $1
        """,
        account_id,
    )

    # Nonzero fees only -- see MixedCurrencyError's docstring: fee_currency
    # defaults to 'USD' in the schema, so a zero fee's currency is noise.
    fee_currencies = {r["fee_currency"] for r in fill_rows if r["fee"] != Decimal(0)}
    currencies = (
        {r["currency"] for r in movement_rows}
        | {r["quote_currency"] for r in fill_rows}
        | fee_currencies
    )
    if len(currencies) > 1:
        raise MixedCurrencyError(
            f"account {account_id} has cash movements/instruments/nonzero fill "
            f"fees in more than one currency: {', '.join(sorted(currencies))}"
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
