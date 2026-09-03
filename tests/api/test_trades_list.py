"""GET /api/trades: the filterable log (spec §5). All values invented."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from db.accounts import create_account
from db.fills import insert_fills
from db.instruments import upsert_instrument
from db.trades import regroup_account
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db

_T = datetime(2026, 5, 4, 14, 30, tzinfo=UTC)


def _fill(acc, inst, *, side, qty, price, minutes, ref):
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=_T.replace(minute=minutes % 60, hour=14 + minutes // 60),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=ref,
        is_estimated=False,
    )


async def _seed(conn):
    """One account, two instruments: a CLOSED round trip on ZZA (tagged) and
    an OPEN long on ZZB."""
    acc = await create_account(conn, name="A", venue="manual", account_type="cash")
    zza = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZA", quote_currency="USD")
    )
    zzb = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZB", quote_currency="USD")
    )
    await insert_fills(
        conn,
        [
            _fill(acc, zza, side=Side.BUY, qty="5", price="100", minutes=0, ref="a1"),
            _fill(acc, zza, side=Side.SELL, qty="5", price="110", minutes=10, ref="a2"),
            _fill(acc, zzb, side=Side.BUY, qty="2", price="50", minutes=20, ref="b1"),
        ],
    )
    await regroup_account(conn, acc)
    await conn.execute(
        "UPDATE trade SET strategy_tag = 'swing' WHERE account_id = $1 AND status = 'closed'",
        acc,
    )
    return acc


async def test_lists_newest_opened_first_with_total(client, conn):
    acc = await _seed(conn)
    r = await client.get("/api/trades", params={"account": str(acc)})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert [t["status"] for t in body["trades"]] == ["open", "closed"]
    assert body["trades"][0]["instrument_symbol"] == "ZZB"
    assert_no_json_floats(body)
    closed = body["trades"][1]
    assert closed["realized_pnl"] == "50.000000000000000000"
    assert isinstance(closed["qty_opened"], str)


async def test_filters_combine(client, conn):
    acc = await _seed(conn)
    r = await client.get(
        "/api/trades",
        params={"account": str(acc), "status": "closed", "instrument": "zza", "tag": "SWING"},
    )
    body = r.json()
    assert body["total"] == 1
    assert body["trades"][0]["primary_underlying"] == "ZZA"

    r = await client.get(
        "/api/trades", params={"account": str(acc), "status": "closed", "instrument": "zzb"}
    )
    assert r.json()["total"] == 0


async def test_date_window_is_inclusive(client, conn):
    acc = await _seed(conn)
    r = await client.get(
        "/api/trades",
        params={"account": str(acc), "from": "2026-05-04", "to": "2026-05-04"},
    )
    assert r.json()["total"] == 2
    r = await client.get(
        "/api/trades", params={"account": str(acc), "to": "2026-05-03"}
    )
    assert r.json()["total"] == 0


async def test_paging_keeps_total(client, conn):
    acc = await _seed(conn)
    r = await client.get(
        "/api/trades", params={"account": str(acc), "limit": 1, "offset": 1}
    )
    body = r.json()
    assert body["total"] == 2
    assert len(body["trades"]) == 1
    assert body["trades"][0]["status"] == "closed"
    assert (body["limit"], body["offset"]) == (1, 1)


async def test_bad_params_are_422(client):
    assert (await client.get("/api/trades", params={"account": "not-a-uuid"})).status_code == 422
    assert (await client.get("/api/trades", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/trades", params={"status": "sideways"})).status_code == 422


async def _seed_sortable(conn):
    """Four CLOSED round trips with DISTINCT realized P&L, plus one OPEN trade
    whose realized_pnl is NULL. Enough to tell a real sort from a no-op, which
    two rows cannot."""
    acc = await create_account(conn, name="S", venue="manual", account_type="cash")
    fills = []
    # Exits chosen so realized P&L is strictly ordered and none of them ties.
    for n, (sym, exit_price) in enumerate(
        [("ZS1", "110"), ("ZS2", "130"), ("ZS3", "90"), ("ZS4", "120")]
    ):
        inst = await upsert_instrument(
            conn,
            Instrument(id=None, asset_class=AssetClass.EQUITY, symbol=sym, quote_currency="USD"),
        )
        fills += [
            _fill(acc, inst, side=Side.BUY, qty="5", price="100", minutes=n * 10, ref=f"{sym}b"),
            _fill(acc, inst, side=Side.SELL, qty="5", price=exit_price,
                  minutes=n * 10 + 5, ref=f"{sym}s"),
        ]
    still_open = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZS5", quote_currency="USD")
    )
    fills.append(_fill(acc, still_open, side=Side.BUY, qty="1", price="10", minutes=99, ref="ZS5b"))
    await insert_fills(conn, fills)
    await regroup_account(conn, acc)
    return acc


async def test_trades_sort_by_realized_puts_the_biggest_win_first(client, conn):
    """The point of sortable columns. Server-side, because the list is paged
    50 at a time -- sorting the current page would answer "the biggest win on
    this page", which looks identical to the real answer and is not it."""
    acc = await _seed_sortable(conn)
    r = await client.get(
        "/api/trades", params={"account": str(acc), "sort": "realized", "dir": "desc", "limit": 200}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sort"] == "realized" and body["dir"] == "desc"
    values = [Decimal(t["realized_pnl"]) for t in body["trades"] if t["realized_pnl"] is not None]
    # Without this the assertion below is vacuous: [] is trivially sorted, and
    # an empty result is exactly what a test that forgets to seed produces.
    # Five, not four: an OPEN trade carries realized_pnl = 0, not NULL. Only
    # avg_exit and r_multiple go NULL while a trade is open.
    assert len(values) == 5, f"expected 5 trades to sort, got {len(values)}"
    assert len(set(values)) == 5, "seed must not produce ties, or order proves nothing"
    assert values == sorted(values, reverse=True)


async def test_trades_sort_puts_nulls_last_in_both_directions(client, conn):
    """A NULL avg_exit means "still open", not "lowest". Postgres sorts NULLs
    first on ASC by default, so without NULLS LAST, asking for the cheapest
    exits would return a page of trades that have never exited.

    Sorted on `exit` rather than `realized` deliberately: an open trade's
    realized_pnl is 0, not NULL, so that column would not exercise this at
    all -- which is what the first draft of this test got wrong."""
    acc = await _seed_sortable(conn)
    for direction in ("asc", "desc"):
        body = (await client.get(
            "/api/trades",
            params={"account": str(acc), "sort": "exit", "dir": direction, "limit": 200},
        )).json()
        nulls = [t for t in body["trades"] if t["avg_exit"] is None]
        reals = [t for t in body["trades"] if t["avg_exit"] is not None]
        assert nulls and reals, "seed must contain BOTH a NULL and a real value"
        seen_null = False
        for t in body["trades"]:
            if t["avg_exit"] is None:
                seen_null = True
            else:
                assert not seen_null, f"a non-NULL followed a NULL on dir={direction}"


async def test_trades_sort_is_stable_across_pages(client, conn):
    """Every sortable column except opened_at has ties in real data, and rows
    that compare equal have no defined order without a unique tiebreaker -- so
    paging through ties can show one trade twice and skip another. `t.id` is
    the final ORDER BY term for exactly this."""
    acc = await _seed_sortable(conn)
    q = {"account": str(acc), "sort": "status", "dir": "asc", "limit": 2}
    p1 = (await client.get("/api/trades", params={**q, "offset": 0})).json()["trades"]
    p2 = (await client.get("/api/trades", params={**q, "offset": 2})).json()["trades"]
    ids1, ids2 = [t["id"] for t in p1], [t["id"] for t in p2]
    # Four of the five seeded trades share status='closed', so this pages
    # straight through a tie -- which is the only arrangement that can catch a
    # missing tiebreaker.
    assert len(ids1) == 2 and len(ids2) == 2, "pages must be full for this to prove anything"
    assert not (set(ids1) & set(ids2)), "a trade appeared on two pages of one sorted list"


async def test_an_unknown_sort_key_is_refused_not_interpolated(client):
    """ORDER BY is built by string concatenation, so this is the injection
    boundary. FastAPI's Literal refuses it as a 422 before the query is built."""
    for bad in ("id; DROP TABLE trade", "opened_at", "'", "1"):
        r = await client.get("/api/trades", params={"sort": bad})
        assert r.status_code == 422, f"{bad!r} was not refused"


async def test_an_unknown_direction_is_refused(client):
    r = await client.get("/api/trades", params={"sort": "realized", "dir": "sideways"})
    assert r.status_code == 422


def test_the_order_by_fragment_always_ends_in_a_unique_tiebreaker():
    """Structural, because the behavioural test above cannot prove this.

    Rows that compare equal have no defined order without a unique tiebreaker,
    so paging through ties can show one trade twice and skip another. But that
    is a *may*, not a *must*: on a table this small Postgres returns a
    consistent order anyway, and the paging test passes with the tiebreaker
    removed. Asserting on the generated SQL is the only check here that
    actually goes red when `t.id` is dropped."""
    from db.trades import _TRADE_SORTS, _trade_order_by

    for key in _TRADE_SORTS:
        for direction in ("asc", "desc"):
            frag = _trade_order_by(key, direction)
            assert frag.rstrip().endswith("t.id"), f"{key}/{direction} has no tiebreaker: {frag}"
            assert "NULLS LAST" in frag, f"{key}/{direction} would sort NULLs first on asc"


def test_the_order_by_whitelist_refuses_anything_it_does_not_know():
    """The db layer guards independently of FastAPI's Literal. cli.py and the
    tests call query_trades directly, so a check that lives only at the HTTP
    edge protects only HTTP callers."""
    import pytest as _pytest

    from db.trades import _trade_order_by

    for bad in ("t.id; DROP TABLE trade", "opened_at", "", "1", "symbol) --"):
        with _pytest.raises(ValueError, match="unsortable"):
            _trade_order_by(bad, "asc")
    with _pytest.raises(ValueError, match="direction"):
        _trade_order_by("opened", "asc; DROP TABLE trade")
