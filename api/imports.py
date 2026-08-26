"""POST /api/imports/preview and /api/imports/commit (spec section 3 of the
CSV import wizard plan).

Parses an uploaded broker export and returns exactly the reports
db/import_flow.py computes -- the same values cli.py renders for
`deadband import` -- so the wizard and the command line describe a file
identically. See db/import_flow.py's module docstring for why that module
exists at all: restating its routing/refusal rules here would be a second
place for them to drift out of agreement with the CLI's.

`_parse_upload` is the one parse path both endpoints share: resolving the
importer, decoding the upload, and calling `importer.parse()` went through
two fix rounds on the preview endpoint (see its refusals below), and a
second, independently-written copy of that path on the commit side would be
a second place for those fixes to go stale. commit_import calls it on the
SAME upload every time rather than trusting anything the client sent back
from a prior preview -- there is no server-side session state, so a commit
re-derives its batch from the file exactly as a bare `deadband import
--commit` would.
"""

from __future__ import annotations

import csv
import dataclasses
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from api.deps import get_conn, get_write_conn
from api.serialization import DeadbandJSONResponse
from db.import_flow import (
    AccountNotFoundError,
    AccountVenueMismatchError,
    BlockingRowsError,
    ImportCommitReport,
    MixedDedupePathsError,
    PreviewReport,
    TransferRefused,
    UnknownRefsError,
    UnroutableRowsError,
    commit,
    preview,
)
from importers.base import ImportBatch, Importer
from importers.registry import get_importer, list_importers

router = APIRouter()


async def _parse_upload(file: UploadFile, venue: str) -> tuple[Importer, ImportBatch]:
    """Resolve the importer and turn the upload into an `ImportBatch`.

    The three refusals below are unchanged from preview_import's own two fix
    rounds -- moved here verbatim, not reimplemented, so commit_import gets
    them for free and neither endpoint can drift from the other's parsing
    quirks.
    """
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

    try:
        batch = importer.parse(text)
    except csv.Error as exc:
        # The ONE exception type this actually needs to catch, confirmed by
        # exercising importers/fidelity.py directly rather than guessing:
        # every ValueError/decimal.InvalidOperation a malformed row can
        # produce (a bad date, a bad number, a non-finite quantity) is
        # already caught INSIDE parse()'s own row loop and turned into a
        # warning or a blocking reason -- see reject()'s docstring, which
        # exists precisely so that no row-level parse failure ever escapes
        # as an exception. What does still escape is the stdlib `csv`
        # module's own refusal of a single field over ~128KB ("field larger
        # than field limit"), which a genuinely wrong delimiter (or a
        # non-CSV file with no delimiter at all) reaches before parse() ever
        # gets a row to reject: a whole unsplit line becomes one field. This
        # is deliberately NOT a bare `except Exception`: an AssertionError
        # from parse()'s own "unhandled rule outcome" guard, for instance,
        # means the importer itself has a bug, and turning that into "your
        # file is bad" would hide a real defect behind a client-facing 422
        # instead of surfacing it as the 500 it actually is.
        raise HTTPException(422, f"could not parse this {venue} file: {exc}") from None

    return importer, batch


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
    importer, batch = await _parse_upload(file, venue)
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


def _serialize_commit(report: ImportCommitReport) -> dict:
    """Same reasoning as `_serialize` above: `routing` is a dataclass
    (RoutingReport) DeadbandJSONResponse's encoder cannot render on its own,
    so it is flattened with dataclasses.asdict() rather than restated field
    by field. Unlike PreviewReport.routing, ImportCommitReport.routing is
    never None -- a report only exists once a commit actually happened.
    """
    return {
        "fills_inserted": report.fills_inserted,
        "fills_skipped": report.fills_skipped,
        "cash_inserted": report.cash_inserted,
        "transfers_inserted": report.transfers_inserted,
        "transfers_skipped": report.transfers_skipped,
        "trades_regrouped": report.trades_regrouped,
        "warnings": report.warnings,
        "ignored_refs": report.ignored_refs,
        "routing": dataclasses.asdict(report.routing),
    }


@router.post("/api/imports/commit")
async def commit_import(
    file: UploadFile,
    venue: str = Form(...),
    account_id: UUID | None = Form(default=None),
    conn: asyncpg.Connection = Depends(get_write_conn),
) -> DeadbandJSONResponse:
    """Commit an uploaded broker export through the same db/import_flow the
    CLI's `deadband import --commit` uses.

    The upload is re-parsed here, exactly like preview_import -- there is no
    server-side session that could let a client hand back a batch it saw in
    an earlier preview response and have this endpoint trust it unread. Two
    consequences follow from that: a client MUST re-upload the same bytes to
    convert a preview into a commit (spec section 3), and `commit`'s own
    content_hash dedupe (db/importing.py) makes re-uploading the identical
    file a no-op rather than a second import, which is what makes "preview,
    then commit the same file" and "re-import last quarter's export" both
    safe to repeat.

    `source="csv"` is passed explicitly and is not a parameter of this route:
    commit_batch's own `source: str = "csv"` default is exactly the bug (I2)
    that made every API-synced fill claim CSV provenance, and this endpoint
    exists only for uploaded files, so there is nothing else it could
    honestly say.

    No transaction is opened here. `commit` already wraps every target
    account's insert and its `regroup_account` call in one
    `async with conn.transaction():` (see its docstring) before this handler
    ever sees a result, so a second, outer transaction here would be
    redundant at best and, nested without SAVEPOINTs, actively wrong.

    Every refusal below is a subclass of `db.import_flow.ImportRefused`
    (see there for what triggers each) and is raised before `commit` writes
    anything -- or, for MixedDedupePathsError/TransferRefused, from inside
    its transaction, which rolls back on the way out -- so a refused request
    always leaves the ledger exactly as it was.
    """
    importer, batch = await _parse_upload(file, venue)
    try:
        report = await commit(
            conn,
            venue=importer.account_venue,
            batch=batch,
            account_id=account_id,
            source="csv",
        )
    except UnroutableRowsError as exc:
        # 422: the file itself is fine, but the client's request (no
        # account_id) cannot be honoured without silently dropping rows.
        # Named here, not just in str(exc), because a wizard user has to
        # know the fix, not just the symptom.
        raise HTTPException(
            422, f"{exc}; pass account_id to say where they go"
        ) from None
    except BlockingRowsError as exc:
        # 422: row(s) in the file itself need a human decision (typically a
        # `corporate add` this API has no equivalent for yet) before this
        # file can commit at all.
        reasons = "; ".join(msg for _, msg in exc.reasons)
        raise HTTPException(422, f"{exc}: {reasons}") from None
    except AccountNotFoundError as exc:
        # 404, matching api/accounts.py and api/fills.py's existing
        # convention for "this id names no row" -- account_id is the one
        # thing in this request that is a direct resource reference, not a
        # property of the file.
        raise HTTPException(404, str(exc)) from None
    except AccountVenueMismatchError as exc:
        # 422, not 404: the account exists, so this is a semantically
        # invalid combination of (this file, that account) rather than a
        # missing resource -- the same category of error as UnroutableRows.
        raise HTTPException(422, str(exc)) from None
    except UnknownRefsError as exc:
        # 422: a ref in the file matches no registered account and carries
        # money -- a property of the file/registration state, not of
        # anything already written.
        raise HTTPException(422, str(exc)) from None
    except MixedDedupePathsError as exc:
        # 409: unlike the refusals above, this one exists ONLY because of
        # fills a target account already holds -- the request conflicts with
        # the account's current state, the textbook 409 case, not with
        # anything wrong in the upload by itself.
        raise HTTPException(409, str(exc)) from None
    except TransferRefused as exc:
        # 409 for the same reason: whether an outbound transfer can be
        # honoured depends entirely on that account's existing ledger
        # history (importing years out of order), not on the file in
        # isolation.
        raise HTTPException(409, str(exc)) from None
    return DeadbandJSONResponse(_serialize_commit(report))
