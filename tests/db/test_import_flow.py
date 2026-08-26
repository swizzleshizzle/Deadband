"""The extracted import decision layer. These assert on RETURNED DATA, never on
printed output -- that separation is the whole point of the extraction, and it
is what lets the API reuse these decisions instead of restating them.

The connection-free guarantee is pinned in tests/test_import_flow.py, which
runs in the pure lane because a test claiming "this needs no database" must not
be skipped when the database is absent.

All values invented."""

import pytest

from db.accounts import create_account
from db.import_flow import (
    ImportCommitReport,
    UnknownRefsError,
    UnroutableRowsError,
    commit,
    preview,
)
from importers.base import ImportBatch
from tests.conftest import requires_db
from tests.import_flow_fixtures import fill_with_ref

pytestmark = requires_db


def _batch(*, ref: str | None = None, n: int = 1, refs_seen: tuple[str, ...] = ()) -> ImportBatch:
    """Modelled on tests/db/test_importing.py's batch_of, plus the external_ref
    that routing turns on."""
    return ImportBatch(
        fills=tuple(fill_with_ref(ref, day=i) for i in range(n)),
        refs_seen=refs_seen or ((ref,) if ref else ()),
    )


async def _fills_in(conn, account_id) -> int:
    return await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", account_id)


async def test_preview_reports_every_ref_seen_including_wholly_unmapped_accounts(conn):
    """refs_seen is a strict superset of the refs reachable from fills/cash. An
    account whose rows are ALL unmapped contributes nothing to either, and is
    exactly the account this report most needs to surface."""
    await create_account(
        conn, name="Known", venue="fidelity", account_type="cash", external_ref="ZREF1"
    )
    report = await preview(
        _batch(ref="ZREF1", refs_seen=("ZREF1", "ZGHOST")), venue="fidelity", conn=conn
    )
    assert "ZGHOST" in report.unknown_refs
    assert "ZREF1" not in report.unknown_refs


async def test_preview_maps_rows_to_the_same_accounts_commit_then_writes_them_to(conn):
    """The wizard's whole purpose is to show, BEFORE committing, which account
    each row lands in. If preview computed that mapping any way other than the
    one commit acts on, the screen a user approves and the write it authorises
    would be two independent answers to the same question, free to disagree."""
    acc = await create_account(
        conn, name="Mapped", venue="fidelity", account_type="cash", external_ref="ZREF3"
    )
    batch = _batch(ref="ZREF3", n=2)

    previewed = await preview(batch, venue="fidelity", conn=conn)
    assert previewed.routing is not None
    assert previewed.routing.mapped == ((acc, 2),)

    committed = await commit(
        conn, venue="fidelity", batch=batch, account_id=None, source="csv"
    )
    assert committed.routing.mapped == previewed.routing.mapped
    assert committed.fills_inserted == 2
    assert await _fills_in(conn, acc) == 2


async def test_commit_writes_and_regroups_and_reports_both(conn):
    acc = await create_account(
        conn, name="Flow", venue="fidelity", account_type="cash", external_ref="ZREF2"
    )
    report = await commit(
        conn, venue="fidelity", batch=_batch(ref="ZREF2"), account_id=None, source="csv"
    )
    assert isinstance(report, ImportCommitReport)
    assert report.fills_inserted == 1
    assert report.trades_regrouped >= 1
    assert await _fills_in(conn, acc) == 1


async def test_commit_refuses_unrouted_rows_when_no_account_is_given(conn):
    """A venue with no per-row account ref (the History dialect) has nothing to
    route on. Committing it without an explicit account would silently drop
    every row, so it must refuse instead."""
    with pytest.raises(UnroutableRowsError):
        await commit(
            conn, venue="fidelity", batch=_batch(ref=None), account_id=None, source="csv"
        )


async def test_commit_routes_everything_to_the_given_account_when_one_is_supplied(conn):
    acc = await create_account(conn, name="Whole", venue="fidelity", account_type="cash")
    report = await commit(
        conn, venue="fidelity", batch=_batch(ref=None), account_id=acc, source="csv"
    )
    assert report.fills_inserted == 1
    assert await _fills_in(conn, acc) == 1


# --- The two hard-won invariants below were pinned ONLY by tests/db/test_cli.py
# --- until this file existed -- i.e. only through the renderer. Asserted here
# --- on the returned dataclasses so a rewrite of that renderer cannot take
# --- their only guard with it.


async def test_an_ignored_account_routes_successfully_and_is_never_a_failure(conn):
    """ignore_on_import means "drop this account's rows ON PURPOSE". It routes
    SUCCESSFULLY: reported as ignored, never as unknown, and its sibling in the
    same batch commits normally. Treating it as a failure is what would make a
    deliberately-excluded account (a retirement plan with no instrument
    identity, say) refuse every import of the file it appears in, permanently.
    """
    active = await create_account(
        conn, name="Active", venue="fidelity", account_type="cash", external_ref="ZREF4"
    )
    ignored = await create_account(
        conn,
        name="Plan",
        venue="fidelity",
        account_type="cash",
        external_ref="ZREF5",
        ignore_on_import=True,
    )
    batch = ImportBatch(
        fills=(fill_with_ref("ZREF4"), fill_with_ref("ZREF5", day=1)),
        refs_seen=("ZREF4", "ZREF5"),
    )

    report = await commit(conn, venue="fidelity", batch=batch, account_id=None, source="csv")

    assert report.ignored_refs == ("ZREF5",)
    assert "ZREF5" not in report.routing.unknown_refs
    assert "ZREF5" not in report.routing.unclassified_refs
    assert report.routing.mapped == ((active, 1),)
    assert report.fills_inserted == 1
    assert await _fills_in(conn, active) == 1
    assert await _fills_in(conn, ignored) == 0


async def test_a_blocking_row_on_an_ignored_account_does_not_refuse_the_commit(conn):
    """C1: the blocking check runs AFTER routing and drops any reason whose ref
    belongs to an ignore_on_import account. Otherwise a money-carrying unmapped
    row on an account the user has explicitly said to skip refuses the ENTIRE
    import, permanently, with no escape."""
    active = await create_account(
        conn, name="Active2", venue="fidelity", account_type="cash", external_ref="ZREF6"
    )
    await create_account(
        conn,
        name="Plan2",
        venue="fidelity",
        account_type="cash",
        external_ref="ZREF7",
        ignore_on_import=True,
    )
    batch = ImportBatch(
        fills=(fill_with_ref("ZREF6"),),
        blocking=(("ZREF7", "ZZ UNRECOGNISED ACTION carrying 41.00"),),
        refs_seen=("ZREF6", "ZREF7"),
    )

    report = await commit(conn, venue="fidelity", batch=batch, account_id=None, source="csv")

    assert report.ignored_refs == ("ZREF7",)
    assert report.fills_inserted == 1
    assert await _fills_in(conn, active) == 1


async def test_only_a_money_carrying_unknown_ref_refuses_a_commit(conn):
    """unknown_refs (money-scoped) is the ONLY field allowed to drive a
    refusal. The reported superset must not: one stray boilerplate row
    attributed to an unregistered account would otherwise block every import of
    that file forever."""
    known = await create_account(
        conn, name="Known2", venue="fidelity", account_type="cash", external_ref="ZREF8"
    )

    # ZGHOST2 is seen in the raw rows and matches no account, but carries no
    # fill, cash movement or blocking reason -- reported, not refused.
    reported = await commit(
        conn,
        venue="fidelity",
        batch=_batch(ref="ZREF8", refs_seen=("ZREF8", "ZGHOST2")),
        account_id=None,
        source="csv",
    )
    assert "ZGHOST2" in reported.routing.unknown_refs
    assert reported.fills_inserted == 1
    assert await _fills_in(conn, known) == 1

    # The same unregistered ref, now carrying a fill: refused, and nothing more
    # is written -- not even the row that routed fine.
    with pytest.raises(UnknownRefsError) as raised:
        await commit(
            conn,
            venue="fidelity",
            batch=ImportBatch(
                fills=(fill_with_ref("ZREF8", day=5), fill_with_ref("ZMONEY", day=6)),
                refs_seen=("ZREF8", "ZMONEY"),
            ),
            account_id=None,
            source="csv",
        )
    assert raised.value.refs == ("ZMONEY",)
    assert "ZGHOST2" not in raised.value.refs
    assert await _fills_in(conn, known) == 1
