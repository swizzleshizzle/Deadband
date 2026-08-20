"""GET /api/trades and /api/trades/{id} (spec §5, §6)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps import get_conn
from db.marks import latest_marks
from db.trades import query_trades
from ledger.pnl import unrealized_pnl
from ledger.types import Direction

router = APIRouter()


@router.get("/api/trades")
async def trades(
    conn: asyncpg.Connection = Depends(get_conn),
    account: UUID | None = None,
    intent: Literal["trade", "investment", "unassigned"] | None = None,
    instrument: str | None = None,
    status: Literal["open", "closed"] | None = None,
    from_: date | None = Query(default=None, alias="from"),
    to: date | None = None,
    tag: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    rows, total = await query_trades(
        conn,
        account_id=account,
        intent=intent,
        underlying=instrument,
        status=status,
        opened_from=datetime.combine(from_, datetime.min.time(), tzinfo=UTC) if from_ else None,
        # Inclusive `to` date -> exclusive next-midnight bound.
        opened_before=(
            datetime.combine(to + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
            if to
            else None
        ),
        tag=tag,
        limit=limit,
        offset=offset,
    )
    return {"trades": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/api/trades/{trade_id}")
async def trade_detail(
    trade_id: UUID, conn: asyncpg.Connection = Depends(get_conn)
) -> dict:
    trade = await conn.fetchrow("SELECT * FROM trade WHERE id = $1", trade_id)
    if trade is None:
        raise HTTPException(status_code=404, detail="trade not found")

    instrument = await conn.fetchrow(
        """
        SELECT i.* FROM instrument i
         WHERE i.id = COALESCE(
                   (SELECT f.instrument_id FROM fill f WHERE f.id = $1),
                   (SELECT d.instrument_id FROM derived_fill d WHERE d.id = $2))
        """,
        trade["opening_fill_id"],
        trade["opening_derived_fill_id"],
    )
    effective = (
        await conn.fetchrow(
            "SELECT * FROM instrument WHERE id = $1", trade["effective_instrument_id"]
        )
        if trade["effective_instrument_id"] is not None
        else None
    )

    fills = [
        dict(r)
        for r in await conn.fetch(
            """
            SELECT 'fill' AS source, f.id, f.executed_at, f.side, f.quantity,
                   f.price, f.fee, f.is_estimated, tf.quantity AS allocated_quantity
              FROM trade_fill tf JOIN fill f ON f.id = tf.fill_id
             WHERE tf.trade_id = $1 AND tf.fill_id IS NOT NULL
            UNION ALL
            SELECT 'derived_fill', d.id, d.executed_at, d.side, d.quantity,
                   d.price, d.fee, d.is_estimated, tf.quantity
              FROM trade_fill tf JOIN derived_fill d ON d.id = tf.derived_fill_id
             WHERE tf.trade_id = $1 AND tf.derived_fill_id IS NOT NULL
             ORDER BY executed_at, id
            """,
            trade_id,
        )
    ]

    # Timeline: the fills as events, corporate actions on the trade's
    # instrument(s) with an ex-date inside the trade's window, transfers that
    # closed quantity (branch B), and synthetic opened/closed endpoints. Typed
    # and open-ended (spec §6). Window-overlap attribution errs toward showing
    # -- see the spec's gaps table.
    window_start = trade["opened_at"]
    window_end = trade["closed_at"] or datetime.now(tz=UTC)
    instrument_ids = [
        i for i in {
            instrument["id"] if instrument else None,
            trade["effective_instrument_id"],
        } if i is not None
    ]
    timeline: list[dict] = [{"type": "opened", "at": trade["opened_at"]}]
    for f in fills:
        timeline.append(
            {
                "type": f["source"],
                "at": f["executed_at"],
                "side": f["side"],
                "quantity": f["allocated_quantity"],
                "price": f["price"],
            }
        )
    if instrument_ids:
        actions = await conn.fetch(
            """
            SELECT action_type, ex_date, ratio_numerator, ratio_denominator, note
              FROM corporate_action
             WHERE instrument_id = ANY($1::uuid[])
               AND ex_date >= $2::date AND ex_date <= $3::date
            """,
            instrument_ids,
            window_start.date(),
            window_end.date(),
        )
        for a in actions:
            timeline.append(
                {
                    "type": "corporate_action",
                    "at": datetime.combine(a["ex_date"], datetime.min.time(), tzinfo=UTC),
                    "action_type": a["action_type"],
                    "ratio_numerator": a["ratio_numerator"],
                    "ratio_denominator": a["ratio_denominator"],
                    "note": a["note"],
                }
            )
        if trade["qty_transferred"] is not None:
            transfers = await conn.fetch(
                """
                SELECT occurred_at, quantity, market_value, note
                  FROM asset_transfer
                 WHERE account_id = $1 AND instrument_id = ANY($2::uuid[])
                   AND occurred_at >= $3 AND occurred_at <= $4
                """,
                trade["account_id"],
                instrument_ids,
                window_start,
                window_end,
            )
            for t in transfers:
                timeline.append(
                    {
                        "type": "transfer",
                        "at": t["occurred_at"],
                        "quantity": t["quantity"],
                        "market_value": t["market_value"],
                        "note": t["note"],
                    }
                )
    if trade["closed_at"] is not None:
        timeline.append({"type": "closed", "at": trade["closed_at"]})
    _order = {"opened": 0, "closed": 2}
    timeline.sort(key=lambda e: (e["at"], _order.get(e["type"], 1)))

    mark = None
    unrealized = None
    if trade["status"] == "open" and trade["open_quantity"] and instrument_ids:
        pricing_id = trade["effective_instrument_id"] or instrument_ids[0]
        marks = await latest_marks(conn, [pricing_id])
        if pricing_id in marks:
            price, as_of = marks[pricing_id]
            mult = await conn.fetchval(
                "SELECT contract_multiplier FROM instrument WHERE id = $1", pricing_id
            )
            unrealized = unrealized_pnl(
                trade["open_quantity"],
                trade["open_cost_basis"] or Decimal(0),
                price,
                mult,
                Direction(trade["direction"]),
            )
            mark = {"price": price, "as_of": as_of}

    return {
        "trade": dict(trade),
        "instrument": dict(instrument) if instrument else None,
        "effective_instrument": dict(effective) if effective else None,
        "fills": fills,
        "timeline": timeline,
        "pnl": {
            "realized": trade["realized_pnl"],
            "gross_realized": trade["gross_realized_pnl"],
            "fees_total": trade["fees_total"],
            "fees_realized": trade["fees_realized"],
            "unrealized": unrealized,
            "mark": mark,
        },
        "r_multiple": trade["r_multiple"],
        "notes": trade["notes"],
    }
