"""GET /api/accounts and /api/accounts/{id}: the Accounts screen's data
(design section 8 screen 2, scoped to what the ledger actually holds).

Deliberately NOT covered, because the maths does not exist yet: funded-account
headroom, distance-to-breach and account equity all belong to milestone C
(analytics/funded.py) and all depend on marks. The rule row is returned as
recorded; nothing here computes against it.

All values invented.
"""

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

_T = datetime(2026, 5, 4, 14, 0, tzinfo=UTC)


def _fill(acc, inst, *, side, qty, price, minutes, ref):
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=_T.replace(minute=minutes),
        side=side,
        quantity=Decimal(qty),
        price=Decimal(price),
        fee=Decimal("0"),
        fee_currency="USD",
        source=FillSource.MANUAL,
        venue_fill_id=ref,
        is_estimated=False,
    )


async def _seed_account_with_one_of_each_trade(conn):
    """Deposit 1000; buy 2 @ 50 and sell 2 @ 60 (one closed trade, +20
    realized); buy 3 @ 10 and leave it open. Cash: 1000 - 100 + 120 - 30 = 990."""
    acc = await create_account(conn, name="AcctA", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZA", quote_currency="USD")
    )
    other = await upsert_instrument(
        conn, Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZZB", quote_currency="USD")
    )
    await conn.execute(
        "INSERT INTO cash_movement (account_id, occurred_at, kind, amount)"
        " VALUES ($1, $2, 'deposit', 1000)",
        acc,
        _T,
    )
    await insert_fills(
        conn,
        [
            _fill(acc, inst, side=Side.BUY, qty="2", price="50", minutes=1, ref="a1"),
            _fill(acc, inst, side=Side.SELL, qty="2", price="60", minutes=2, ref="a2"),
            _fill(acc, other, side=Side.BUY, qty="3", price="10", minutes=3, ref="a3"),
        ],
    )
    await regroup_account(conn, acc)
    return acc, inst, other


async def test_list_returns_account_config_and_ledger_derived_counts(client, conn):
    acc, _inst, _other = await _seed_account_with_one_of_each_trade(conn)

    r = await client.get("/api/accounts")
    assert r.status_code == 200
    body = r.json()
    assert_no_json_floats(body)

    row = next(a for a in body["accounts"] if a["id"] == str(acc))
    assert row["name"] == "AcctA"
    assert row["venue"] == "manual"
    assert row["account_type"] == "cash"
    assert row["base_currency"] == "USD"
    assert row["default_intent"] == "trade"
    assert row["is_active"] is True
    assert row["ignore_on_import"] is False
    assert row["open_trades"] == 1
    assert row["closed_trades"] == 1
    assert row["has_rule"] is False


async def test_list_renders_money_as_exact_decimal_strings(client, conn):
    acc, _i, _o = await _seed_account_with_one_of_each_trade(conn)

    body = (await client.get("/api/accounts")).json()
    row = next(a for a in body["accounts"] if a["id"] == str(acc))

    # Strings, not floats -- the app-wide convention (spec D4). Compared as
    # Decimals so a change of scale ('20' vs '20.00') is not a failure.
    assert isinstance(row["cash"], str)
    assert isinstance(row["realized_pnl"], str)
    assert Decimal(row["cash"]) == Decimal("990")
    assert Decimal(row["realized_pnl"]) == Decimal("20")


async def test_list_nulls_cash_for_a_mixed_currency_account_rather_than_summing(client, conn):
    """account_cash refuses an account whose movements span currencies, because
    v1 does not model FX. The tile must show nothing rather than a confident
    wrong number -- the same choice /api/dashboard makes."""
    acc = await create_account(conn, name="AcctMixed", venue="manual", account_type="cash")
    for currency, amount in (("USD", 100), ("EUR", 50)):
        await conn.execute(
            "INSERT INTO cash_movement (account_id, occurred_at, kind, amount, currency)"
            " VALUES ($1, $2, 'deposit', $3, $4)",
            acc,
            _T,
            Decimal(amount),
            currency,
        )

    body = (await client.get("/api/accounts")).json()
    row = next(a for a in body["accounts"] if a["id"] == str(acc))
    assert row["cash"] is None


async def test_list_never_exposes_external_ref(client, conn):
    """external_ref holds the real account number. The app is reachable over a
    tailnet shared with other people; the screen has no use for it."""
    await create_account(
        conn,
        name="AcctWithRef",
        venue="fidelity",
        account_type="cash",
        external_ref="Z12345678",
    )

    r = await client.get("/api/accounts")
    assert "Z12345678" not in r.text
    assert all("external_ref" not in a for a in r.json()["accounts"])


async def test_detail_returns_open_positions_without_valuing_them(client, conn):
    """Valuation lives in /api/dashboard and nowhere else: one owner means the
    two screens cannot disagree about what a position is worth. Detail carries
    quantity and basis only."""
    acc, _inst, other = await _seed_account_with_one_of_each_trade(conn)

    r = await client.get(f"/api/accounts/{acc}")
    assert r.status_code == 200
    body = r.json()
    assert_no_json_floats(body)

    assert body["account"]["id"] == str(acc)
    pos = next(p for p in body["open_positions"] if p["instrument"]["id"] == str(other))
    assert Decimal(pos["quantity"]) == Decimal("3")
    # PER-UNIT average cost, not the position total: ledger/positions.py
    # computes `weighted / quantity`. 3 @ 10 is 10 here, not 30. Pinned
    # because "cost_basis" reads as a total at a glance, and the UI has to
    # label it as an average or it will be misread the same way.
    assert Decimal(pos["cost_basis"]) == Decimal("10")
    assert pos["instrument"]["symbol"] == "ZZB"
    assert "market_value" not in pos
    assert "mark" not in pos


async def test_detail_returns_the_funded_rule_as_recorded_when_one_exists(client, conn):
    acc = await create_account(conn, name="AcctFunded", venue="apex", account_type="funded")
    await conn.execute(
        """INSERT INTO funded_account_rule
               (account_id, max_drawdown, drawdown_type, daily_loss_limit,
                profit_target, payout_split, consistency_rule)
           VALUES ($1, 2500, 'trailing', 1000, 3000, 0.9, 'no day over 40% of profit')""",
        acc,
    )

    body = (await client.get(f"/api/accounts/{acc}")).json()
    rule = body["funded_rule"]
    assert Decimal(rule["max_drawdown"]) == Decimal("2500")
    assert rule["drawdown_type"] == "trailing"
    assert Decimal(rule["daily_loss_limit"]) == Decimal("1000")
    assert Decimal(rule["profit_target"]) == Decimal("3000")
    assert Decimal(rule["payout_split"]) == Decimal("0.9")
    assert rule["consistency_rule"] == "no day over 40% of profit"

    # No headroom, no distance-to-breach: that is milestone C and needs marks.
    assert "headroom" not in rule


async def test_detail_funded_rule_is_null_when_the_account_has_none(client, conn):
    acc, _i, _o = await _seed_account_with_one_of_each_trade(conn)

    body = (await client.get(f"/api/accounts/{acc}")).json()
    assert body["funded_rule"] is None


async def test_list_flags_which_accounts_have_a_rule(client, conn):
    """has_rule drives whether the list hints at a rules panel, so it must not
    be hardcoded false -- the bug that would make every funded account look
    like a cash one."""
    funded = await create_account(conn, name="AcctF2", venue="apex", account_type="funded")
    plain = await create_account(conn, name="AcctP2", venue="manual", account_type="cash")
    await conn.execute(
        "INSERT INTO funded_account_rule (account_id, max_drawdown) VALUES ($1, 500)", funded
    )

    rows = {a["id"]: a for a in (await client.get("/api/accounts")).json()["accounts"]}
    assert rows[str(funded)]["has_rule"] is True
    assert rows[str(plain)]["has_rule"] is False


async def test_detail_404s_on_an_unknown_account(client):
    r = await client.get(f"/api/accounts/{uuid4()}")
    assert r.status_code == 404


# --- PATCH /api/accounts/{id}: renaming ------------------------------------
#
# Accounts arrive from an import named after their number ("Fidelity 856"),
# which says nothing about what the account is for.


async def test_rename_changes_the_name_and_nothing_else(client, conn):
    acc = await create_account(conn, name="Fidelity 856", venue="fidelity", account_type="cash")
    before = await conn.fetchrow("SELECT * FROM account WHERE id = $1", acc)

    r = await client.patch(f"/api/accounts/{acc}", json={"name": "Options income"})
    assert r.status_code == 200
    assert r.json()["name"] == "Options income"

    after = await conn.fetchrow("SELECT * FROM account WHERE id = $1", acc)
    assert after["name"] == "Options income"
    # external_ref is what imports route on (never the nickname -- see the
    # comment in importers/fidelity.py). Renaming must not disturb it, or a
    # rename would silently orphan every future import for this account.
    assert after["external_ref"] == before["external_ref"]
    assert after["venue"] == before["venue"]
    assert after["account_type"] == before["account_type"]


async def test_rename_refuses_a_blank_name(client, conn):
    """`account.name` is TEXT NOT NULL, which does NOT forbid the empty string
    -- the exact shape of issue #27, where a blank instrument symbol produced
    a row that was visibly populated and silently nameless. A rename endpoint
    that accepted "" would reintroduce it one table over."""
    acc = await create_account(conn, name="Fidelity 856", venue="fidelity", account_type="cash")
    for blank in ("", "   ", "\t"):
        r = await client.patch(f"/api/accounts/{acc}", json={"name": blank})
        assert r.status_code == 422, f"{blank!r} was accepted"
    assert await conn.fetchval("SELECT name FROM account WHERE id = $1", acc) == "Fidelity 856"


async def test_rename_trims_surrounding_whitespace(client, conn):
    """Stored trimmed, so " Roth " and "Roth" cannot become two names that
    render identically in a picker."""
    acc = await create_account(conn, name="Fidelity 856", venue="fidelity", account_type="cash")
    r = await client.patch(f"/api/accounts/{acc}", json={"name": "  Roth IRA  "})
    assert r.status_code == 200
    assert await conn.fetchval("SELECT name FROM account WHERE id = $1", acc) == "Roth IRA"


async def test_rename_404s_on_an_unknown_account(client):
    r = await client.patch(f"/api/accounts/{uuid4()}", json={"name": "Anything"})
    assert r.status_code == 404


async def test_rename_shows_up_in_the_accounts_list(client, conn):
    """The rename is worthless if the screen that displays it does not see it."""
    acc = await create_account(conn, name="Fidelity 856", venue="fidelity", account_type="cash")
    await client.patch(f"/api/accounts/{acc}", json={"name": "Swing book"})
    listing = (await client.get("/api/accounts")).json()["accounts"]
    names = {a["id"]: a["name"] for a in listing}
    assert names[str(acc)] == "Swing book"
