"""POST /api/imports/commit (spec section 3). All fixtures invented.

Built on the same two real Fidelity dialects test_imports_preview.py and
tests/db/test_cli.py use: Activity (per-row "Account Number" column, used
below wherever a ref is meant to route on its own) and History (no account
columns at all, used wherever a test needs rows this file cannot route
without an explicit account_id). Every value is made up; only the column
names and order are copied from tests/fixtures/fidelity/activity.csv and
tests/fixtures/fidelity/real_shape_history.csv.
"""

from db.accounts import create_account
from tests.conftest import requires_db

pytestmark = requires_db

_ACTIVITY_HEADER = (
    "Run Date,Account Number,Action,Symbol,Description,Quantity,Price,Commission,Fees,Amount"
)

_HISTORY_HEADER = (
    "Run Date,Action,Symbol,Description,Type,Price ($),Quantity,Commission ($),"
    "Fees ($),Accrued Interest ($),Amount ($),Cash Balance ($),Settlement Date"
)


def _sample_fidelity_csv(ref: str = "X12345678") -> str:
    """One invented equity buy row in the real Fidelity Activity dialect
    (header + column order copied from tests/fixtures/fidelity/activity.csv;
    every value below is made up, never a real row). Carries its own
    per-row account ref, so this routes without an explicit account_id."""
    row = f"01/15/2026,{ref},YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00"
    return _ACTIVITY_HEADER + "\n" + row + "\n"


def _sample_history_csv() -> str:
    """One invented equity buy row in the real Fidelity History dialect
    (header + column order copied from tests/fixtures/fidelity/
    real_shape_history.csv; every value below is made up, never a real
    row). This dialect carries no Account/Account Number column at all, so
    a row parsed from it has no external_ref and cannot route by itself --
    exactly the shape UnroutableRowsError and the explicit-account_id path
    below both exist for."""
    row = (
        '02/01/2026,YOU BOUGHT IMAGINARY WIDGET CO (IMWD) (Cash),IMWD,'
        'IMAGINARY WIDGET CO,Cash,25.00,20,"",0.05,"","-500.05",1000.00,02/03/2026'
    )
    return _HISTORY_HEADER + "\n" + row + "\n"


async def test_commit_writes_fills_and_regroups(client, conn):
    await create_account(
        conn, name="Imp", venue="fidelity", account_type="cash", external_ref="F1"
    )
    r = await client.post(
        "/api/imports/commit",
        files={"file": ("export.csv", _sample_fidelity_csv(ref="F1"), "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["fills_inserted"] >= 1
    assert body["trades_regrouped"] >= 1


async def test_committing_the_same_file_twice_is_a_no_op(client, conn):
    """content_hash dedupe is what makes a repeat import safe, and it is the
    property the whole 'import the same 90-day export again' workflow rests
    on. Asserted here because the wizard makes repeating an import one click."""
    await create_account(
        conn, name="Imp2", venue="fidelity", account_type="cash", external_ref="F2"
    )
    args = dict(
        files={"file": ("e.csv", _sample_fidelity_csv(ref="F2"), "text/csv")},
        data={"venue": "fidelity"},
    )
    first = (await client.post("/api/imports/commit", **args)).json()
    second = (await client.post("/api/imports/commit", **args)).json()
    assert first["fills_inserted"] >= 1
    assert second["fills_inserted"] == 0
    assert second["fills_skipped"] == first["fills_inserted"]


async def test_commit_refuses_unroutable_rows_without_an_account(client, conn):
    """Rows with no account ref and no chosen account would be silently
    dropped. Refusing is the only honest option."""
    r = await client.post(
        "/api/imports/commit",
        files={"file": ("e.csv", _sample_history_csv(), "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 422
    assert "account" in r.text.lower()


async def test_commit_routes_to_an_explicitly_chosen_account(client, conn):
    acc = await create_account(conn, name="Whole", venue="fidelity", account_type="cash")
    r = await client.post(
        "/api/imports/commit",
        files={"file": ("e.csv", _sample_history_csv(), "text/csv")},
        data={"venue": "fidelity", "account_id": str(acc)},
    )
    assert r.status_code == 200
    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) >= 1


async def test_commit_records_csv_as_the_fill_source(client, conn):
    """api/imports.py hardcodes source="csv" rather than relying on
    commit_batch's own default -- that default is exactly the bug (I2) that
    once made every API-synced fill claim CSV provenance. fill.source is the
    only column that can answer "where did this row come from", so the HTTP
    commit path needs its own assertion rather than trusting the CLI's
    equivalent test (tests/db/test_cli.py) to cover it by proxy."""
    acc = await create_account(
        conn, name="Src", venue="fidelity", account_type="cash", external_ref="F3"
    )
    r = await client.post(
        "/api/imports/commit",
        files={"file": ("e.csv", _sample_fidelity_csv(ref="F3"), "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 200
    sources = await conn.fetch("SELECT source FROM fill WHERE account_id = $1", acc)
    assert len(sources) >= 1
    assert all(row["source"] == "csv" for row in sources)
