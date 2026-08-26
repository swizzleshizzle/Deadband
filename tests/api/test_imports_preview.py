"""POST /api/imports/preview (spec section 3). All fixtures invented.

The wizard's whole point is showing what a file WOULD do before anything is
written -- it must return the same PreviewReport shape db/import_flow.py
gives the CLI, not a restatement of it (see api/imports.py's module
docstring). Built on the real Fidelity "Account Number" activity dialect
(tests/fixtures/fidelity/activity.csv) rather than a hand-rolled dialect
sample: a header shape the parser would never actually see in production
proves nothing about the endpoint that has to parse real exports.
"""

from tests.api.conftest import assert_no_json_floats
from tests.conftest import requires_db

pytestmark = requires_db

_CSV = "Run Date,Action,Symbol,Quantity,Price ($),Amount ($)\n"


def _sample_fidelity_csv(ref: str = "X12345678") -> str:
    """One invented equity buy row in the real Fidelity activity dialect
    (header + column order copied from tests/fixtures/fidelity/activity.csv;
    every value below is made up, never a real row)."""
    header = (
        "Run Date,Account Number,Action,Symbol,Description,Quantity,Price,Commission,Fees,Amount"
    )
    row = f"01/15/2026,{ref},YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00"
    return header + "\n" + row + "\n"


async def test_preview_returns_counts_and_never_writes(client, conn):
    before = await conn.fetchval("SELECT count(*) FROM fill")
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("export.csv", _sample_fidelity_csv(), "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 200
    body = r.json()
    assert_no_json_floats(body)
    assert body["fill_count"] >= 1
    assert await conn.fetchval("SELECT count(*) FROM fill") == before


async def test_preview_reports_unknown_refs_rather_than_failing(client, conn):
    """An export naming an account you have not registered is a normal, common
    situation -- the screen must be able to SAY so. Refusing the whole preview
    would hide every other thing the file contains."""
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("export.csv", _sample_fidelity_csv(ref="NOSUCH"), "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 200
    assert "NOSUCH" in r.json()["unknown_refs"]


async def test_preview_rejects_an_unknown_venue(client):
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("x.csv", _CSV, "text/csv")},
        data={"venue": "notabroker"},
    )
    assert r.status_code == 422


async def test_preview_rejects_a_file_that_is_not_utf8_text(client):
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("x.csv", b"\xff\xfe\x00binary", "application/octet-stream")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 422
