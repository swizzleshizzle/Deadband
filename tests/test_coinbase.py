import pathlib
from datetime import UTC, datetime
from decimal import Decimal

from importers.coinbase import CoinbaseImporter
from ledger.types import AssetClass, Side

# Anchored to this test file's own location, not the process cwd — the same
# hazard test_purity.py's discovery had. A path relative to "tests/fixtures/..."
# only resolves when pytest happens to be invoked from the repo root; from any
# other directory this raises FileNotFoundError instead of quietly finding
# nothing, but it still makes the suite's ability to run depend on cwd, which
# it must not.
_FIXTURES_DIR = pathlib.Path(__file__).resolve().parent / "fixtures"
FIXTURE = (_FIXTURES_DIR / "coinbase" / "transactions.csv").read_text()


def batch():
    return CoinbaseImporter().parse(FIXTURE)


def test_buys_and_sells_become_fills():
    fills = batch().fills
    assert len(fills) == 3
    assert [f.side for f in fills] == [Side.BUY, Side.BUY, Side.SELL]


def test_fill_fields_are_mapped():
    f = batch().fills[0]
    assert f.executed_at == datetime(2026, 1, 15, 14, 30, tzinfo=UTC)
    assert f.quantity == Decimal("0.50000000")
    assert f.price == Decimal("61200.00")
    assert f.fee == Decimal("153.00")
    assert f.fee_currency == "USD"
    assert f.instrument.asset_class is AssetClass.CRYPTO_SPOT
    assert f.instrument.symbol == "BTC"
    assert f.instrument.quote_currency == "USD"


def test_deposits_become_cash_movements():
    cash = [c for c in batch().cash if c.kind == "deposit"]
    assert len(cash) == 1
    assert cash[0].amount == Decimal("5000.00")


def test_rewards_become_interest_cash_movements():
    cash = [c for c in batch().cash if c.kind == "interest"]
    assert len(cash) == 1
    assert cash[0].symbol == "ETH"
    assert cash[0].amount == Decimal("32.00")


def test_unhandled_row_types_are_reported_not_silently_dropped():
    result = batch()
    assert any("Convert" in w for w in result.warnings)
    assert len(result.unmapped_rows) == 1


def test_empty_input_yields_empty_batch():
    result = CoinbaseImporter().parse("")
    assert result.fills == ()
    assert result.cash == ()


def test_header_only_input_yields_empty_batch():
    header = FIXTURE.splitlines()[0]
    assert CoinbaseImporter().parse(header + "\n").fills == ()


def test_malformed_row_is_warned_about_and_skipped():
    bad = FIXTURE.splitlines()[0] + "\n2026-01-15T14:30:00Z,Buy,BTC,notanumber,USD,1,1,1,0,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert len(result.warnings) == 1


def test_utf8_bom_is_stripped():
    """UTF-8 BOM (U+FEFF) at start of file should not break parsing."""
    with_bom = "﻿" + FIXTURE
    result = CoinbaseImporter().parse(with_bom)
    result_no_bom = batch()
    # Both should parse identically
    assert len(result.fills) == len(result_no_bom.fills) == 3
    assert result.fills[0].quantity == result_no_bom.fills[0].quantity
    assert result.fills[0].price == result_no_bom.fills[0].price


def test_non_z_offset_timestamps_are_normalized_to_utc():
    """A timestamp with -05:00 offset should be converted to the equivalent UTC instant."""
    # 2026-01-15T14:30:00-05:00 is 2026-01-15T19:30:00Z in UTC
    csv_with_offset = (
        FIXTURE.splitlines()[0]
        + "\n2026-01-15T14:30:00-05:00,Buy,BTC,0.50000000,USD,61200.00,"
        + "30600.00,30753.00,153.00,Test\n"
    )
    result = CoinbaseImporter().parse(csv_with_offset)
    assert len(result.fills) == 1
    ts = result.fills[0].executed_at
    # Should be converted to UTC (tzinfo must be UTC, not a fixed offset)
    assert ts.tzinfo is UTC
    # And represent the same instant: 19:30 UTC
    assert ts.hour == 19
    assert ts.minute == 30


# --- Final fix wave, item 1: Coinbase lacked the zero-quantity guard its ----
# --- Fidelity twin has. A blank/zero Quantity Transacted survived parse() and
# --- preview, then raised inside Fill.__post_init__ during commit and took
# --- the whole batch down with it (the two good rows lost along with it). ---


def test_zero_quantity_buy_row_is_skipped_not_fatal():
    """Fails if the guard is missing: the blank-quantity row would show up in
    fills with quantity 0 (a Fill that raises in __post_init__ downstream)
    instead of being warned about, marked unmapped, and skipped — leaving the
    two good rows to commit successfully."""
    header = FIXTURE.splitlines()[0]
    rows = "\n".join(
        [
            header,
            "2026-01-15T14:30:00Z,Buy,BTC,0.50000000,USD,61200.00,30600.00,30753.00,153.00,ok",
            "2026-01-16T09:05:00Z,Buy,BTC,,USD,60800.00,0,0,0,blank quantity",
            "2026-02-03T11:20:00Z,Sell,BTC,0.25000000,USD,68000.00,17000.00,16915.00,85.00,ok",
        ]
    )
    result = CoinbaseImporter().parse(rows + "\n")
    assert len(result.fills) == 2
    assert [f.side for f in result.fills] == [Side.BUY, Side.SELL]
    assert len(result.unmapped_rows) == 1
    assert any("non-positive quantity" in w for w in result.warnings)


# --- Blocker pass, item 3: the guard above used `quantity == 0`, so a --------
# --- negative Quantity Transacted (unlike Fidelity, Coinbase never abs()'s ---
# --- it) still reached Fill.__post_init__'s `quantity <= 0` check at commit --
# --- and took the whole batch down with it, same failure mode as item 1. ----


def test_negative_quantity_buy_row_is_skipped_not_fatal():
    """Fails if the guard is still `quantity == 0`: the negative-quantity row
    would show up in fills with quantity -0.3 (a Fill that raises in
    __post_init__ downstream, since Coinbase — unlike Fidelity — never takes
    abs() of the parsed quantity) instead of being warned about, marked
    unmapped, and skipped, leaving the two good rows to commit successfully."""
    header = FIXTURE.splitlines()[0]
    rows = "\n".join(
        [
            header,
            "2026-01-15T14:30:00Z,Buy,BTC,0.50000000,USD,61200.00,30600.00,30753.00,153.00,ok",
            "2026-01-16T09:05:00Z,Buy,BTC,-0.30000000,USD,60800.00,0,0,0,negative quantity",
            "2026-02-03T11:20:00Z,Sell,BTC,0.25000000,USD,68000.00,17000.00,16915.00,85.00,ok",
        ]
    )
    result = CoinbaseImporter().parse(rows + "\n")
    assert len(result.fills) == 2
    assert [f.side for f in result.fills] == [Side.BUY, Side.SELL]
    assert len(result.unmapped_rows) == 1
    assert any("non-positive quantity" in w for w in result.warnings)


# --- Blocker pass, item 4: fee and (for non-fiat-priced cash) amount were ----
# --- computed from the same poison-prone Decimal fields as quantity/price ---
# --- but were not checked for finiteness anywhere. -------------------------


def test_non_finite_fee_is_rejected():
    header = FIXTURE.splitlines()[0]
    bad = header + "\n2026-01-15T14:30:00Z,Buy,BTC,0.5,USD,61200.00,0,0,Infinity,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert len(result.unmapped_rows) == 1
    assert any("non-finite" in w for w in result.warnings)


def test_non_finite_cash_amount_is_rejected():
    """Deposit rows compute amount directly from Quantity Transacted (asset ==
    currency, so no multiplication needed to reach Infinity). Fails if the
    cash branch has no finiteness guard: the deposit would show up in `cash`
    with amount Decimal("Infinity") instead of being warned about and
    skipped."""
    header = FIXTURE.splitlines()[0]
    bad = header + "\n2026-02-10T00:00:00Z,Deposit,USD,Infinity,USD,1.00,0,0,0,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.cash == ()
    assert len(result.unmapped_rows) == 1
    assert any("non-finite" in w for w in result.warnings)


# --- Final fix wave, item 2: poison Decimal values (NaN/Infinity) pass the --
# --- existing `except InvalidOperation` guard, since both are valid Decimal
# --- constructions. Verified upstream: Infinity survives Fill.__post_init__'s
# --- `quantity > 0` check, the DB's `quantity > 0` CHECK, and becomes a live
# --- allocation in group_fills. ----------------------------------------------


def test_non_finite_quantity_is_rejected():
    header = FIXTURE.splitlines()[0]
    bad = header + "\n2026-01-15T14:30:00Z,Buy,BTC,Infinity,USD,61200.00,0,0,0,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert len(result.unmapped_rows) == 1
    assert any("non-finite" in w for w in result.warnings)


def test_non_finite_price_is_rejected():
    header = FIXTURE.splitlines()[0]
    bad = header + "\n2026-01-15T14:30:00Z,Buy,BTC,0.5,USD,Infinity,0,0,0,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert len(result.unmapped_rows) == 1
    assert any("non-finite" in w for w in result.warnings)


# --- CRITICAL 1: a literal NaN quantity used to abort the whole file. -------
# --- `Decimal("NaN") <= 0` raises InvalidOperation (NaN is unordered), which
# --- escaped the parse loop's own try/except (that one only wraps the parse
# --- call above, not the ordering comparison below it) and took every row in
# --- the batch down with it, not just the poisoned one. Verified upstream:
# --- `NaN <= 0` raises InvalidOperation. Fixed by moving the is_finite()
# --- check above the `quantity <= 0` comparison. -----------------------------


def test_literal_nan_quantity_does_not_crash_the_whole_import():
    """Fails if the finiteness check is ever moved back below the `quantity <=
    0` comparison: a literal NaN would then raise InvalidOperation out of the
    parse loop (uncaught — that try/except only wraps the parse call above),
    aborting the whole file, so this test would error out instead of
    returning a result with the two good rows intact."""
    header = FIXTURE.splitlines()[0]
    rows = "\n".join(
        [
            header,
            "2026-01-15T14:30:00Z,Buy,BTC,0.50000000,USD,61200.00,30600.00,30753.00,153.00,ok",
            "2026-01-16T09:05:00Z,Buy,BTC,NaN,USD,60800.00,0,0,0,poison",
            "2026-02-03T11:20:00Z,Sell,BTC,0.25000000,USD,68000.00,17000.00,16915.00,85.00,ok",
        ]
    )
    result = CoinbaseImporter().parse(rows + "\n")
    assert len(result.fills) == 2
    assert [f.side for f in result.fills] == [Side.BUY, Side.SELL]
    assert len(result.unmapped_rows) == 1
    assert any("non-finite" in w for w in result.warnings)


# --- Item 4: cash amount sign convention. amount is always positive; --------
# --- direction lives in `kind` alone (see importers.base.OUTFLOW_KINDS). ----


def test_withdrawal_amount_is_positive_even_if_the_export_encodes_it_negatively():
    """Fails if the abs() normalization in the cash branch is removed: a
    withdrawal whose raw Quantity Transacted is negative would then produce a
    negative CanonicalCash.amount, disagreeing with Fidelity's twin (which
    always emits a positive amount for the same kind), and anything summing
    cash_movement.amount across accounts would get garbage."""
    header = FIXTURE.splitlines()[0]
    bad = header + "\n2026-04-01T00:00:00Z,Withdrawal,USD,-2000.00,USD,1.00,0,0,0,x\n"
    result = CoinbaseImporter().parse(bad)
    withdrawals = [c for c in result.cash if c.kind == "withdrawal"]
    assert len(withdrawals) == 1
    assert withdrawals[0].amount == Decimal("2000.00")


def test_zero_price_on_non_fiat_cash_is_warned_about():
    """A reward in non-fiat currency (e.g., ETH) with no spot price should warn."""
    csv_with_zero_price = (
        FIXTURE.splitlines()[0]
        + "\n2026-03-01T00:00:00Z,Rewards Income,ETH,0.01000000,USD,,0,0,0,Staking reward\n"
    )
    result = CoinbaseImporter().parse(csv_with_zero_price)
    # Should still create the cash movement
    assert len(result.cash) == 1
    # But should warn about missing price
    assert len(result.warnings) == 1
    assert "no spot price" in result.warnings[0]
    assert result.warnings[0].startswith("line 2:")
    # Amount should be 0 (0.01 * 0)
    assert result.cash[0].amount == Decimal("0")


# --- Task 5: silent loss must be impossible ---------------------------------
#
# The defect that started this whole effort: a real export names its money
# columns with a currency suffix, the importer read the bare names, missed
# every one, and _decimal(None) silently returned Decimal("0") for each --
# dates/quantities/symbols all correct, price and fee zero, no warning.
# importers/base.zero_price_warning is the shared guard; this pins that
# Coinbase's fill branch actually calls it, not just Fidelity's.


def _coinbase_row_with_zero_price() -> str:
    header = FIXTURE.splitlines()[0]
    return (
        header
        + "\n2026-01-15T14:30:00Z,Buy,BTC,0.50000000,USD,0.00,0.00,0.00,0.00,zero price test\n"
    )


def test_the_zero_price_guard_covers_coinbase_too():
    """Same defect class, same guard. Coinbase was never audited for it."""
    result = CoinbaseImporter().parse(_coinbase_row_with_zero_price())
    assert any("zero price" in w.lower() for w in result.warnings)


# --- C2: cash rows had no zero-amount guard ---------------------------------
#
# Coinbase's cash branch computes `amount` from Quantity Transacted (and,
# for non-fiat cash, Spot Price) with no equivalent of the fill branch's
# zero_price_warning. A blank/zero Quantity Transacted on a deposit-shaped
# row silently produces a $0.00 cash_movement with no warning at all --
# unlike the fill branch, which at least gets a "non-positive quantity"
# warning (and drops the row) for the same input shape.


def test_a_zero_quantity_deposit_produces_a_zero_amount_warning():
    header = FIXTURE.splitlines()[0]
    row = header + "\n2026-02-10T00:00:00Z,Deposit,USD,0,USD,1.00,0,0,0,blank deposit\n"
    result = CoinbaseImporter().parse(row)
    assert len(result.cash) == 1
    assert result.cash[0].amount == Decimal("0")
    assert any("zero amount" in w.lower() for w in result.warnings)


# --- I4: Coinbase never populated `blocking` at all -------------------------
#
# The spec's failure-policy table is venue-neutral; the plan narrowed the
# money-carrying-unmapped-row blocking policy to Fidelity, so Coinbase's
# ImportBatch never set blocking, and `--commit` proceeded past an
# unrecognised transaction type even when it carries real money. The shipped
# fixture (tests/fixtures/coinbase/transactions.csv) already contains an
# unmapped "Convert" row with a non-zero Quantity Transacted -- it has always
# been silently non-blocking.


def test_the_shipped_fixtures_unmapped_convert_row_blocks_the_commit():
    """The Convert row in the real, shipped fixture carries a non-zero
    Quantity Transacted (0.1 BTC) -- exactly the shape that must refuse the
    commit rather than let it proceed silently."""
    result = batch()
    assert result.blocking, "the shipped fixture's money-carrying Convert row must block"
    assert any("Convert" in msg for _ref, msg in result.blocking)


def test_an_unrecognised_transaction_type_carrying_an_amount_blocks():
    header = FIXTURE.splitlines()[0]
    row = (
        header
        + "\n2026-03-15T16:45:00Z,Stake,BTC,0.10000000,USD,70000.00,7000.00,7000.00,0.00,x\n"
    )
    result = CoinbaseImporter().parse(row)
    assert len(result.unmapped_rows) == 1
    assert result.blocking, "an unrecognised type carrying an amount must block"
    assert any("Stake" in msg for _ref, msg in result.blocking)


def test_an_unrecognised_transaction_type_with_no_quantity_only_warns():
    """A zero-quantity unrecognised row has no financial content -- blocking
    on it would make an inert, unrecognised row type (a report footer, say)
    refuse every import forever, same reasoning as Fidelity's twin guard."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n2026-03-15T16:45:00Z,Stake,BTC,0,USD,70000.00,0,0,0.00,x\n"
    result = CoinbaseImporter().parse(row)
    assert len(result.unmapped_rows) == 1
    assert result.blocking == ()


# --- I4 residual: matched-but-bad-data rows still only warned, never --------
# --- blocked. `blocking` was populated ONLY by the "unhandled transaction ---
# --- type" branch; the matched-but-garbled-money paths -- non-finite -------
# --- quantity/price/fee, non-positive quantity in the fill branch, and -----
# --- non-finite amount in the cash branch -- appended to `unmapped` and ----
# --- `warnings` directly and never consulted `blocking` at all. A row that -
# --- DID match a rule (Buy/Sell/Deposit/...) but carried a garbled or -------
# --- negative quantity/price/fee/amount therefore dropped a real dollar ----
# --- figure with only a warning nobody has to read, and --commit proceeded -
# --- with rc=0. This is the exact defect shape already closed for Fidelity -
# --- (finding I3) -- see importers/fidelity.py's reject().


def test_reviewer_demonstration_row_negative_quantity_with_real_total_blocks():
    """The reviewer's exact demonstration row: a Buy with a negative quantity
    and a real $30,000 total. Before the fix this fell into the
    non-positive-quantity branch, which appended to unmapped/warnings but
    never to blocking -- the $30,000 fill vanished with a warning nobody has
    to read, and the import reported success."""
    header = FIXTURE.splitlines()[0]
    row = (
        header
        + "\n2026-01-15T14:30:00Z,Buy,BTC,-0.50000000,USD,60000.00,30000.00,30000.00,0.00,x\n"
    )
    result = CoinbaseImporter().parse(row)
    assert result.fills == ()
    assert len(result.unmapped_rows) == 1
    assert result.blocking, "a negative-quantity row carrying a real total must block"


def test_non_finite_quantity_with_money_blocks():
    header = FIXTURE.splitlines()[0]
    bad = (
        header
        + "\n2026-01-15T14:30:00Z,Buy,BTC,Infinity,USD,61200.00,30600.00,30753.00,153.00,x\n"
    )
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert result.blocking, "a non-finite quantity carrying a real total must block"


def test_non_finite_price_with_money_blocks():
    header = FIXTURE.splitlines()[0]
    bad = (
        header
        + "\n2026-01-15T14:30:00Z,Buy,BTC,0.5,USD,Infinity,30600.00,30753.00,153.00,x\n"
    )
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert result.blocking, "a non-finite price on a real-quantity row must block"


def test_non_finite_fee_with_money_blocks():
    header = FIXTURE.splitlines()[0]
    bad = (
        header
        + "\n2026-01-15T14:30:00Z,Buy,BTC,0.5,USD,61200.00,30600.00,30753.00,Infinity,x\n"
    )
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert result.blocking, "a non-finite fee on a real-quantity row must block"


def test_non_finite_cash_amount_with_money_blocks():
    header = FIXTURE.splitlines()[0]
    bad = header + "\n2026-02-10T00:00:00Z,Deposit,USD,Infinity,USD,1.00,0,0,0,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.cash == ()
    assert result.blocking, "a non-finite cash amount must block"


# --- Finding B: the money guard read raw, exact-cased header names ---------
#
# _row_carries_money looked up "Quantity Transacted", "Subtotal", and
# "Total (inclusive of fees and/or spread)" verbatim. importers/fidelity.py
# normalizes header casing (and strips a trailing parenthetical qualifier)
# before any lookup; Coinbase did not. If the venue re-cases or renames a
# column, all three lookups miss, the guard returns False, and a
# money-carrying row silently stops blocking -- the same shape as the defect
# that motivated the whole task (money columns renamed with a currency
# suffix, read as zero, no warning), just on Coinbase's header casing
# instead of Fidelity's currency suffix.


def _with_recased_money_headers(text: str) -> str:
    """Rewrite the fixture's header row so every money column Coinbase's
    guard inspects is differently cased than the venue's own documented
    names, preserving column order so the data rows still align."""
    lines = text.splitlines()
    header = lines[0]
    header = header.replace("Quantity Transacted", "quantity transacted")
    header = header.replace("Subtotal", "SUBTOTAL")
    header = header.replace(
        "Total (inclusive of fees and/or spread)",
        "TOTAL (Inclusive Of Fees And/Or Spread)",
    )
    return "\n".join([header, *lines[1:]]) + "\n"


def test_recased_headers_still_parse_fills_and_cash_normally():
    """Guards against the fix breaking the ordinary path: re-casing the
    header must not change a single parsed value."""
    result = CoinbaseImporter().parse(_with_recased_money_headers(FIXTURE))
    baseline = batch()
    assert [f.quantity for f in result.fills] == [f.quantity for f in baseline.fills]
    assert [f.price for f in result.fills] == [f.price for f in baseline.fills]
    assert [c.amount for c in result.cash] == [c.amount for c in baseline.cash]
    assert result.fills[0].quantity == Decimal("0.50000000")


def test_recased_money_headers_still_block_a_money_carrying_unmapped_row():
    """Reproduces finding B directly: before normalization, re-casing
    'Subtotal' and 'Quantity Transacted' makes _row_carries_money's
    exact-cased lookups miss every one, so an unrecognised-transaction-type
    row that carries real money (a non-zero Quantity Transacted and Total)
    silently stops blocking, and --commit would proceed with rc=0."""
    header = _with_recased_money_headers(FIXTURE).splitlines()[0]
    row = (
        header
        + "\n2026-03-15T16:45:00Z,Stake,BTC,0.10000000,USD,70000.00,7000.00,7000.00,0.00,x\n"
    )
    result = CoinbaseImporter().parse(row)
    assert len(result.unmapped_rows) == 1
    assert result.blocking, (
        "a money-carrying unmapped row must still block after header re-casing"
    )
    assert any("Stake" in msg for _ref, msg in result.blocking)


def test_no_money_unmapped_row_still_warns_without_blocking():
    """A blank-quantity, blank-subtotal, blank-total unrecognised row has no
    financial content -- it must still be reported as unmapped (with a
    warning) but must NOT block, otherwise the fix would make a
    legitimately-unmappable row (a report footer, a currency-neutral no-op)
    refuse every import forever -- exactly the trap the Fidelity policy was
    carefully designed to avoid."""
    header = FIXTURE.splitlines()[0]
    row = header + "\n2026-03-15T16:45:00Z,Stake,BTC,,USD,70000.00,,,0.00,x\n"
    result = CoinbaseImporter().parse(row)
    assert len(result.unmapped_rows) == 1
    assert any("unhandled transaction type" in w for w in result.warnings)
    assert result.blocking == ()
