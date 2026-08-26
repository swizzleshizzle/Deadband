"""POST /api/imports/preview (spec section 3 of the CSV import wizard plan).

Parses an uploaded broker export and returns exactly the PreviewReport
db/import_flow.py's `preview` computes -- the same values cli.py renders for
`deadband import` -- so the wizard and the command line describe a file
identically. See db/import_flow.py's module docstring for why that function
exists at all: restating its routing/refusal rules here would be a second
place for them to drift out of agreement with the CLI's.

Writes nothing. `preview` never opens a transaction and never inserts (see
its docstring); this handler only reads the upload and calls it with the
request's read connection so duplicates are probed like any other preview --
the wizard has no reason to hide them from the user just because the request
came in over HTTP instead of a terminal.
"""

from __future__ import annotations

import dataclasses
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from api.deps import get_conn
from api.serialization import DeadbandJSONResponse
from db.import_flow import PreviewReport, preview
from importers.registry import get_importer, list_importers

router = APIRouter()


def _serialize(report: PreviewReport) -> dict:
    """Everything the screen needs, and nothing recomputed here.

    `routing` and `duplicates` are dataclasses (RoutingReport, DuplicateReport)
    that DeadbandJSONResponse's encoder does not know how to render on its
    own -- it only special-cases Decimal/datetime/date/UUID (see
    api/serialization.py) -- so they are flattened with dataclasses.asdict()
    rather than reimplemented field by field, which would be a second place
    for their shape to drift from db/import_flow.py's. Everything else
    (tuples of str/UUID/int, and the StrEnum duplicates_skipped_reason, which
    is already a str subclass) is JSON-serializable as-is.
    """
    return {
        "fill_count": report.fill_count,
        "cash_count": report.cash_count,
        "transfer_count": report.transfer_count,
        "warnings": report.warnings,
        "unmapped_row_count": report.unmapped_row_count,
        "refs_seen": report.refs_seen,
        "rows_per_ref": report.rows_per_ref,
        "unknown_refs": report.unknown_refs,
        "unknown_money_refs": report.unknown_money_refs,
        "ignored_refs": report.ignored_refs,
        "blocking": report.blocking,
        "corporate_proposals": report.corporate_proposals,
        "routing": dataclasses.asdict(report.routing) if report.routing is not None else None,
        "duplicates": (
            dataclasses.asdict(report.duplicates) if report.duplicates is not None else None
        ),
        "duplicates_skipped_reason": report.duplicates_skipped_reason,
        "needs_account": report.needs_account,
    }


@router.post("/api/imports/preview")
async def preview_import(
    file: UploadFile,
    venue: str = Form(...),
    account_id: UUID | None = Form(default=None),
    conn: asyncpg.Connection = Depends(get_conn),
) -> DeadbandJSONResponse:
    # Resolved before the upload is even read: an unknown venue means there is
    # no importer to hand the bytes to, and cli.py refuses the same way
    # (get_importer raises KeyError, mapped here to 422 rather than a 500 --
    # an unrecognised --venue/venue is a client mistake, not a server fault).
    try:
        importer = get_importer(venue)
    except KeyError:
        raise HTTPException(
            422, f"unknown venue {venue!r}; available: {list_importers()}"
        ) from None

    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # Every real venue export here is plain text; a file that is not even
        # valid UTF-8 was never going to parse into rows, so refusing before
        # handing it to the importer gives a clear reason instead of whatever
        # exception the importer's own text processing happens to raise on
        # binary input.
        raise HTTPException(422, "file must be UTF-8 text") from None

    batch = importer.parse(text)
    # importer.account_venue, not importer.venue: this is the registered
    # account venue rows route/match against, and differs from the importer's
    # own identity for coinbase-api (see importers/base.py's Importer.account_venue
    # docstring and cli.py's _preview_or_commit, which the same rule protects).
    # conn=conn (never None): a request always has a connection to spend, and
    # the wizard's whole reason to exist is showing routing and duplicates
    # before anything commits -- hiding them behind the CLI's connection-free
    # default would make the preview strictly worse than what cli.py can already show.
    report = await preview(batch, venue=importer.account_venue, conn=conn, account_id=account_id)
    return DeadbandJSONResponse(_serialize(report))
