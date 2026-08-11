from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
import pytest_asyncio

from db.accounts import create_account
from db.cash import MixedCurrencyError, account_cash
from db.fills import insert_fills
from db.importing import commit_batch
from db.instruments import upsert_instrument
from importers.base import CanonicalCash, ImportBatch
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from tests.conftest import requires_db

pytestmark = requires_db

T0 = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)


def _fill(acc, inst, *, side, quantity, price, ref, fee="0", fee_currency="USD"):
    return Fill(
        id=uuid4(),
        account_id=acc,
        instrument_id=inst,
        executed_at=T0,
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_currency=fee_currency,
        source=FillSource.MANUAL,
        venue_fill_id=ref,
        is_estimated=False,
    )


async def _deposit(conn, acc, *, amount, currency="USD", kind="deposit"):
    await commit_batch(
        conn,
        acc,
        ImportBatch(
            cash=(
                CanonicalCash(
                    occurred_at=T0,
                    kind=kind,
                    amount=Decimal(amount),
                    currency=currency,
                ),
            )
        ),
        source="csv",
    )


@pytest_asyncio.fixture
async def funded_account(conn):
    """Deposit 1000.00, then buy 5 shares at 51.00 (spends 255.00 as a FILL,
    not a movement). Net cash: 1000.00 - 255.00 = 745.00."""
    acc = await create_account(conn, name="Funded", venue="manual", account_type="cash")
    await _deposit(conn, acc, amount="1000.00")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCO", quote_currency="USD"),
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, quantity="5", price="51.00", ref="zx1")]
    )
    return acc


@pytest_asyncio.fixture
async def option_account(conn):
    """One buy of 2 option contracts at 3.50, contract_multiplier 100 -- no
    deposit, so the account's cash is entirely the fill's notional: -(2 * 3.50
    * 100) = -700.00. Every other fixture in this file uses AssetClass.EQUITY,
    whose multiplier is 1, so nothing here distinguishes "read the column"
    from "assume 1"."""
    acc = await create_account(conn, name="Option", venue="manual", account_type="cash")
    inst = await upsert_instrument(
        conn,
        Instrument(
            id=None,
            asset_class=AssetClass.OPTION,
            symbol="ZXCO  261218C00050000",
            quote_currency="USD",
            underlying="ZXCO",
            strike=Decimal("50"),
            expiry=datetime(2026, 12, 18, tzinfo=UTC).date(),
            option_right="call",
            contract_multiplier=Decimal("100"),
        ),
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, quantity="2", price="3.50", ref="opt1")]
    )
    return acc


@pytest_asyncio.fixture
async def an_account(conn):
    """No movements, no fills."""
    return await create_account(conn, name="Empty", venue="manual", account_type="cash")


@pytest_asyncio.fixture
async def two_funded_accounts(conn):
    """Two accounts with different cash, and -- load-bearing -- the second one
    has a FILL as well as a deposit.

    A: deposit 500.00, nothing else, so its cash is 500.00.
    B: deposit 300.00 then buy 2 ZXCB at 25.00, spending 50.00 as a fill: 250.00.

    The fill is what makes this fixture able to catch an unscoped fill query.
    With deposits alone on both accounts, dropping `WHERE f.account_id = $1`
    from account_cash's fill fetch changes nothing at all -- there are no
    fills to leak -- so the scoping test would pass against code that summed
    every account's trading into one balance. The movement predicate was
    always pinned; this closes the asymmetry."""
    a = await create_account(conn, name="FundedA", venue="manual", account_type="cash")
    await _deposit(conn, a, amount="500.00")
    b = await create_account(conn, name="FundedB", venue="manual", account_type="cash")
    await _deposit(conn, b, amount="300.00")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZXCB", quote_currency="USD"),
    )
    await insert_fills(
        conn, [_fill(b, inst, side=Side.BUY, quantity="2", price="25.00", ref="zb1")]
    )
    return a, b


@pytest_asyncio.fixture
async def mixed_currency_account(conn):
    """Two cash movements on the same account in different currencies -- USD
    and EUR. v1 does not model FX."""
    acc = await create_account(conn, name="Mixed", venue="manual", account_type="cash")
    await _deposit(conn, acc, amount="100.00", currency="USD")
    await _deposit(conn, acc, amount="50.00", currency="EUR")
    return acc


@pytest_asyncio.fixture
async def mixed_currency_instrument_account(conn):
    """The mismatch lives entirely on the OTHER source this time: every
    cash_movement here is USD, but the account's only fill is on an
    instrument whose quote_currency is EUR. Spec §8 requires checking both
    sources independently -- an account can be single-currency in one and
    not the other -- so this fixture must trip the refusal via the
    instrument side alone, with the movement side never varying."""
    acc = await create_account(conn, name="MixedInstrument", venue="manual", account_type="cash")
    await _deposit(conn, acc, amount="100.00", currency="USD")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="EURX", quote_currency="EUR"),
    )
    await insert_fills(
        conn, [_fill(acc, inst, side=Side.BUY, quantity="1", price="10.00", ref="eu1")]
    )
    return acc


@pytest_asyncio.fixture
async def mixed_fee_currency_account(conn):
    """The mismatch lives on the THIRD source: every cash_movement is USD and
    the only instrument quotes in USD, but the fill's fee is denominated in
    EUR. `net_cash` subtracts `fee` from a USD balance regardless of its
    currency (ledger/cash.py), so without the fee_currency check this account
    silently loses a EUR number out of a USD balance -- the confident wrong
    number the refusal exists to prevent. This was docs/known-gaps.md gap #24.

    The fee is NONZERO on purpose: the check ignores zero fees, so a zero here
    would prove nothing."""
    acc = await create_account(conn, name="MixedFee", venue="manual", account_type="cash")
    await _deposit(conn, acc, amount="100.00", currency="USD")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="FEEX", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            _fill(
                acc, inst, side=Side.BUY, quantity="1", price="10.00", ref="fee1",
                fee="1.50", fee_currency="EUR",
            )
        ],
    )
    return acc


@pytest_asyncio.fixture
async def zero_fee_in_another_currency_account(conn):
    """Same shape as the fixture above with ONE difference: the fee is zero.

    `fill.fee_currency` is `TEXT NOT NULL DEFAULT 'USD'` (db/schema.sql:73), so
    every fill carries some currency whether or not it charges a fee, and a
    venue that stamps its quote currency on a free fill is ordinary. A check
    built over every fill's fee_currency would refuse this perfectly
    single-currency account; the `fee != 0` guard is what stops it. A zero fee
    adds zero to the balance in any currency, so nothing can go wrong here."""
    acc = await create_account(conn, name="ZeroFee", venue="manual", account_type="cash")
    await _deposit(conn, acc, amount="100.00", currency="USD")
    inst = await upsert_instrument(
        conn,
        Instrument(id=None, asset_class=AssetClass.EQUITY, symbol="ZFEE", quote_currency="USD"),
    )
    await insert_fills(
        conn,
        [
            _fill(
                acc, inst, side=Side.BUY, quantity="1", price="10.00", ref="zfee1",
                fee="0", fee_currency="EUR",
            )
        ],
    )
    return acc


async def test_cash_combines_movements_and_fills(conn, funded_account):
    """A buy spends cash as a FILL, not a movement -- a balance built from
    movements alone would omit every trade."""
    assert await account_cash(conn, funded_account) == Decimal("745.00")


async def test_an_option_fill_uses_its_contract_multiplier(conn, option_account):
    """2 contracts at 3.50 with x100 costs 700, not 7. Dropping the multiplier
    makes the balance wrong by a hundredfold on every option trade."""
    assert await account_cash(conn, option_account) == Decimal("-700.00")


async def test_an_account_with_nothing_has_zero_cash(conn, an_account):
    assert await account_cash(conn, an_account) == Decimal(0)


async def test_cash_is_scoped_to_its_account(conn, two_funded_accounts):
    """Exact values, not `!=`: both the movement predicate and the FILL
    predicate have to be pinned. `!=` passes for any two unequal numbers, so
    an account_cash that summed B's fill into A's balance (450.00 vs 250.00)
    would still satisfy it -- two wrong numbers that happen to differ."""
    a, b = two_funded_accounts
    # A has no fills of its own; 500.00 only holds if B's is not summed in.
    assert await account_cash(conn, a) == Decimal("500.00")
    # 300.00 deposited - (2 * 25.00) spent as a fill.
    assert await account_cash(conn, b) == Decimal("250.00")


async def test_a_mixed_currency_account_is_refused(conn, mixed_currency_account):
    """v1 does not model FX. Summing across currencies produces a confident
    wrong number, which is the failure class this project exists to avoid."""
    with pytest.raises(MixedCurrencyError) as exc:
        await account_cash(conn, mixed_currency_account)
    assert "USD" in str(exc.value) and "EUR" in str(exc.value)


async def test_a_mixed_currency_account_is_refused_via_instrument_quote_currency(
    conn, mixed_currency_instrument_account
):
    """The mismatch this time comes from instrument.quote_currency, not
    cash_movement.currency -- every movement on this account is USD. Spec §8
    requires checking both sources independently ("an account can be
    single-currency in one and not the other"); a gate pinned only on the
    movement side would let this account straight through."""
    with pytest.raises(MixedCurrencyError) as exc:
        await account_cash(conn, mixed_currency_instrument_account)
    assert "USD" in str(exc.value) and "EUR" in str(exc.value)


async def test_a_nonzero_fee_in_another_currency_is_refused(
    conn, mixed_fee_currency_account
):
    """docs/known-gaps.md gap #24, closed. A fill carries TWO currencies and
    only one of them used to be checked: this account's movements and its
    instrument are both USD, so a gate built from those two alone lets it
    straight through and `net_cash` subtracts a EUR fee from a USD balance.

    Both currencies must be named -- the message is the only place a reader
    learns WHICH pair disagreed, and an account can hold several instruments."""
    with pytest.raises(MixedCurrencyError) as exc:
        await account_cash(conn, mixed_fee_currency_account)
    assert "USD" in str(exc.value) and "EUR" in str(exc.value)


async def test_a_zero_fee_in_another_currency_is_not_refused(
    conn, zero_fee_in_another_currency_account
):
    """The other half of the fee-currency check, and the reason it is written
    `if r["fee"] != Decimal(0)` rather than over every fill: fee_currency has a
    schema DEFAULT of 'USD' and is NOT NULL, so a fee-free fill always carries
    a currency that means nothing. Refusing on it would break ordinary
    single-currency accounts -- a false refusal, which for a command whose
    whole value is trustworthiness is as damaging as a false pass.

    Asserts the exact balance, not merely "did not raise": 100 deposited minus
    the 10 the fill spent, with a zero fee changing nothing."""
    assert await account_cash(conn, zero_fee_in_another_currency_account) == Decimal("90.00")
