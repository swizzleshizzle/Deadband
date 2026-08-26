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


# --- structurally malformed CSV (Finding 1) ---------------------------------
#
# importers/fidelity.py is deliberately defensive against a garbled ROW: a
# bad date, a bad number, a non-finite quantity are all caught inside
# parse()'s own loop and turned into a warning or a blocking reason (see
# reject()'s docstring there) -- confirmed directly, not assumed, by parsing
# a header-less blob and a well-formed-but-columnless CSV through
# FidelityImporter().parse() and observing a normal (if empty) ImportBatch
# come back, no exception, in both cases.
#
# What DOES still escape parse() is the stdlib `csv` module's own refusal of
# a single field over ~128KB ("field larger than field limit") -- reached
# when a wrong delimiter (or a non-CSV file with no delimiter at all) means
# a whole line is read as one field before parse() ever gets a row to
# reject. Both tests below add that oversized field on top of the shape
# named in their title, since the shape ALONE (confirmed above) never
# raises -- only degrades to an empty, warning-laden, 200 preview.


async def test_preview_rejects_a_csv_whose_header_cannot_be_located(client):
    """No comma-separated structure at all -- the kind of upload you get if
    someone feeds the wizard a plain text file, or a non-CSV export, instead
    of a broker CSV. With nothing splitting it into fields, one line becomes
    one oversized field and the stdlib csv reader itself refuses it."""
    text = "not a csv file at all just prose with no delimiter in it " * 3000
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("export.csv", text, "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 422
    assert "fidelity" in r.json()["detail"]


async def test_preview_rejects_a_well_formed_csv_with_none_of_the_expected_columns(client):
    """Proper comma-delimited CSV, with a real header row and real
    delimiters -- just none of the column names Fidelity's importer looks
    for. Distinct from the previous test: this one exercises a row-level
    field (not the header line) growing past the csv module's field limit,
    so it must be checked independently rather than assumed to fail the
    same way."""
    text = "Foo,Bar,Baz\n1,2," + ("x" * 200_000) + "\n"
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("export.csv", text, "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 422
    assert "fidelity" in r.json()["detail"]


async def test_preview_parses_real_suffixed_money_headers_without_zeroing_them(client):
    """This is the exact defect class that motivated normalize_field
    (importers/base.py): a real Fidelity export suffixes its money columns
    with a currency parenthetical -- "Price ($)", "Fees($)" (no space is a
    real observed variant too), "Amount ($)" -- and a header lookup that
    misses the suffix silently reads Decimal("0") for every one, with the
    parse otherwise looking entirely successful.

    tests/fixtures/fidelity/activity.csv (reused by every other test in this
    file) does NOT carry these suffixes, so none of the other tests can catch
    a regression here -- this is the one test in this file built on the
    suffixed dialect instead, to close that gap at this endpoint specifically.

    PreviewReport carries no money figures at all (by design -- see its
    dataclass in db/import_flow.py; it reports routing and counts, not
    parsed amounts), so "reports non-zero money" is checked the only way this
    report can show it: zero_price_warning (importers/base.py) fires and
    lands in `warnings` if and only if a real, non-zero quantity parsed
    against a zero price -- exactly the silent-zero failure mode. Its
    ABSENCE here, alongside a fill actually being counted, is the proof the
    suffixed "Price ($)" column was read.
    """
    header = (
        "Run Date,Account Number,Action,Symbol,Description,Quantity,"
        "Price ($),Commission,Fees($),Amount ($)"
    )
    row = "01/15/2026,X12345678,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00"
    r = await client.post(
        "/api/imports/preview",
        files={"file": ("export.csv", header + "\n" + row + "\n", "text/csv")},
        data={"venue": "fidelity"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["fill_count"] == 1
    assert not any("zero price" in w for w in body["warnings"])
