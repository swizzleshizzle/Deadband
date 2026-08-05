import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from db.accounts import create_account
from db.importing import commit_batch
from importers.base import CanonicalCash, CanonicalFill, ImportBatch
from importers.coinbase import CoinbaseImporter
from ledger.types import AssetClass, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db


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
    assert await conn.fetchval("SELECT count(*) FROM instrument") == 1


async def test_recommitting_the_same_batch_inserts_nothing(conn):
    """Re-importing an overlapping export must not duplicate history."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, batch_of(2), source="csv")
    result = await commit_batch(conn, acc, batch_of(2), source="csv")
    assert result.fills_inserted == 0
    assert result.fills_skipped == 2
    assert await conn.fetchval("SELECT count(*) FROM fill") == 2


async def test_overlapping_batch_inserts_only_the_new_rows(conn):
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, batch_of(2), source="csv")
    result = await commit_batch(conn, acc, batch_of(3), source="csv")
    assert result.fills_inserted == 1
    assert await conn.fetchval("SELECT count(*) FROM fill") == 3


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
    assert await conn.fetchval("SELECT count(*) FROM fill") == 3
    assert await conn.fetchval("SELECT count(*) FROM cash_movement") == 2


async def test_dividend_with_unique_symbol_match_attributes_instrument(conn):
    """A cash movement's symbol must resolve to the instrument the account already
    holds via a fill, when exactly one candidate exists. Fails if instrument_id is
    hardcoded to NULL (the brief's original bug) or if the match is done
    case-sensitively (fixture uses lowercase 'spy')."""
    acc = await create_account(conn, name="T", venue="fidelity", account_type="cash")
    await commit_batch(conn, acc, batch_of(1), source="csv")  # one SPY equity fill
    spy_id = await conn.fetchval("SELECT id FROM instrument WHERE symbol = 'SPY'")

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

    row = await conn.fetchrow("SELECT instrument_id, note FROM cash_movement")
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

    row = await conn.fetchrow("SELECT instrument_id, note FROM cash_movement")
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
    assert await conn.fetchval("SELECT count(*) FROM instrument") == 2

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

    row = await conn.fetchrow("SELECT instrument_id, note FROM cash_movement")
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
