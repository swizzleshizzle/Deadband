import pathlib
from datetime import UTC
from decimal import Decimal

from importers.coinbase import CoinbaseImporter

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


# --- §10 gap 6, closed 2026-08-08: Coinbase fills come only from the -------
# --- Advanced Trade API now (importers/coinbase_api.py, venue
# --- "coinbase-api"). Before this cut-over, a fill imported by the CSV path
# --- was keyed on content_hash and one imported by the API path was keyed
# --- on venue_fill_id -- a fill arriving by both paths would not dedupe
# --- against itself and would be counted twice. These two tests used to
# --- assert the CSV importer mapped Buy/Sell rows to fills; they are
# --- rewritten, not deleted, so the cut-over reads as a deliberate decision
# --- rather than a regression a future reader has to reconstruct.


def test_buys_and_sells_are_reported_not_mapped_to_fills():
    """Used to assert the CSV importer mapped the fixture's two Buys and one
    Sell to three fills (see git history). It must now produce NO fills at
    all, and every one of those rows must still surface as a warning
    pointing at the new path -- see
    test_ignored_trade_rows_are_reported_not_silently_dropped below for the
    isolated version of that second assertion."""
    result = batch()
    assert result.fills == ()
    assert sum(1 for w in result.warnings if "coinbase-api" in w) == 3


def test_reported_trade_row_warning_names_the_row_and_the_new_path():
    """Used to assert the first fill's fields (timestamp, quantity, price,
    fee, instrument) were mapped correctly. There is no fill to hold those
    fields anymore, so this pins the replacement contract instead: the
    warning for the first Buy row (line 2 of the fixture) names the line,
    names the transaction type that triggered it, and tells the reader
    where fills come from now."""
    warning = next(w for w in batch().warnings if w.startswith("line 2:"))
    assert "'buy'" in warning
    assert "coinbase-api" in warning
    assert "deadband sync coinbase" in warning


def test_coinbase_csv_no_longer_produces_fills():
    """§10 gap 6, closed: fills come only from the API, so the two dedupe
    keys can never meet on one row."""
    result = CoinbaseImporter().parse(FIXTURE)
    assert result.fills == ()


def test_coinbase_csv_still_produces_cash():
    """The API has NO deposits, withdrawals, rewards or staking income.
    Retiring the CSV path wholesale would have silently destroyed every
    Coinbase cash movement."""
    kinds = {c.kind for c in CoinbaseImporter().parse(FIXTURE).cash}
    assert "deposit" in kinds


def test_ignored_trade_rows_are_reported_not_silently_dropped():
    """A trade row the CSV now declines to map must be visible -- dropping
    it without a word is the silent-loss shape this project keeps
    rediscovering. Built from just the fixture's Buy/Sell lines, not the
    full shipped FIXTURE: FIXTURE also contains an unrelated unmapped
    Convert row that blocks on its own (see
    test_the_shipped_fixtures_unmapped_convert_row_blocks_the_commit), and
    mixing that in here would make `blocking == ()` fail for a reason that
    has nothing to do with trade-row reporting."""
    header = FIXTURE.splitlines()[0]
    trade_rows = "\n".join(
        [
            header,
            "2026-01-15T14:30:00Z,Buy,BTC,0.50000000,USD,61200.00,30600.00,30753.00,153.00,ok",
            "2026-01-16T09:05:00Z,Buy,BTC,0.50000000,USD,60800.00,30400.00,30552.00,152.00,ok",
            "2026-02-03T11:20:00Z,Sell,BTC,0.25000000,USD,68000.00,17000.00,16915.00,85.00,ok",
        ]
    )
    result = CoinbaseImporter().parse(trade_rows + "\n")
    assert any("coinbase-api" in w for w in result.warnings)
    assert result.blocking == ()  # reported, but must not block a cash-only import


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
    """UTF-8 BOM (U+FEFF) at start of file should not break parsing. Used to
    compare the two parses via fills[0].quantity/.price; fills are always
    empty now (gap 6), so this compares cash amounts and the trade-row
    warning count instead -- still pinning that the BOM doesn't perturb
    parsing."""
    with_bom = "﻿" + FIXTURE
    result = CoinbaseImporter().parse(with_bom)
    result_no_bom = batch()
    # Both should parse identically
    assert result.fills == result_no_bom.fills == ()
    assert [c.amount for c in result.cash] == [c.amount for c in result_no_bom.cash]
    assert len(result.warnings) == len(result_no_bom.warnings)


def test_non_z_offset_timestamps_are_normalized_to_utc():
    """A timestamp with -05:00 offset should be converted to the equivalent
    UTC instant. Used a Buy row to observe this on the resulting fill;
    fills don't come from this path anymore, so this now uses a Deposit row
    -- the timestamp-parsing code above the fill/cash branch is shared by
    both, so a cash row exercises the exact same conversion."""
    # 2026-01-15T14:30:00-05:00 is 2026-01-15T19:30:00Z in UTC
    csv_with_offset = (
        FIXTURE.splitlines()[0]
        + "\n2026-01-15T14:30:00-05:00,Deposit,USD,5000.00,USD,1.00,"
        + "5000.00,5000.00,0.00,Test\n"
    )
    result = CoinbaseImporter().parse(csv_with_offset)
    assert len(result.cash) == 1
    ts = result.cash[0].occurred_at
    # Should be converted to UTC (tzinfo must be UTC, not a fixed offset)
    assert ts.tzinfo is UTC
    # And represent the same instant: 19:30 UTC
    assert ts.hour == 19
    assert ts.minute == 30


# --- Final fix wave, item 1: Coinbase lacked the zero-quantity guard its ----
# --- Fidelity twin has. A blank/zero Quantity Transacted survived parse() and
# --- preview, then raised inside Fill.__post_init__ during commit and took
# --- the whole batch down with it (the two good rows lost along with it). ---


def test_zero_quantity_buy_row_is_reported_like_any_other_trade_row():
    """Used to pin a guard that stopped a blank-quantity Buy row from
    reaching Fill.__post_init__ and taking the whole batch down with the two
    good rows. That guard, and the fill-construction path it protected, are
    both gone now (gap 6): no Buy/Sell row -- garbled or clean -- ever
    builds a CanonicalFill anymore, so there is nothing left to crash. This
    pins that a blank-quantity trade row is reported exactly like a clean
    one -- and, unlike an unmapped row, is never added to unmapped_rows or
    blocking, since "trade row" is a recognised shape, not an unhandled
    one."""
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
    assert result.fills == ()
    assert result.unmapped_rows == ()
    assert result.blocking == ()
    assert sum(1 for w in result.warnings if "coinbase-api" in w) == 3


# --- Blocker pass, item 3: the guard above used `quantity == 0`, so a --------
# --- negative Quantity Transacted (unlike Fidelity, Coinbase never abs()'s ---
# --- it) still reached Fill.__post_init__'s `quantity <= 0` check at commit --
# --- and took the whole batch down with it, same failure mode as item 1. ----


def test_negative_quantity_buy_row_is_reported_like_any_other_trade_row():
    """Used to pin that a negative-quantity Buy row (Coinbase, unlike
    Fidelity, never abs()'s the parsed quantity) was rejected before it
    could reach Fill.__post_init__'s `quantity <= 0` check. Same reasoning
    as the blank-quantity case above: that check lived inside the
    fill-construction branch, which no longer exists, so there is nothing
    left for a negative quantity to crash."""
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
    assert result.fills == ()
    assert result.unmapped_rows == ()
    assert result.blocking == ()
    assert sum(1 for w in result.warnings if "coinbase-api" in w) == 3


# --- Blocker pass, item 4: fee and (for non-fiat-priced cash) amount were ----
# --- computed from the same poison-prone Decimal fields as quantity/price ---
# --- but were not checked for finiteness anywhere. -------------------------


def test_non_finite_fee_on_a_trade_row_is_reported_not_rejected():
    """Used to assert a non-finite fee got the row rejected (marked
    unmapped) before it could reach Fill.__post_init__. Fee is now only
    ever read on the fill-construction path, and no Buy/Sell row takes
    that path anymore -- so a poison fee on a trade row is just part of a
    row that gets reported like any other, not a hazard needing its own
    guard, and it is never marked unmapped (an unmapped row is one whose
    TYPE isn't recognised; "buy" is recognised, just not mapped to a
    fill)."""
    header = FIXTURE.splitlines()[0]
    bad = header + "\n2026-01-15T14:30:00Z,Buy,BTC,0.5,USD,61200.00,0,0,Infinity,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert result.unmapped_rows == ()
    assert any("coinbase-api" in w for w in result.warnings)


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


def test_non_finite_quantity_on_a_trade_row_is_reported_not_rejected():
    """Same reasoning as the non-finite fee case above: the finiteness
    guard this pinned lived in the fill-construction branch, which no
    longer exists for Buy/Sell rows."""
    header = FIXTURE.splitlines()[0]
    bad = header + "\n2026-01-15T14:30:00Z,Buy,BTC,Infinity,USD,61200.00,0,0,0,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert result.unmapped_rows == ()
    assert any("coinbase-api" in w for w in result.warnings)


def test_non_finite_price_on_a_trade_row_is_reported_not_rejected():
    """Same reasoning again: price is only ever read to build a fill."""
    header = FIXTURE.splitlines()[0]
    bad = header + "\n2026-01-15T14:30:00Z,Buy,BTC,0.5,USD,Infinity,0,0,0,x\n"
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert result.unmapped_rows == ()
    assert any("coinbase-api" in w for w in result.warnings)


# --- CRITICAL 1: a literal NaN quantity used to abort the whole file. -------
# --- `Decimal("NaN") <= 0` raises InvalidOperation (NaN is unordered), which
# --- escaped the parse loop's own try/except (that one only wraps the parse
# --- call above, not the ordering comparison below it) and took every row in
# --- the batch down with it, not just the poisoned one. Verified upstream:
# --- `NaN <= 0` raises InvalidOperation. Fixed by moving the is_finite()
# --- check above the `quantity <= 0` comparison. -----------------------------


def test_literal_nan_quantity_on_a_trade_row_does_not_crash_the_import():
    """Used to pin that a literal NaN quantity didn't crash the whole file
    via an unguarded `quantity <= 0` comparison (`Decimal("NaN") <= 0`
    itself raises InvalidOperation, since NaN is unordered). That
    comparison no longer exists on the Buy/Sell path at all -- a trade row
    is reported and skipped before any arithmetic touches quantity,
    poisoned or not -- so this now pins the weaker, still load-bearing
    fact: parsing a NaN-quantity trade row does not raise, and the two
    good rows around it are unaffected."""
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
    assert result.fills == ()
    assert result.unmapped_rows == ()
    assert sum(1 for w in result.warnings if "coinbase-api" in w) == 3


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


def test_zero_price_on_a_trade_row_no_longer_needs_its_own_guard():
    """Used to pin that zero_price_warning fired for a zero-priced Buy row.
    That warning only ever fired from the fill-construction branch, which
    is gone -- a zero-priced Buy row is reported exactly like any other
    trade row now, with no separate "zero price" signal to look for on a
    row that was never going to become a fill either way."""
    result = CoinbaseImporter().parse(_coinbase_row_with_zero_price())
    assert result.fills == ()
    assert any("coinbase-api" in w for w in result.warnings)


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


def test_reviewer_demonstration_row_negative_quantity_is_reported_not_blocked():
    """The reviewer's exact demonstration row: a Buy with a negative quantity
    and a real $30,000 total. It used to fall into the non-positive-quantity
    branch, which (before finding I3 was fixed) dropped it with only a
    warning, then (after I3) blocked the commit outright. Gap 6 changes the
    picture again: this is a trade row, not fill data to validate at all,
    and a cash-only Coinbase `--commit` must not be refused because of it --
    see the brief's explicit "must NOT block" constraint."""
    header = FIXTURE.splitlines()[0]
    row = (
        header
        + "\n2026-01-15T14:30:00Z,Buy,BTC,-0.50000000,USD,60000.00,30000.00,30000.00,0.00,x\n"
    )
    result = CoinbaseImporter().parse(row)
    assert result.fills == ()
    assert result.unmapped_rows == ()
    assert result.blocking == ()
    assert any("coinbase-api" in w for w in result.warnings)


def test_non_finite_quantity_on_a_trade_row_is_reported_not_blocked():
    """Same reasoning as the negative-quantity case above: a poison quantity
    on a Buy/Sell row is never validated as fill data anymore, so it must
    not block a cash-only import."""
    header = FIXTURE.splitlines()[0]
    bad = (
        header
        + "\n2026-01-15T14:30:00Z,Buy,BTC,Infinity,USD,61200.00,30600.00,30753.00,153.00,x\n"
    )
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert result.blocking == ()
    assert any("coinbase-api" in w for w in result.warnings)


def test_non_finite_price_on_a_trade_row_is_reported_not_blocked():
    """Same reasoning again, for price."""
    header = FIXTURE.splitlines()[0]
    bad = (
        header
        + "\n2026-01-15T14:30:00Z,Buy,BTC,0.5,USD,Infinity,30600.00,30753.00,153.00,x\n"
    )
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert result.blocking == ()
    assert any("coinbase-api" in w for w in result.warnings)


def test_non_finite_fee_on_a_trade_row_is_reported_not_blocked():
    """Same reasoning again, for fee."""
    header = FIXTURE.splitlines()[0]
    bad = (
        header
        + "\n2026-01-15T14:30:00Z,Buy,BTC,0.5,USD,61200.00,30600.00,30753.00,Infinity,x\n"
    )
    result = CoinbaseImporter().parse(bad)
    assert result.fills == ()
    assert result.blocking == ()
    assert any("coinbase-api" in w for w in result.warnings)


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


def test_recased_headers_still_parse_cash_normally_and_still_report_trades():
    """Guards against the fix breaking the ordinary path: re-casing the
    header must not change a single parsed cash value, and Buy/Sell rows
    must still be reported (never mapped to fills, gap 6) regardless of
    header casing."""
    result = CoinbaseImporter().parse(_with_recased_money_headers(FIXTURE))
    baseline = batch()
    assert result.fills == baseline.fills == ()
    assert [c.amount for c in result.cash] == [c.amount for c in baseline.cash]
    assert sum(1 for w in result.warnings if "coinbase-api" in w) == sum(
        1 for w in baseline.warnings if "coinbase-api" in w
    )


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
