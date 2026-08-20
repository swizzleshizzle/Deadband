"""GET /api/dashboard (spec §4): one call returns everything the Dashboard
renders. Valuation is honest about staleness (D6): every valued number
carries the mark it used, an unvaluable holding nulls its account's equity,
and a partial sum is never presented as a total."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import asyncpg
from fastapi import APIRouter, Depends

from api.deps import get_conn
from api.serialization import DeadbandJSONResponse
from db.accounts import list_accounts
from db.cash import MixedCurrencyError, account_cash
from db.marks import latest_marks
from db.positions import open_positions
from db.snapshots import latest_snapshot
from ledger.pnl import unrealized_pnl
from ledger.reconcile import (
    Position,
    ReconcileVerdict,
    Snapshot,
    UnvaluableRef,
    reconcile,
)
from ledger.types import Direction

router = APIRouter()

_RECENT_ACTIVITY_LIMIT = 20

_ACTIVITY_SQL = """
    SELECT * FROM (
        SELECT 'fill' AS type, f.executed_at AS at, f.account_id,
               i.symbol, f.side::text AS side, f.quantity, f.price,
               NULL::text AS kind, NULL::numeric AS amount
          FROM fill f JOIN instrument i ON i.id = f.instrument_id
        UNION ALL
        SELECT 'cash_movement', c.occurred_at, c.account_id,
               i.symbol, NULL, NULL::numeric, NULL::numeric, c.kind, c.amount
          FROM cash_movement c LEFT JOIN instrument i ON i.id = c.instrument_id
        UNION ALL
        SELECT 'transfer', t.occurred_at, t.account_id,
               i.symbol, NULL, t.quantity, NULL::numeric, NULL, t.market_value
          FROM asset_transfer t JOIN instrument i ON i.id = t.instrument_id
    ) events
     ORDER BY at DESC
     LIMIT $1
"""


@router.get("/api/dashboard")
async def dashboard(conn: asyncpg.Connection = Depends(get_conn)) -> DeadbandJSONResponse:
    now = datetime.now(UTC)

    accounts = await list_accounts(conn)
    all_positions = await open_positions(conn, None)
    valuable = [p for p in all_positions if p.unvaluable_reason is None]
    raw_marks = await latest_marks(conn, [p.instrument_id for p in valuable])

    positions_payload: list[dict] = []
    unvaluable_payload: list[dict] = []
    # Per-account working state for the tiles below.
    valued_sum: dict = {}
    has_unvalued: set = set()
    recon_positions: dict = {}
    recon_unvaluable: dict = {}

    for p in all_positions:
        entry = {
            "account_id": p.account_id,
            "instrument": {"id": p.instrument_id, "symbol": p.symbol, "multiplier": p.multiplier},
            "quantity": p.quantity,
            "cost_basis": p.cost_basis,
            "mark": None,
            "market_value": None,
            "unrealized_pnl": None,
            "is_estimated": p.is_estimated,
        }
        if p.unvaluable_reason is not None:
            has_unvalued.add(p.account_id)
            unvaluable_payload.append(
                {"instrument": entry["instrument"], "account_id": p.account_id,
                 "reason": p.unvaluable_reason}
            )
            recon_unvaluable.setdefault(p.account_id, []).append(
                UnvaluableRef(instrument_id=p.instrument_id, symbol=p.symbol,
                              reason=p.unvaluable_reason)
            )
        elif p.instrument_id in raw_marks:
            price, as_of = raw_marks[p.instrument_id]
            sign = Decimal(-1) if p.direction is Direction.SHORT else Decimal(1)
            value = sign * price * p.quantity * p.multiplier
            entry["mark"] = {"price": price, "as_of": as_of}
            entry["market_value"] = value
            entry["unrealized_pnl"] = unrealized_pnl(
                p.quantity, p.cost_basis, price, p.multiplier, p.direction
            )
            valued_sum[p.account_id] = valued_sum.get(p.account_id, Decimal(0)) + value
            recon_positions.setdefault(p.account_id, []).append(
                Position(instrument_id=p.instrument_id, quantity=p.quantity,
                         cost_basis=p.cost_basis, multiplier=p.multiplier,
                         direction=p.direction)
            )
        else:
            # Valuable in shape, but no mark exists. Visible twice by design:
            # nulls in place, plus a row here (spec §4).
            has_unvalued.add(p.account_id)
            unvaluable_payload.append(
                {"instrument": entry["instrument"], "account_id": p.account_id,
                 "reason": "no mark recorded"}
            )
        positions_payload.append(entry)

    marks_prices = {iid: price for iid, (price, _t) in raw_marks.items()}

    tiles: list[dict] = []
    drift_warnings: list[dict] = []
    aggregate: Decimal | None = Decimal(0)
    for a in accounts:
        acc_id = a["id"]
        try:
            cash = await account_cash(conn, acc_id)
        except MixedCurrencyError as exc:
            cash = None
            drift_warnings.append(
                {"account_id": acc_id, "verdict": ReconcileVerdict.UNRELIABLE.value,
                 "detail": str(exc)}
            )

        snap_row = await latest_snapshot(conn, acc_id, now)
        snapshot = None
        drift = None
        if snap_row is not None:
            snapshot = {
                "as_of": snap_row["as_of"],
                "total_equity": snap_row["total_equity"],
                "cash_balance": snap_row["cash_balance"],
            }
            if cash is not None:
                verdict = reconcile(
                    Snapshot(
                        account_id=acc_id,
                        as_of=snap_row["as_of"],
                        cash_balance=snap_row["cash_balance"],
                        total_equity=snap_row["total_equity"],
                    ),
                    recon_positions.get(acc_id, []),
                    marks_prices,
                    cash,
                    unvaluable=tuple(recon_unvaluable.get(acc_id, [])),
                )
                drift = {"verdict": verdict.verdict.value,
                         "amount": verdict.equity_difference}
                if verdict.verdict is not ReconcileVerdict.OK:
                    drift_warnings.append(
                        {"account_id": acc_id, "verdict": verdict.verdict.value,
                         "detail": f"equity difference {verdict.equity_difference}"}
                    )

        equity = None
        if cash is not None and acc_id not in has_unvalued:
            equity = cash + valued_sum.get(acc_id, Decimal(0))
        if equity is None:
            aggregate = None
        elif aggregate is not None:
            aggregate += equity

        tiles.append(
            {
                "id": acc_id,
                "name": a["name"],
                "venue": a["venue"],
                "account_type": a["account_type"],
                "base_currency": a["base_currency"],
                "is_active": a["is_active"],
                "cash": cash,
                "equity": equity,
                "snapshot": snapshot,
                "drift": drift,
            }
        )

    activity = [dict(r) for r in await conn.fetch(_ACTIVITY_SQL, _RECENT_ACTIVITY_LIMIT)]

    return DeadbandJSONResponse(
        {
            "generated_at": now,
            "equity": {"total": aggregate, "basis": "marks"},
            "accounts": tiles,
            "open_positions": positions_payload,
            "recent_activity": activity,
            "unvaluable": unvaluable_payload,
            "drift_warnings": drift_warnings,
        }
    )
