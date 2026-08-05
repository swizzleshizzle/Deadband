import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from db.accounts import create_account
from db.importing import commit_batch
from importers.base import CanonicalCash, CanonicalFill, ImportBatch
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
    """End-to-end through a real importer: coinbase fills carry no venue_fill_id,
    so this only dedupes if commit_batch synthesizes a content_hash for each one.
    Fails if that synthesis regresses — a second import would insert 3 more fills
    instead of 0."""
    acc = await create_account(conn, name="T", venue="coinbase", account_type="cash")
    text = pathlib.Path("tests/fixtures/coinbase/transactions.csv").read_text()
    batch = CoinbaseImporter().parse(text)

    first = await commit_batch(conn, acc, batch, source="csv")
    second = await commit_batch(conn, acc, batch, source="csv")

    assert first.fills_inserted == 3
    assert second.fills_inserted == 0
    assert second.fills_skipped == 3
    assert await _fill_count(conn, acc) == 3
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


async def test_coinbase_import_with_a_blank_quantity_row_commits_the_good_rows(conn):
    """Before the fix, Coinbase had no zero-quantity guard: the blank-quantity
    row became a CanonicalFill with quantity 0, which parsed and previewed
    fine, then raised ValueError out of Fill.__post_init__ inside commit_batch
    — and since commit_batch has no per-row try/except, that exception
    propagated and took the two good rows down with it (0 fills committed,
    not 2). Fails if the importer's parse() stops filtering the bad row: this
    test would then raise ValueError instead of asserting."""
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
    assert len(batch.fills) == 2
    assert len(batch.unmapped_rows) == 1

    acc = await create_account(conn, name="T", venue="coinbase", account_type="cash")
    result = await commit_batch(conn, acc, batch, source="csv")

    assert result.fills_inserted == 2
    assert await _fill_count(conn, acc) == 2


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
