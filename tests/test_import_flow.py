"""The one import-flow guarantee that must hold with NO database at all.

Lives outside tests/db deliberately, so it runs in the pure lane: a test whose
whole claim is "this never reaches for a connection" has no business being
skipped when the database is absent -- that is precisely the condition it
describes.

All values invented."""

import pytest

import db.pool
from db import import_flow
from db.import_flow import DuplicateProbeSkipped, PreviewReport, preview
from importers.base import ImportBatch
from tests.import_flow_fixtures import fill_with_ref


async def test_preview_opens_no_connection_when_duplicates_are_not_requested(monkeypatch):
    """A deliberate guarantee, pinned for the CLI by
    test_preview_import_never_opens_a_database_connection. conn=None must be a
    supported call, not an accident that happens to work.

    Asserting only on the returned fields would pass unchanged even if preview
    opened a pool of its own -- the exact thing this test's name claims it
    prevents, and a vacuous guard this branch has already paid for once. Two
    checks, because they fail on different regressions: create_pool raises if
    anything reaches for it AT CALL TIME (a lazy, in-function import), and the
    attribute assertion catches a module-level `from db.pool import
    create_pool`, whose binding is captured at import time and would sail
    straight past the monkeypatch.
    """

    async def boom(*_args, **_kwargs):
        raise AssertionError("preview must not open a database connection")

    monkeypatch.setattr(db.pool, "create_pool", boom)
    assert not hasattr(import_flow, "create_pool"), (
        "db.import_flow must not import create_pool at all -- the "
        "connection-free guarantee is structural, not a rule to remember"
    )

    batch = ImportBatch(warnings=("w1",), unmapped_rows=("r1",), refs_seen=("A",))
    report = await preview(batch, venue="fidelity", conn=None)
    assert isinstance(report, PreviewReport)
    assert report.warnings == ("w1",)
    assert report.unmapped_row_count == 1
    assert report.duplicates is None
    assert report.duplicates_skipped_reason is DuplicateProbeSkipped.NO_CONNECTION
    # Not (): routing is a database question, and "not computed" must never be
    # renderable as "no rows go anywhere".
    assert report.routing is None


async def test_preview_says_it_needs_an_account_before_it_says_it_has_no_connection(monkeypatch):
    """Both reasons are true of a ref-less export previewed with no
    connection. The user can act on only one of them, so that is the one
    reported -- see _probe_skipped_reason."""

    async def boom(*_args, **_kwargs):
        raise AssertionError("preview must not open a database connection")

    monkeypatch.setattr(db.pool, "create_pool", boom)

    batch = ImportBatch(fills=(fill_with_ref(None),))
    report = await preview(batch, venue="fidelity", conn=None)
    assert report.needs_account is True
    assert report.duplicates_skipped_reason is DuplicateProbeSkipped.NEEDS_ACCOUNT


@pytest.mark.parametrize("reason", list(DuplicateProbeSkipped))
def test_every_skip_reason_is_a_plain_string_on_the_wire(reason):
    """A StrEnum so the API layer can serialise the reason unchanged rather
    than inventing a second vocabulary for it."""
    assert isinstance(reason, str)
