import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from db.accounts import create_account
from db.importing import commit_batch, probe_duplicates, route_batch
from importers.base import CanonicalCash, CanonicalFill, CanonicalTransfer, ImportBatch
from importers.coinbase import CoinbaseImporter
from importers.fidelity import FidelityImporter
from ledger.types import AssetClass, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db

# The shared deadband_test database is not guaranteed empty when a test runs —
# other tests, or a prior manual CLI run, can leave rows (instrument in
# particular has no FK back to account, so nothing cascades it away). Every
# assertion below is therefore scoped to the account_id a test created, never a
# bare `SELECT count(*) FROM <table>` — a global count is only correct on an
# empty database and is not a safe thing to assert in a shared one.


async def _instrument_count_for_account(conn, account_id) -> int:
    return await conn.fetchval(
        "SELECT count(DISTINCT i.id) FROM instrument i "
        "JOIN fill f ON f.instrument_id = i.id WHERE f.account_id = $1",
        account_id,
    )


async def _fill_count(conn, account_id) -> int:
    return await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", account_id)


async def _cash_count(conn, account_id) -> int:
    return await conn.fetchval(
        "SELECT count(*) FROM cash_movement WHERE account_id = $1", account_id
    )


def batch_of(n: int) -> ImportBatch:
    return ImportBatch(
        fills=tuple(
            CanonicalFill(
                instrument=Instrument(
                    id=None,
                    asset_class=AssetClass.EQUITY,
                    symbol="SPY",
                    quote_currency="USD",
                ),
                executed_at=datetime(2026, 1, 15 + i, 14, 30, tzinfo=UTC),
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("500"),
                fee=Decimal("0"),
                fee_currency="USD",
            )
            for i in range(n)
        )
    )


async def test_commit_inserts_fills_and_creates_instruments(conn):
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    result = await commit_batch(conn, acc, batch_of(2), source="csv")
    assert result.fills_inserted == 2
    assert await _instrument_count_for_account(conn, acc) == 1


async def test_recommitting_the_same_batch_inserts_nothing(conn):
    """Re-importing an overlapping export must not duplicate history."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, batch_of(2), source="csv")
    result = await commit_batch(conn, acc, batch_of(2), source="csv")
    assert result.fills_inserted == 0
    assert result.fills_skipped == 2
    assert await _fill_count(conn, acc) == 2


async def test_overlapping_batch_inserts_only_the_new_rows(conn):
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, batch_of(2), source="csv")
    result = await commit_batch(conn, acc, batch_of(3), source="csv")
    assert result.fills_inserted == 1
    assert await _fill_count(conn, acc) == 3


async def test_reimporting_the_same_export_file_is_idempotent(conn):
    """§10 gap 6, closed 2026-08-08: this used to be an end-to-end proof,
    through the real importer, that re-importing a Coinbase export didn't
    double-count FILLS -- Coinbase fills carry no venue_fill_id, so that
    only worked if commit_batch synthesized a content_hash for each one.
    Coinbase fills come only from the API now; this CSV path parses to
    zero of them, so there is nothing left for that synthesis to dedupe.

    What survives is the guarantee the test name actually promises:
    re-importing the same file must not double-count CASH either. Cash
    dedupes on its own content_hash (see commit_batch's `ON CONFLICT DO
    NOTHING` on cash_movement) -- a mechanism this test never exercised
    before because the Coinbase fixture's fills always dominated the
    assertions. Fails if that cash dedup regresses: a second import would
    insert 2 more cash rows instead of 0."""
    acc = await create_account(conn, name="T", venue="coinbase", account_type="cash")
    text = pathlib.Path("tests/fixtures/coinbase/transactions.csv").read_text()
    batch = CoinbaseImporter().parse(text)
    assert batch.fills == ()  # gap 6: nothing left here to dedupe by content_hash

    first = await commit_batch(conn, acc, batch, source="csv")
    second = await commit_batch(conn, acc, batch, source="csv")

    assert first.fills_inserted == 0
    assert first.cash_inserted == 2
    assert second.fills_inserted == 0
    assert second.fills_skipped == 0
    assert second.cash_inserted == 0
    assert await _fill_count(conn, acc) == 0
    assert await _cash_count(conn, acc) == 2


async def test_dividend_with_unique_symbol_match_attributes_instrument(conn):
    """A cash movement's symbol must resolve to the instrument the account already
    holds via a fill, when exactly one candidate exists. Fails if instrument_id is
    hardcoded to NULL (the brief's original bug) or if the match is done
    case-sensitively (fixture uses lowercase 'spy')."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, batch_of(1), source="csv")  # one SPY equity fill
    # Scoped to this account's own fill, not a bare symbol lookup: `instrument`
    # is global (no account_id column), and on a shared, non-empty database
    # another account's SPY (or a crypto SPY, different natural_key) could also
    # match a bare "WHERE symbol = 'SPY'".
    spy_id = await conn.fetchval(
        "SELECT instrument_id FROM fill WHERE account_id = $1 LIMIT 1", acc
    )

    dividend = ImportBatch(
        cash=(
            CanonicalCash(
                occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
                kind="dividend",
                amount=Decimal("42.15"),
                currency="USD",
                symbol="spy",
            ),
        )
    )
    result = await commit_batch(conn, acc, dividend, source="csv")
    assert result.cash_inserted == 1

    row = await conn.fetchrow(
        "SELECT instrument_id, note FROM cash_movement WHERE account_id = $1", acc
    )
    assert row["instrument_id"] == spy_id
    assert row["note"] is None


async def test_dividend_with_no_symbol_match_preserves_symbol_in_note(conn):
    """No instrument named QQQ has ever traded in this account, so instrument_id
    must stay NULL — but the symbol must not be silently lost. Fails if the note
    is left unchanged (dropping the attribution) or if instrument_id is
    incorrectly set."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")

    dividend = ImportBatch(
        cash=(
            CanonicalCash(
                occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
                kind="dividend",
                amount=Decimal("10"),
                currency="USD",
                symbol="QQQ",
                note="dividend received",
            ),
        )
    )
    result = await commit_batch(conn, acc, dividend, source="csv")
    assert result.cash_inserted == 1

    row = await conn.fetchrow(
        "SELECT instrument_id, note FROM cash_movement WHERE account_id = $1", acc
    )
    assert row["instrument_id"] is None
    assert row["note"] == "dividend received [symbol=QQQ]"


async def test_dividend_with_ambiguous_symbol_match_preserves_symbol_in_note(conn):
    """Two different instruments (an equity and a crypto asset) can share the same
    symbol text. With two candidates, instrument_id must stay NULL rather than
    guessing — fails if the code picks the first match instead of checking for
    exactly one."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    equity = CanonicalFill(
        instrument=Instrument(
            id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"
        ),
        executed_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("500"),
        fee=Decimal("0"),
        fee_currency="USD",
    )
    crypto = CanonicalFill(
        instrument=Instrument(
            id=None, asset_class=AssetClass.CRYPTO_SPOT, symbol="SPY", quote_currency="USD"
        ),
        executed_at=datetime(2026, 1, 16, 14, 30, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("1"),
        fee=Decimal("0"),
        fee_currency="USD",
    )
    await commit_batch(conn, acc, ImportBatch(fills=(equity, crypto)), source="csv")
    assert await _instrument_count_for_account(conn, acc) == 2

    dividend = ImportBatch(
        cash=(
            CanonicalCash(
                occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
                kind="dividend",
                amount=Decimal("5"),
                currency="USD",
                symbol="SPY",
            ),
        )
    )
    result = await commit_batch(conn, acc, dividend, source="csv")
    assert result.cash_inserted == 1

    row = await conn.fetchrow(
        "SELECT instrument_id, note FROM cash_movement WHERE account_id = $1", acc
    )
    assert row["instrument_id"] is None
    assert row["note"] == "symbol=SPY"


async def test_a_failure_between_commit_and_regroup_leaves_no_fills(conn):
    """cmd_import wraps commit_batch and regroup_account in one transaction
    precisely so a crash between them (fills inserted, trades never regrouped)
    can't happen. Reproduced here by raising right after commit_batch inside
    that same pattern. Fails if commit_batch does anything that defeats the
    caller's rollback — e.g. issuing its own explicit COMMIT — in which case
    the fills would survive the exception instead of vanishing with it."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")

    class _SimulatedCrash(Exception):
        pass

    try:
        async with conn.transaction():
            await commit_batch(conn, acc, batch_of(2), source="csv")
            raise _SimulatedCrash
    except _SimulatedCrash:
        pass

    assert await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", acc) == 0


# --- Fix round 1, item 1: genuine same-day repeats must not be deduped away ---
#
# Fidelity's "Run Date" carries no time component. Two real, distinct trades on
# the same day with identical symbol/side/quantity/price therefore hash
# identically unless commit_batch breaks the tie with an occurrence index — and
# a hash collision here means one of the two fills is silently discarded as a
# "duplicate" by insert_fills' ON CONFLICT, which is real data loss, not a
# benign re-import skip.

_TWO_IDENTICAL_FIDELITY_ROWS = (
    "Run Date,Account,Action,Symbol,Description,Quantity,Price,Commission,Fees,Amount\n"
    "01/15/2026,X1,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00\n"
    "01/15/2026,X1,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00\n"
)

_THREE_IDENTICAL_FIDELITY_ROWS = (
    "Run Date,Account,Action,Symbol,Description,Quantity,Price,Commission,Fees,Amount\n"
    "01/15/2026,X1,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00\n"
    "01/15/2026,X1,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00\n"
    "01/15/2026,X1,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00\n"
)


async def test_two_identical_same_day_rows_both_commit(conn):
    """Fails if content_hash collapses same-shape same-day rows onto one hash:
    fills_inserted would be 1 (one fill silently dropped) instead of 2, and the
    scoped fill count would be 1 instead of 2."""
    batch = FidelityImporter().parse(_TWO_IDENTICAL_FIDELITY_ROWS)
    assert len(batch.fills) == 2

    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    result = await commit_batch(conn, acc, batch, source="csv")

    assert result.fills_inserted == 2
    assert await _fill_count(conn, acc) == 2


async def test_recommitting_two_identical_same_day_rows_inserts_nothing(conn):
    """The occurrence index must be stable across re-imports of the same file:
    walking the same two rows in the same order a second time must assign the
    same two occurrence indices (0, 1) and therefore the same two hashes,
    deduping to zero. Fails (inserts 2 again) if occurrence assignment isn't
    deterministic per call, or regresses to 0 for every row."""
    batch = FidelityImporter().parse(_TWO_IDENTICAL_FIDELITY_ROWS)
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")

    await commit_batch(conn, acc, batch, source="csv")
    result = await commit_batch(conn, acc, batch, source="csv")

    assert result.fills_inserted == 0
    assert result.fills_skipped == 2
    assert await _fill_count(conn, acc) == 2


async def test_three_identical_rows_after_two_already_committed_inserts_one(conn):
    """A later export with one more repeat of an already-imported same-day row
    must add exactly the new occurrence, not zero (which would mean the third
    genuine trade is lost) and not three (which would mean the first two were
    re-inserted as duplicates)."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    two = FidelityImporter().parse(_TWO_IDENTICAL_FIDELITY_ROWS)
    three = FidelityImporter().parse(_THREE_IDENTICAL_FIDELITY_ROWS)

    await commit_batch(conn, acc, two, source="csv")
    result = await commit_batch(conn, acc, three, source="csv")

    assert result.fills_inserted == 1
    assert result.fills_skipped == 2
    assert await _fill_count(conn, acc) == 3


# --- Fix round 2 -------------------------------------------------------------


async def test_cash_occurrence_counter_is_independent_of_the_fill_counter(conn):
    """The cash and fill occurrence counters must never share state. A mutant
    that seeds (or shifts) the cash counter using how many fills were already
    processed in the same call would compute occurrence 1 for the dividend in
    a 1-fill batch but occurrence 2 for the SAME dividend in a 2-fill batch —
    different hashes, so the second commit would insert a phantom duplicate
    cash_movement row instead of recognizing it as already present. Fails
    (cash_inserted == 1, cash row count == 2 on the second commit) if the
    counters are coupled."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")

    dividend = CanonicalCash(
        occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
        kind="dividend",
        amount=Decimal("10"),
        currency="USD",
    )

    one_fill_batch = ImportBatch(fills=(batch_of(1).fills[0],), cash=(dividend,))
    two_fill_batch = ImportBatch(fills=batch_of(2).fills, cash=(dividend,))

    first = await commit_batch(conn, acc, one_fill_batch, source="csv")
    second = await commit_batch(conn, acc, two_fill_batch, source="csv")

    assert first.cash_inserted == 1
    assert second.cash_inserted == 0
    assert await _cash_count(conn, acc) == 1


async def test_occurrence_key_normalizes_symbol_case_like_content_hash(conn):
    """The occurrence key must use the same normalization content_hash applies
    internally (symbol upper-cased, side lower-cased) or two rows differing
    only in symbol casing get different occurrence keys (each starting fresh
    at occurrence 0) while content_hash's own internal upper-casing makes their
    final hashes identical anyway — colliding the two rows onto one hash and
    silently dropping one. Fails (fills_inserted == 1, one row in the table)
    if the occurrence key is built from the raw, un-normalized symbol."""
    same_shape = dict(
        executed_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("500"),
        fee=Decimal("0"),
        fee_currency="USD",
    )
    upper = CanonicalFill(
        instrument=Instrument(
            id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"
        ),
        **same_shape,
    )
    mixed_case = CanonicalFill(
        instrument=Instrument(
            id=None, asset_class=AssetClass.EQUITY, symbol="Spy", quote_currency="USD"
        ),
        **same_shape,
    )

    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    result = await commit_batch(conn, acc, ImportBatch(fills=(upper, mixed_case)), source="csv")

    assert result.fills_inserted == 2
    assert await _fill_count(conn, acc) == 2


# --- Final fix wave, item 1: a blank-quantity Coinbase row must not sink the
# --- whole import — the two good rows around it must still commit. ----------


async def test_coinbase_import_with_a_blank_quantity_row_commits_without_crashing(conn):
    """Before the zero-quantity guard existed, this blank-quantity row became
    a CanonicalFill with quantity 0, which parsed and previewed fine, then
    raised ValueError out of Fill.__post_init__ inside commit_batch — and
    since commit_batch has no per-row try/except, that exception propagated
    and took the two good rows down with it (0 fills committed, not 2).

    §10 gap 6, closed 2026-08-08: that whole failure mode is gone now, not
    patched further — no Buy/Sell row, blank quantity or garbled or clean,
    ever becomes a CanonicalFill anymore, so there is nothing left for
    commit_batch to crash on. Fails if the importer's parse() ever starts
    building a fill for a trade row again: this test would then either see
    a non-empty batch.fills or raise ValueError out of commit_batch instead
    of asserting."""
    header = (
        "Timestamp,Transaction Type,Asset,Quantity Transacted,Spot Price Currency,"
        "Spot Price at Transaction,Subtotal,Total (inclusive of fees and/or spread),"
        "Fees and/or Spread,Notes"
    )
    rows = "\n".join(
        [
            header,
            "2026-01-15T14:30:00Z,Buy,BTC,0.50000000,USD,61200.00,30600.00,30753.00,153.00,ok",
            "2026-01-16T09:05:00Z,Buy,BTC,,USD,60800.00,0,0,0,blank quantity",
            "2026-02-03T11:20:00Z,Sell,BTC,0.25000000,USD,68000.00,17000.00,16915.00,85.00,ok",
        ]
    )
    batch = CoinbaseImporter().parse(rows + "\n")
    assert batch.fills == ()
    assert batch.unmapped_rows == ()

    acc = await create_account(conn, name="T", venue="coinbase", account_type="cash")
    result = await commit_batch(conn, acc, batch, source="csv")

    assert result.fills_inserted == 0
    assert await _fill_count(conn, acc) == 0


# --- Blocker pass, item 3: Coinbase guarded quantity == 0 but not a negative
# --- Quantity Transacted — unlike Fidelity, Coinbase never abs()'s the parsed
# --- quantity, so a negative value survived parse() and preview, then raised
# --- ValueError out of Fill.__post_init__ inside commit_batch, taking the two
# --- good rows down with it (0 fills committed, not 2), same failure mode as
# --- item 1's blank-quantity row. ---------------------------------------------


async def test_coinbase_import_with_a_negative_quantity_row_commits_without_crashing(conn):
    """Before the negative-quantity guard existed (`quantity <= 0`, not
    `quantity == 0` — Coinbase, unlike Fidelity, never abs()'s the parsed
    quantity), this row survived parse() and preview, then raised
    ValueError out of Fill.__post_init__ inside commit_batch, taking the
    two good rows down with it.

    §10 gap 6, closed 2026-08-08: same reasoning as the blank-quantity
    twin above — no Buy/Sell row builds a CanonicalFill anymore, negative
    quantity or not, so there is nothing left to crash. Fails if the
    importer's parse() ever starts building a fill for a trade row again."""
    header = (
        "Timestamp,Transaction Type,Asset,Quantity Transacted,Spot Price Currency,"
        "Spot Price at Transaction,Subtotal,Total (inclusive of fees and/or spread),"
        "Fees and/or Spread,Notes"
    )
    rows = "\n".join(
        [
            header,
            "2026-01-15T14:30:00Z,Buy,BTC,0.50000000,USD,61200.00,30600.00,30753.00,153.00,ok",
            "2026-01-16T09:05:00Z,Buy,BTC,-0.30000000,USD,60800.00,0,0,0,negative quantity",
            "2026-02-03T11:20:00Z,Sell,BTC,0.25000000,USD,68000.00,17000.00,16915.00,85.00,ok",
        ]
    )
    batch = CoinbaseImporter().parse(rows + "\n")
    assert batch.fills == ()
    assert batch.unmapped_rows == ()

    acc = await create_account(conn, name="T", venue="coinbase", account_type="cash")
    result = await commit_batch(conn, acc, batch, source="csv")

    assert result.fills_inserted == 0
    assert await _fill_count(conn, acc) == 0


# --- Task 1: funding_source round-trips through the database ---------------


def _fill(*, symbol: str, funding_source: str | None = None) -> CanonicalFill:
    """Local helper mirroring the shape batch_of()/the other tests build inline
    (equity Instrument + a fixed executed_at/side/qty/price shape), with
    funding_source only passed through when the caller wants a non-default
    value — so omitting it exercises CanonicalFill's own default."""
    kwargs = dict(
        instrument=Instrument(
            id=None, asset_class=AssetClass.EQUITY, symbol=symbol, quote_currency="USD"
        ),
        executed_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("10"),
        fee=Decimal("0"),
        fee_currency="USD",
    )
    if funding_source is not None:
        kwargs["funding_source"] = funding_source
    return CanonicalFill(**kwargs)


async def test_funding_source_round_trips_through_commit(conn):
    """A reinvestment-funded fill must persist as such. Without this the column
    exists but nothing can ever set it, and contributed_capital cannot be
    distinguished from cost basis."""
    account_id = await create_account(
        conn, name="t", venue="coinbase", account_type="cash"
    )
    batch = ImportBatch(
        fills=(
            _fill(symbol="AAA", funding_source="reinvestment"),
            _fill(symbol="BBB"),  # defaults to external
        )
    )
    await commit_batch(conn, account_id, batch, source="csv")

    rows = await conn.fetch(
        """SELECT i.symbol, f.funding_source
             FROM fill f JOIN instrument i ON i.id = f.instrument_id
            WHERE f.account_id = $1 ORDER BY i.symbol""",
        account_id,
    )
    assert [(r["symbol"], r["funding_source"]) for r in rows] == [
        ("AAA", "reinvestment"),
        ("BBB", "external"),
    ]


async def test_reordering_a_mixed_venue_fill_id_batch_stays_idempotent(conn):
    """A row carrying its own venue_fill_id dedupes on that id alone and must
    not also consume an occurrence slot in the shared counter — if it did, a
    hash-carrying row sharing its shape would get a different occurrence index
    (and therefore a different hash) depending on where the id-carrying row
    sits relative to it. Reproduced with the minimal case: one id-carrying and
    one hash-carrying fill, identical in every other respect, re-imported in
    the opposite order. Fails (second commit inserts 1, three rows exist
    instead of two) if the occurrence counter increments for every row instead
    of only the ones that actually consume a slot."""
    instrument = Instrument(
        id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"
    )
    same_shape = dict(
        executed_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("500"),
        fee=Decimal("0"),
        fee_currency="USD",
    )
    with_id = CanonicalFill(instrument=instrument, venue_fill_id="v1", **same_shape)
    without_id = CanonicalFill(instrument=instrument, **same_shape)

    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")

    first = await commit_batch(conn, acc, ImportBatch(fills=(with_id, without_id)), source="csv")
    assert first.fills_inserted == 2

    # Same two rows, reordered — a genuine re-import can arrive in any order;
    # nothing in ImportBatch promises row order is preserved across exports.
    second = await commit_batch(conn, acc, ImportBatch(fills=(without_id, with_id)), source="csv")

    assert second.fills_inserted == 0
    assert second.fills_skipped == 2
    assert await _fill_count(conn, acc) == 2


# --- Task 4: route_batch splits a parsed batch by account -------------------
#
# One export can span several accounts. Routing matches each row's
# external_ref (the venue's own account NUMBER, never the nickname -- see
# tests/test_fidelity.py) against account.external_ref within the venue.


def _batch_spanning(*refs: str) -> ImportBatch:
    """One fill per ref, all otherwise identical -- only external_ref varies,
    since that's the only thing route_batch looks at."""
    return ImportBatch(
        fills=tuple(
            CanonicalFill(
                instrument=Instrument(
                    id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"
                ),
                executed_at=datetime(2026, 1, 15, tzinfo=UTC),
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("500"),
                fee=Decimal("0"),
                fee_currency="USD",
                external_ref=ref,
            )
            for ref in refs
        )
    )


async def test_routing_splits_a_batch_by_account(conn):
    a1 = await create_account(
        conn, name="one", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    a2 = await create_account(
        conn, name="two", venue="fidelity", account_type="cash", external_ref="A0000002"
    )
    plan = await route_batch(conn, "fidelity", _batch_spanning("A0000001", "A0000002"))
    assert set(plan.by_account) == {a1, a2}
    assert plan.unknown_refs == ()


async def test_an_unknown_account_ref_is_reported_not_merged(conn):
    await create_account(
        conn, name="one", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    plan = await route_batch(conn, "fidelity", _batch_spanning("A0000001", "A0000009"))
    assert plan.unknown_refs == ("A0000009",)


async def test_an_ignored_account_routes_successfully_and_is_skipped(conn):
    await create_account(
        conn,
        name="plan",
        venue="fidelity",
        account_type="cash",
        external_ref="A0000003",
        ignore_on_import=True,
    )
    plan = await route_batch(conn, "fidelity", _batch_spanning("A0000003"))
    assert plan.ignored_refs == ("A0000003",)
    assert plan.by_account == {}
    assert plan.unknown_refs == ()  # ignored is NOT unknown


# --- Finding F: refs_seen-only refs (all-unmapped, non-financial rows) were -
# --- invisible to route_batch's classification entirely. --------------------
#
# route_batch built its ref set from fills, cash, and blocking only. An
# account contributing NOTHING but unmapped, non-financial rows (no fill, no
# cash, no blocking reason -- refs_seen is the ONLY place such an account is
# visible at all, see importers/fidelity.py's own refs_seen test) therefore
# never reached this function's account lookup and could never be classified
# as unknown or ignored. The external reviewer proposed making this REFUSE
# the commit -- rejected: that reintroduces the over-block trap A2-6 exists
# to avoid, where one stray boilerplate row attributed to an unregistered
# account refuses every import permanently. The fix instead completes the
# CLASSIFICATION (so reporting can say mapped/ignored/unknown) without
# extending the REFUSAL, which stays keyed on money (fills/cash/blocking).


def _batch_with_refs_seen_only(*refs: str) -> ImportBatch:
    """No fills, no cash, no blocking -- refs_seen is the only place these
    refs appear, exactly the "every row on this account failed to classify,
    and none of them carried money" shape."""
    return ImportBatch(refs_seen=tuple(refs))


async def test_an_unregistered_refs_seen_only_account_is_classified_unknown_not_invisible(conn):
    """No account is registered for A0000099 at all. Before the fix this ref
    never reached the account lookup (fills/cash/blocking are all empty), so
    it was neither unknown_refs nor ignored_refs nor routable -- invisible to
    every classification. It must now show up as reported-unknown, and
    critically must NOT show up in unknown_refs (which drives cli.py's
    refusal) since it carries no money."""
    plan = await route_batch(conn, "fidelity", _batch_with_refs_seen_only("A0000099"))
    assert plan.by_account == {}
    assert plan.unknown_refs == (), (
        "a refs_seen-only ref must never drive refusal -- that's the over-block trap"
    )
    assert "A0000099" in plan.reported_unknown_refs


async def test_a_registered_ignored_refs_seen_only_account_is_classified_ignored(conn):
    """The mirror case: the account IS registered, and IS ignore_on_import,
    but contributed only non-financial unmapped rows. Must classify as
    ignored (for reporting), not as unknown -- same as an ignored account
    that does contribute fills/cash."""
    await create_account(
        conn,
        name="plan",
        venue="fidelity",
        account_type="cash",
        external_ref="A0000003",
        ignore_on_import=True,
    )
    plan = await route_batch(conn, "fidelity", _batch_with_refs_seen_only("A0000003"))
    assert plan.by_account == {}
    assert plan.unknown_refs == ()
    assert "A0000003" in plan.ignored_refs
    assert "A0000003" not in plan.reported_unknown_refs


async def test_a_money_carrying_unknown_ref_still_drives_refusal_even_when_also_in_refs_seen(conn):
    """The other direction, pinned at the route_batch level: a ref that DOES
    carry money (via a blocking reason) and has no matching account must
    still land in unknown_refs -- refusal is unaffected by this fix."""
    batch = ImportBatch(
        blocking=(("A0000099", "line 2: unhandled action 'X'"),),
        refs_seen=("A0000099",),
    )
    plan = await route_batch(conn, "fidelity", batch)
    assert plan.unknown_refs == ("A0000099",)
    assert "A0000099" in plan.reported_unknown_refs


async def test_a_null_external_ref_account_is_never_a_wildcard(conn):
    """UNIQUE (venue, external_ref) does not constrain NULLs, so several accounts
    may have none. Treating NULL as a match would make the first such account a
    silent catch-all for every unroutable row."""
    await create_account(conn, name="no-ref", venue="fidelity", account_type="cash")
    plan = await route_batch(conn, "fidelity", _batch_spanning("A0000009"))
    assert plan.by_account == {}
    assert plan.unknown_refs == ("A0000009",)


async def test_a_row_with_no_external_ref_is_never_routed(conn):
    """A row whose external_ref is None (e.g. a venue with no per-row account
    identifier) must never be routed to any account -- not even one that also
    has no external_ref. Distinguishes routing on a NULL row-side ref from
    routing on a NULL account-side ref (the sibling test above)."""
    await create_account(conn, name="no-ref", venue="fidelity", account_type="cash")
    batch = ImportBatch(
        fills=(
            CanonicalFill(
                instrument=Instrument(
                    id=None, asset_class=AssetClass.EQUITY, symbol="SPY", quote_currency="USD"
                ),
                executed_at=datetime(2026, 1, 15, tzinfo=UTC),
                side=Side.BUY,
                quantity=Decimal("1"),
                price=Decimal("500"),
                fee=Decimal("0"),
                fee_currency="USD",
                external_ref=None,
            ),
        )
    )
    plan = await route_batch(conn, "fidelity", batch)
    assert plan.by_account == {}
    assert plan.unknown_refs == ()
    assert plan.ignored_refs == ()


async def test_routing_splits_cash_movements_too(conn):
    """route_batch must partition cash, not only fills -- a cash-only batch
    (e.g. a dividend-only export) must still route correctly."""
    a1 = await create_account(
        conn, name="one", venue="fidelity", account_type="cash", external_ref="A0000001"
    )
    a2 = await create_account(
        conn, name="two", venue="fidelity", account_type="cash", external_ref="A0000002"
    )
    batch = ImportBatch(
        cash=(
            CanonicalCash(
                occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
                kind="dividend",
                amount=Decimal("5"),
                currency="USD",
                external_ref="A0000001",
            ),
            CanonicalCash(
                occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
                kind="dividend",
                amount=Decimal("7"),
                currency="USD",
                external_ref="A0000002",
            ),
        )
    )
    plan = await route_batch(conn, "fidelity", batch)
    assert set(plan.by_account) == {a1, a2}
    assert len(plan.by_account[a1].cash) == 1
    assert len(plan.by_account[a2].cash) == 1


# --- Task 6: preview duplicate probe -----------------------------------------
#
# Preview deliberately never opens a database connection (see
# tests/test_cli.py's test_preview_import_never_opens_a_database_connection).
# probe_duplicates is the explicit, opt-in exception -- read-only, wired
# behind --check-duplicates in cli.py, and never called by default preview.


def _batch_of_two_fills() -> ImportBatch:
    return batch_of(2)


async def test_probe_reports_duplicates_without_writing(conn):
    """Task 6, brief Step 1. before == after proves the probe wrote nothing;
    fill_dupes == 2 proves it recognizes both already-committed fills."""
    account_id = await create_account(conn, name="t", venue="fidelity", account_type="cash")
    batch = _batch_of_two_fills()
    await commit_batch(conn, account_id, batch, source="csv")

    before = await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", account_id)
    report = await probe_duplicates(conn, account_id, batch)
    after = await conn.fetchval("SELECT count(*) FROM fill WHERE account_id = $1", account_id)

    assert report.fill_dupes == 2
    assert before == after == 2


async def test_probe_reports_zero_for_a_batch_not_yet_committed(conn):
    """A fresh batch against an empty account has nothing to find yet -- the
    probe must not report phantom duplicates."""
    account_id = await create_account(conn, name="t", venue="fidelity", account_type="cash")
    report = await probe_duplicates(conn, account_id, batch_of(2))
    assert report.fill_dupes == 0
    assert report.cash_dupes == 0


async def test_probe_agrees_with_a_subsequent_commit_on_partial_overlap(conn):
    """The probe and commit_batch must never disagree: 2 of 3 fills already
    committed means the probe reports 2 dupes, and a subsequent commit of the
    same 3-fill batch inserts exactly the 1 new one -- proving they share the
    same dedupe keys rather than two independently-maintained hashing schemes
    that could drift apart."""
    account_id = await create_account(conn, name="t", venue="fidelity", account_type="cash")
    await commit_batch(conn, account_id, batch_of(2), source="csv")

    three = batch_of(3)
    report = await probe_duplicates(conn, account_id, three)
    assert report.fill_dupes == 2

    result = await commit_batch(conn, account_id, three, source="csv")
    assert result.fills_inserted == 1
    assert result.fills_skipped == 2


async def test_probe_reports_duplicates_by_venue_fill_id_too(conn):
    """Not every fill dedupes on content_hash -- a row carrying its own
    venue_fill_id dedupes on (account_id, venue_fill_id) instead (see
    commit_batch). The probe must recognize that path too, not just
    content_hash."""
    account_id = await create_account(conn, name="t", venue="coinbase", account_type="wallet")
    fill = CanonicalFill(
        instrument=Instrument(
            id=None, asset_class=AssetClass.CRYPTO_SPOT, symbol="BTC", quote_currency="USD"
        ),
        executed_at=datetime(2026, 1, 15, 14, 30, tzinfo=UTC),
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("60000"),
        fee=Decimal("0"),
        fee_currency="USD",
        venue_fill_id="v-1",
    )
    batch = ImportBatch(fills=(fill,))
    await commit_batch(conn, account_id, batch, source="csv")

    report = await probe_duplicates(conn, account_id, batch)
    assert report.fill_dupes == 1


async def test_probe_reports_cash_duplicates_without_writing(conn):
    """Same guarantee as the fills test above, for cash movements: the probe
    must recognize an already-committed dividend as a duplicate and must not
    write a new one."""
    account_id = await create_account(conn, name="t", venue="fidelity", account_type="cash")
    dividend = ImportBatch(
        cash=(
            CanonicalCash(
                occurred_at=datetime(2026, 4, 1, tzinfo=UTC),
                kind="dividend",
                amount=Decimal("10"),
                currency="USD",
            ),
        )
    )
    await commit_batch(conn, account_id, dividend, source="csv")

    before = await conn.fetchval(
        "SELECT count(*) FROM cash_movement WHERE account_id = $1", account_id
    )
    report = await probe_duplicates(conn, account_id, dividend)
    after = await conn.fetchval(
        "SELECT count(*) FROM cash_movement WHERE account_id = $1", account_id
    )

    assert report.cash_dupes == 1
    assert report.fill_dupes == 0
    assert before == after == 1


async def test_routing_does_not_cross_venues(conn):
    """An account with a matching external_ref at a DIFFERENT venue must not
    be matched -- routing is scoped to (venue, external_ref), same as the
    UNIQUE constraint."""
    await create_account(
        conn, name="other-venue", venue="coinbase", account_type="wallet", external_ref="A0000001"
    )
    plan = await route_batch(conn, "fidelity", _batch_spanning("A0000001"))
    assert plan.by_account == {}
    assert plan.unknown_refs == ("A0000001",)


# --- I5: the probe/commit agreement tests could not fail against the drift --
# --- they exist to catch. All five probe tests above use batch_of(n), whose
# --- fills fall on DIFFERENT days -- occurrence is 0 for every row, so the
# --- one non-obvious part of _fill_dedupe_keys (the occurrence index, the
# --- entire reason it was extracted as shared code) is never exercised by
# --- any of them. _TWO_IDENTICAL_FIDELITY_ROWS (same day, same shape) is
# --- what actually distinguishes a probe that shares commit_batch's dedupe
# --- keys from one that has quietly drifted.


async def test_probe_agrees_with_commit_on_same_day_duplicate_rows(conn):
    """Commits _TWO_IDENTICAL_FIDELITY_ROWS (same day, same symbol/side/qty/
    price -- occurrence 0 and 1) and then probes the SAME batch again. Fails
    if probe_duplicates ever stops sharing _fill_dedupe_keys with
    commit_batch (e.g. an inline content_hash call that drops the occurrence
    argument): such a drift collapses both rows onto the SAME hash, so the
    probe would report only 1 duplicate while commit_batch (which does use
    occurrence) would still correctly skip both on a re-commit -- the probe
    silently disagreeing with the commit it exists to preview."""
    batch = FidelityImporter().parse(_TWO_IDENTICAL_FIDELITY_ROWS)
    account_id = await create_account(conn, name="t", venue="fidelity", account_type="cash")
    await commit_batch(conn, account_id, batch, source="csv")

    report = await probe_duplicates(conn, account_id, batch)
    assert report.fill_dupes == 2


# --- transfers (branch B): committed directly with content-hash dedupe.


def _transfer_batch() -> ImportBatch:
    return ImportBatch(
        transfers=(
            CanonicalTransfer(
                instrument=Instrument(
                    id=None,
                    asset_class=AssetClass.EQUITY,
                    symbol="ZXCO",
                    quote_currency="USD",
                ),
                occurred_at=datetime(2026, 3, 11, tzinfo=UTC),
                quantity=Decimal("40"),
                market_value=Decimal("259.20"),
            ),
        )
    )


async def test_commit_batch_writes_transfers_and_dedupes_on_reimport(conn):
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    first = await commit_batch(conn, acc, _transfer_batch(), source="csv")
    assert (first.transfers_inserted, first.transfers_skipped) == (1, 0)
    second = await commit_batch(conn, acc, _transfer_batch(), source="csv")
    assert (second.transfers_inserted, second.transfers_skipped) == (0, 1)
    rows = await conn.fetch(
        "SELECT t.quantity, i.symbol FROM asset_transfer t"
        " JOIN instrument i ON i.id = t.instrument_id WHERE t.account_id = $1",
        acc,
    )
    assert len(rows) == 1
    assert rows[0]["quantity"] == Decimal("40")
    assert rows[0]["symbol"] == "ZXCO"


async def test_two_identical_same_day_transfers_in_one_batch_both_insert(conn):
    """The occurrence-index tie-break, same contract as fills and cash: two
    genuinely distinct same-day identical transfers must not collapse."""
    acc = await create_account(conn, name="T2", venue="fidelity", account_type="cash")
    single = _transfer_batch().transfers[0]
    batch = ImportBatch(transfers=(single, single))
    result = await commit_batch(conn, acc, batch, source="csv")
    assert (result.transfers_inserted, result.transfers_skipped) == (2, 0)


async def test_restated_market_value_still_dedupes(conn):
    """market_value is the broker's informational stamp, not identity: the
    same transfer event restated with a different (or blank) Amount across
    two exports must dedupe, or the re-import inserts a phantom second
    transfer and regroup refuses the file forever as an over-transfer."""
    acc = await create_account(conn, name="T3", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, _transfer_batch(), source="csv")
    restated = ImportBatch(
        transfers=(
            CanonicalTransfer(
                instrument=Instrument(
                    id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"
                ),
                occurred_at=datetime(2026, 3, 11, tzinfo=UTC),
                quantity=Decimal("40"),
                market_value=None,
            ),
        )
    )
    result = await commit_batch(conn, acc, restated, source="csv")
    assert (result.transfers_inserted, result.transfers_skipped) == (0, 1)


async def test_probe_counts_committed_transfers(conn):
    """probe_duplicates' own contract: it can never report a row as new that
    commit_batch would then silently skip as a duplicate -- transfers
    included, or the preview and the commit disagree."""
    acc = await create_account(conn, name="T4", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, _transfer_batch(), source="csv")
    report = await probe_duplicates(conn, acc, _transfer_batch())
    assert report.transfer_dupes == 1
