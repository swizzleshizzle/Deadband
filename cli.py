"""Deadband CLI. Preview before commit, always."""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from db.accounts import UnknownAccountError, create_account, get_account, list_accounts
from db.cash import MixedCurrencyError, account_cash
from db.corporate import (
    EffectPreview,
    add_action,
    find_duplicate,
    list_actions,
    preview_effect,
    remove_action,
)
from db.fills import add_manual_fills, delete_manual_fill
from db.import_flow import (
    AccountNotFoundError,
    AccountVenueMismatchError,
    BlockingRowsError,
    MixedDedupePathsError,
    RoutingReport,
    TransferRefused,
    UnknownRefsError,
    commit,
    preview,
)
from db.instruments import upsert_instrument
from db.marks import latest_marks, resolve_instrument_by_symbol, set_mark
from db.migrate import apply as apply_migrations
from db.pool import create_pool
from db.positions import open_positions
from db.snapshots import add_snapshot, latest_snapshot
from db.trades import list_trades, regroup_account
from ledger.grouping import TransferError
from importers.base import CorporateActionProposal, ImportBatch
from importers.registry import get_importer, list_importers
from ledger.corporate import ActionType, CorporateAction
from ledger.pnl import unrealized_pnl
from ledger.reconcile import Position, ReconcileVerdict, Snapshot, UnvaluableRef, reconcile
from ledger.types import AssetClass, Fill, FillSource, Instrument, Side
from venues.coinbase_client import CoinbaseCredentials, fetch_all_fills


async def cmd_migrate(_args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            # apply() unconditionally (re-)executes schema.sql, and db/migrations/
            # holds real migrations (starting with 001_a2_ledger_completion.sql),
            # so `applied` can be non-empty for two different reasons: pending
            # migrations on a database that already existed, or the entire schema
            # having just been created on a virgin one. Those are different
            # outcomes and must not share one message. Check for a table
            # schema.sql creates before calling apply(), while it's still
            # meaningful to ask "did this exist already?". Pinned to
            # current_schema() — the FIRST existing schema on the search_path —
            # because that is the one place apply()'s unqualified CREATEs land.
            # A bare to_regclass('account') scans the whole path and can find a
            # table in a LATER entry, answering about a schema apply() never
            # writes, and wrongly printing the regroup warning below.
            existed_before = await conn.fetchval(
                "SELECT to_regclass(quote_ident(current_schema()) || '.account')"
                " IS NOT NULL"
            )
            applied = await apply_migrations(conn)
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    if applied:
        print(f"applied {len(applied)} migration(s):")
        for name in applied:
            print(f"  {name}")
        if existed_before:
            # A migration can add columns but cannot recompute existing rows —
            # migration 001 changes how realized_pnl is derived, so rows written
            # before it keep the old convention until regrouped. A virgin
            # database has no pre-existing rows to be stale, so this warning is
            # scoped to `existed_before` rather than printed unconditionally;
            # doing otherwise on every fresh install would train the operator
            # to ignore it.
            print(
                "\nDerived columns are stale: migration 001 changes how realized_pnl\n"
                "is computed. Run `regroup --account <uuid>` for every account before\n"
                "trusting any P&L figure."
            )
    else:
        # Unreachable with existed_before == False: db/migrations/ always holds
        # at least one migration file (001_a2_ledger_completion.sql onward), so
        # a virgin database's empty schema_migrations table makes `applied`
        # non-empty every time -- the `if applied:` branch above always wins on
        # a fresh install. This branch only ever runs on a database that was
        # already fully up to date.
        print("already up to date")
    return 0


async def cmd_accounts(_args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            for a in await list_accounts(conn):
                print(f"{a['id']}  {a['venue']:<10} {a['name']:<24} {a['external_ref'] or '-'}")
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    return 0


async def cmd_accounts_add(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            account_id = await create_account(
                conn,
                name=args.name,
                venue=args.venue,
                account_type=args.account_type,
                default_intent=args.default_intent,
                external_ref=args.external_ref,
                # getattr, not args.ignore_on_import: a Namespace built by
                # hand (rather than through argparse, which always supplies
                # the store_true default) may omit the attribute entirely.
                ignore_on_import=getattr(args, "ignore_on_import", False),
            )
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    print(account_id)
    return 0


# --- corporate-action proposals (spec 2026-08-17 Sec8) --------------------
# `_preview_or_commit` prints these in a section of their own -- after the
# parsed-row line on a preview, and before the inserted-rows line on a
# --commit, where both passes run while the transaction is still open. NOTHING
# here
# ever calls add_action: D2/D3 -- a corporate action silently restates
# history across every account holding the instrument, so `corporate add`
# is the only path that stores one, and it is a human who runs it. Every
# function below only ever formats text.

# CorporateActionProposal.kind (importers/fidelity.py) -> the ActionType
# value cmd_corporate_add's --type expects. 'name_change' -> 'symbol_change'
# because ledger/corporate.py's ActionType has no NAME_CHANGE member -- only
# SYMBOL_CHANGE, the same mechanism (rewrite the instrument identity, ratio
# always 1:1) under the ledger's own name.
_KIND_TO_ACTION_TYPE: dict[str, str] = {
    "reverse_split": "reverse_split",
    "name_change": "symbol_change",
    "merger": "merger",
    "spinoff": "spinoff",
    "split": "split",
}

# kinds (see _KIND_TO_ACTION_TYPE) whose ActionType needs --resulting-symbol
# -- mirrors _RESULTING_INSTRUMENT_TYPES above, but keyed on the importer's
# kind strings rather than ActionType members, since that's what a
# CorporateActionProposal carries.
_KINDS_NEEDING_RESULTING_SYMBOL = {"name_change", "merger", "spinoff"}


def _placeholder_ratio_str(ratio: tuple[Decimal, Decimal] | None) -> str:
    return f"{ratio[0]}:{ratio[1]}" if ratio is not None else "<FILL IN>"


def _incomplete_reminder(p: CorporateActionProposal) -> str:
    """The line printed above a rendered `corporate add` that still has a
    placeholder in it, naming ONLY the placeholders that command actually
    carries.

    "fill in --ratio (and --symbol)" was printed unconditionally. For a share
    distribution that is worse than noise: _render_corporate_add_command
    prints the row's own stated ticker for `--symbol`, so the reminder tells
    a human to overwrite a value that is already correct, on a command they
    are about to run against a real ledger. The condition tested is the same
    one that function branches on -- `subject_symbol is not None` -- so the
    two can never disagree about which placeholders are present.
    """
    if p.subject_symbol is not None:
        return "  INCOMPLETE -- fill in --ratio before running:"
    return "  INCOMPLETE -- fill in --ratio (and --symbol) before running:"


def _render_corporate_add_command(
    p: CorporateActionProposal, ratio: tuple[Decimal, Decimal] | None
) -> str:
    """A `corporate add` invocation for one proposal -- printed for a human
    to read, edit and run; never executed by this code (see the module
    comment above and D2).

    `--resulting-symbol` is always a literal `<SYMBOL>` placeholder.
    `--symbol` is a placeholder only when the proposal states no subject
    symbol: the branch below tests `p.subject_symbol is not None`, NOT
    `p.kind`. Today that difference is invisible -- share distributions
    (kind "split") are the only proposals carrying a subject symbol, so
    "except split" and "when subject_symbol is present" pick out the same
    rows -- but the code is the kind-agnostic rule, and any future importer
    that sets subject_symbol on another kind gets that kind's real ticker
    printed here without this docstring having to change. Spec D7 makes CUSIP
    resolution advisory and says plainly that no CUSIP column is needed for
    this work -- there is no lookup this function could perform that would
    ever fill in a resulting symbol, or a source symbol identified only by
    CUSIP. The description and CUSIP printed alongside this command
    (_print_corporate_action_section) are what let a human supply those
    themselves.

    A share distribution (kind == "split") is the one proposal that reaches
    that branch today: unlike a reorganisation row, which states its
    instrument only by CUSIP, a share-distribution row states its own ticker
    outright (CorporateActionProposal.subject_symbol) -- and
    _complete_split_ratio's ratio, when present, is derived from THAT
    ticker's own holding. Printing the `<SYMBOL>` placeholder beside a ratio
    the ticker already named would be incoherent, so `--symbol` prints
    `subject_symbol` whenever it is present.

    `--ratio` is the one other placeholder this module can sometimes
    complete: always for name_change, for reverse_split when its two sources
    do not contradict each other, and for spinoff or split when their
    respective ledger completions (_complete_spinoff_ratio,
    _complete_split_ratio) succeed. When it can't -- always for merger,
    sometimes for spinoff or split, and for a reverse split whose stated and
    derived ratios DISPUTE each other (see _print_corporate_action_section)
    -- `ratio` here is None and the command prints `<FILL IN>` instead, same
    shape as the symbol placeholder, so this command is never rendered as
    though it were ready to run when it is not.
    """
    action_type = _KIND_TO_ACTION_TYPE[p.kind]
    symbol_str = p.subject_symbol if p.subject_symbol is not None else "<SYMBOL>"
    parts = [
        "corporate add",
        f"--type {action_type}",
        f"--symbol {symbol_str}",
        f"--ex-date {p.ex_date.isoformat()}",
        f"--ratio {_placeholder_ratio_str(ratio)}",
    ]
    if p.kind in _KINDS_NEEDING_RESULTING_SYMBOL:
        parts.append("--resulting-symbol <SYMBOL>")
    if p.kind == "spinoff":
        parts.append("--basis-allocation <FILL IN>")
    return " ".join(parts)


# ratio_source (importers/base.py's CorporateActionProposal) -> a sentence
# telling a human how strong the evidence behind the printed ratio is.
# Spec Sec6a: "two independent sources agreed" and "only one source existed"
# must never read the same, because collapsing them is exactly what would
# let a human mistake a confirmed cross-check for one that never ran.
_RATIO_SOURCE_STRENGTH: dict[str, str] = {
    "constant": "fixed -- a name/symbol change always converts 1:1, no share data involved",
    "derived": (
        "derived from the paired quantities only -- the venue's own text states no "
        "ratio at all, so there was nothing to cross-check it against"
    ),
    "derived+confirmed": (
        "derived from the paired quantities AND matches the ratio the venue's own "
        "text states -- two independent sources agree, the strongest evidence "
        "available (spec Sec6a)"
    ),
    "derived+disputed": (
        "derived from the paired quantities, and CONTRADICTED by the ratio the "
        "venue's own text states -- two independent sources disagree, so neither "
        "is offered as the answer (spec Sec6a)"
    ),
}

# What to print when a proposal carries a ratio_source this table has no
# sentence for -- a kind added to importers/ without a matching entry here.
# A missing default made `.get()` return None and print the literal "None"
# in the strength parenthesis, which reads as an assertion about the
# evidence rather than as the gap in this table that it is.
_UNKNOWN_RATIO_SOURCE_STRENGTH = (
    "provenance unrecorded -- this build has no description for that ratio source; "
    "treat the ratio as unverified"
)


def _reduce_decimal_ratio(new_qty: Decimal, old_qty: Decimal) -> tuple[Decimal, Decimal]:
    """Reduce a (new, old) pair to the smallest integer pair with the same
    ratio, via a Decimal-only Euclidean GCD -- never float, so this stays
    exact. The same reduction importers/fidelity.py's private `_reduce_ratio`
    does for a reverse split's quantities, reimplemented rather than
    imported: that function is private to importers/fidelity.py, and this
    task's own constraint ("nothing under importers/ changes") is exactly the
    reason not to reach into it and make cli.py depend on its internals.
    """
    # A non-finite input does not make this function wrong, it makes it
    # NEVER RETURN: bool(Decimal("NaN")) is True and NaN % x is NaN, so
    # `while b:` spins forever -- and it spins inside cmd_import's open
    # commit transaction, holding every lock it has taken. Decimal("Infinity")
    # raises InvalidOperation on the modulo instead, which is survivable but
    # still not something to let escape as an unexplained traceback.
    #
    # fill.quantity's CHECK is `quantity > 0` alone (db/schema.sql), and in
    # Postgres both NaN and Infinity compare > 0 -- migration
    # 002_reject_non_finite_numerics.sql closed that hole for
    # contract_multiplier and price but not for fill.quantity, so a
    # non-finite quantity IS storable today. The real guard is in
    # _long_holdings_as_of, which excludes non-finite sums in SQL before they
    # can reach here; this is a tripwire behind it, not the primary defence.
    # It raises rather than returning something, because there is no ratio a
    # NaN holding could honestly reduce to and a raise inside the commit
    # transaction rolls back cleanly, which the hang does not.
    if not (new_qty.is_finite() and old_qty.is_finite()):
        raise ValueError(
            f"cannot reduce a non-finite ratio ({new_qty}:{old_qty}) -- a "
            "non-finite quantity reached the ratio arithmetic, which "
            "_long_holdings_as_of is supposed to have filtered out"
        )
    a, b = abs(new_qty), abs(old_qty)
    while b:
        a, b = b, a % b
    divisor = a
    if divisor == 0:
        return new_qty, old_qty
    return new_qty / divisor, old_qty / divisor


async def _long_holdings_as_of(
    conn, account_id: UUID, cutoff: datetime, symbol: str | None
) -> list:
    """Instruments the account is NET LONG as of `cutoff`, optionally
    restricted to one symbol. `> 0`, never `<> 0`: both corporate-action
    completions ask "what shares was this received on?", and a net-short or
    flat holding is not an answer to that question.

    Extracted from _complete_spinoff_ratio so the split completion shares one
    query rather than a second copy that could drift from it. `symbol=None`
    reproduces the elimination path's own behaviour exactly (every
    instrument the account has ever traded is a candidate); a symbol narrows
    the candidate set to that one instrument.

    `< 'Infinity'::numeric` is not redundant with `> 0`, and it is the reason
    this is the single seam. Postgres NUMERIC accepts the literals 'NaN' and
    'Infinity', and orders them ABOVE every finite value, so both satisfy
    `> 0` -- verified against the running database, not reasoned about.
    `fill.quantity`'s only CHECK is `quantity > 0` (db/schema.sql);
    migration 002_reject_non_finite_numerics.sql added the `< 'Infinity'`
    bound to contract_multiplier and price but never to fill.quantity, so a
    non-finite quantity is storable today and would sum to one here. What it
    reaches is `_reduce_decimal_ratio`, whose Euclidean loop does not
    terminate on NaN -- inside cmd_import's open commit transaction, holding
    its locks. Excluding the row is the honest outcome: an account whose
    holding is not a number holds no answer to "what shares was this
    received on?", and it is reported as no holding rather than as a ratio.

    ORDER BY makes `rows[0]` deterministic. Nothing here should be reading
    rows[0] out of a multi-row result -- both callers check the length first
    -- but an unordered `LIMIT`-less GROUP BY has no defined row order, and a
    completion that silently depended on one would be irreproducible rather
    than merely wrong.
    """
    return await conn.fetch(
        """
        SELECT i.symbol,
               SUM(CASE WHEN f.side = 'buy' THEN f.quantity ELSE -f.quantity END) AS net_qty
          FROM fill f
          JOIN instrument i ON i.id = f.instrument_id
         WHERE f.account_id = $1
           AND f.executed_at < $2
           AND ($3::text IS NULL OR i.symbol = $3::text)
         GROUP BY i.id, i.symbol
        HAVING SUM(CASE WHEN f.side = 'buy' THEN f.quantity ELSE -f.quantity END) > 0
           AND SUM(CASE WHEN f.side = 'buy' THEN f.quantity ELSE -f.quantity END)
               < 'Infinity'::numeric
         ORDER BY i.symbol, i.id
        """,
        account_id,
        cutoff,
        symbol,
    )


async def _complete_spinoff_ratio(
    conn, account_id: UUID | None, p: CorporateActionProposal
) -> tuple[tuple[Decimal, Decimal] | None, str]:
    """Spec Sec6, last row: a spinoff's ratio is child shares (already on the
    proposal, from the file) over the parent's holding at the ex-date -- not
    derivable from the file at all, which is why importers/ always leaves
    `ratio` None for a spinoff (see CorporateActionProposal's docstring).
    This is the one place a proposal is completed outside the pure layer.

    WHICH instrument is the parent is answered in one of two ways, in this
    order:

    1. The row STATES it. Fidelity's spinoff rows read "DISTRIBUTION SPINOFF
       FROM:(TICKER )" with the child in the Symbol column, so the parent's
       own ticker is a fact the export supplies -- captured by the pure layer
       as CorporateActionProposal.parent_symbol. When it is present, this
       reads that instrument's holding and nothing else.
    2. Otherwise, by elimination: the account's own net holding, as of the
       ex-date, across every instrument it has ever traded. If exactly one
       instrument has a positive net quantity (LONG, never zero or short --
       a spinoff is only received on shares you are long), that is
       unambiguously the parent. Zero long holdings, or more than one, is
       reported as undeterminable (spec Sec7) rather than guessed at.

    Path 2 was the ONLY path until the final fix wave, and gap #47 recorded
    it as holding "for the two real accounts checked". Measured against the
    real exports, it does not: the account is long many instruments at the
    only real spinoff's ex-date, so the elimination rule reports "ambiguous"
    on 100% of the real data and the completion never fired at all. Path 1
    answers the same question exactly on that data (see gap #47 as
    corrected).

    A stated parent the account does not hold is NOT quietly demoted to path
    2: eliminating down to some other instrument would contradict the row's
    own statement and produce a confidently wrong ratio against a security
    the spinoff has nothing to do with. That case reports what happened and
    stops -- usually it means the parent's purchase is in a file that has
    not been imported yet, which the note says.

    Returns (ratio, note): `ratio` is None when it could not be completed,
    and `note` always explains -- either what the ratio was derived from, or
    why it wasn't.
    """
    if conn is None:
        return None, (
            "not completed: preview opens no database connection -- rerun "
            "with --commit to complete it from the ledger"
        )
    if account_id is None:
        return None, "not completed: no --account given, so the parent holding is unknown"
    if not p.quantities:
        return None, "not completed: the proposal carries no child-share quantity"
    child_qty = p.quantities[0]

    # The clock lives here, not in importers/ or ledger/: ex_date is a DATE,
    # fill.executed_at a TIMESTAMPTZ, and "holding at the ex-date" means the
    # position going into that day -- every fill strictly before its
    # midnight UTC. A fill dated ON the ex-date itself (which could be the
    # very reorganisation being priced) is excluded rather than counted
    # toward the parent it is arguably already part of.
    cutoff = datetime.combine(p.ex_date, time.min, tzinfo=UTC)
    # $3 is the stated ticker or NULL, and a NULL means "every instrument",
    # which is the elimination rule's own candidate set -- see
    # _long_holdings_as_of, extracted here so the LONG-only,
    # before-the-ex-date rules can never drift between this and the split
    # completion's own read.
    rows = await _long_holdings_as_of(conn, account_id, cutoff, p.parent_symbol)
    if p.parent_symbol is not None and len(rows) == 0:
        # The row named a parent and the account is not long it. Falling back
        # to elimination here would answer a question the venue already
        # answered, differently -- see this function's docstring.
        return None, (
            f"not completed: this row names {p.parent_symbol} as the parent it was "
            f"distributed on, but account {account_id} holds no LONG position in "
            f"{p.parent_symbol} as of {p.ex_date.isoformat()} -- import the file "
            "containing that purchase and re-run, rather than dividing by some "
            "other instrument the row does not name"
        )
    if len(rows) == 0:
        return None, (
            f"not completed: account {account_id} holds no LONG position in any "
            f"instrument as of {p.ex_date.isoformat()} to use as the parent -- a "
            "spinoff is only received on shares you are long; a net-short or "
            "flat holding does not qualify, so this excludes both"
        )
    if len(rows) > 1:
        symbols = ", ".join(sorted(r["symbol"] for r in rows))
        return None, (
            f"not completed: account {account_id} holds {len(rows)} instruments as "
            f"of {p.ex_date.isoformat()} ({symbols}) -- ambiguous which is the "
            "spinoff's parent"
        )

    parent_symbol = rows[0]["symbol"]
    parent_qty = rows[0]["net_qty"]
    ratio = _reduce_decimal_ratio(child_qty, parent_qty)
    how = (
        "named by the venue's own row"
        if p.parent_symbol is not None
        else "the account's only LONG holding at that date"
    )
    note = (
        f"derived from the ledger: {child_qty} child share(s) against "
        f"{parent_qty} {parent_symbol} share(s) held at {p.ex_date.isoformat()} "
        f"-- parent {how}"
    )
    return ratio, note


async def _complete_split_ratio(
    conn, account_id: UUID | None, p: CorporateActionProposal
) -> tuple[tuple[Decimal, Decimal] | None, str]:
    """A share distribution's ratio is (held + received) : held, and only the
    RECEIVED half is in the file. Spec D2/§5.

    Unlike a spinoff, the subject and the instrument receiving shares are the
    SAME one -- the row's own Symbol column names it -- so this reads that
    instrument's holding rather than another's, and never falls back to
    identifying a holding by elimination.

    The `> 0` long-position rule is deliberate and shared with the spinoff
    completion: shares are distributed on shares you are LONG, so a net-short
    or flat holding does not qualify. `<> 0` would let a short produce a
    negative ratio that only cmd_corporate_add's positivity check would
    catch, far downstream of where the mistake happened.

    Returns (ratio, note) on the same contract as _complete_spinoff_ratio:
    ratio is None when it could not be completed, and note always explains.
    """
    if conn is None:
        return None, (
            "not completed: preview opens no database connection -- rerun "
            "with --commit to complete it from the ledger"
        )
    if account_id is None:
        return None, "not completed: no --account given, so the holding is unknown"
    if not p.quantities or p.subject_symbol is None:
        return None, "not completed: the row states no symbol or no quantity received"

    received = p.quantities[0]
    cutoff = datetime.combine(p.ex_date, time.min, tzinfo=UTC)
    rows = await _long_holdings_as_of(conn, account_id, cutoff, p.subject_symbol)
    if not rows:
        return None, (
            f"not completed: account {account_id} holds no LONG position in "
            f"{p.subject_symbol} as of {p.ex_date.isoformat()} -- import the file "
            "containing that purchase and re-run, rather than deriving a ratio "
            "from a holding that is not there"
        )

    if len(rows) > 1:
        # `instrument.symbol` is NOT unique -- only `natural_key` is
        # (db/schema.sql) -- so filtering _long_holdings_as_of by symbol can
        # legitimately return several instruments, and GROUP BY i.id keeps
        # them apart rather than summing them into one. Taking rows[0] would
        # pick one of them and print a confident ratio derived from a
        # holding that is only part of the answer. _complete_spinoff_ratio
        # reports "ambiguous" in exactly this case; this is that parity,
        # restored. Reported by count and natural key rather than by symbol,
        # because every row here HAS the same symbol -- printing it N times
        # would say nothing about what differs.
        return None, (
            f"not completed: account {account_id} holds {len(rows)} distinct "
            f"instruments under the symbol {p.subject_symbol} as of "
            f"{p.ex_date.isoformat()} -- ambiguous which one this distribution "
            "was received on; symbol is not a unique instrument identifier"
        )

    held = rows[0]["net_qty"]
    # BOTH quantities, not just the symbol. D2's whole posture is to force an
    # informed human decision, and the reader of this note is deciding
    # whether to run a `corporate add` that restates history across every
    # account holding the instrument. The sibling spinoff note already prints
    # its two quantities; printing only the symbol here asked the reader to
    # trust the arithmetic instead of checking it, and gap #53 is precisely
    # the case where checking it is the only thing that would catch the
    # error.
    return _reduce_decimal_ratio(held + received, held), (
        f"derived from the ledger: {received} share(s) delivered by this row "
        f"against {held} {p.subject_symbol} share(s) held at "
        f"{p.ex_date.isoformat()}"
    )


# Kinds whose ratio can only be completed by reading the ledger -- as opposed
# to reverse_split/name_change (always carry their ratio out of importers/)
# and merger (structurally never derivable). _ledger_completed_notes_for is
# the one loop that calls both completions, keyed by the same _COMPLETERS
# table, so a third such kind is one dict entry away rather than a second
# hand-written loop that could drift from this one.
_COMPLETERS = {
    "spinoff": _complete_spinoff_ratio,
    "split": _complete_split_ratio,
}


async def _ledger_completed_notes_for(
    conn, account_id: UUID | None, batch: ImportBatch
) -> dict[int, tuple[tuple[Decimal, Decimal] | None, str]]:
    """(ratio, note) for every proposal in `batch.corporate_actions` whose
    kind needs a ledger read (see _COMPLETERS), keyed by its index. `conn` is
    None in preview (which opens no connection at all) and a real connection
    in the commit path; either way, each completer degrades honestly."""
    notes: dict[int, tuple[tuple[Decimal, Decimal] | None, str]] = {}
    for i, p in enumerate(batch.corporate_actions):
        completer = _COMPLETERS.get(p.kind)
        if completer is not None:
            notes[i] = await completer(conn, account_id, p)
    return notes


_ALL_CORPORATE_ACTION_KINDS = frozenset(
    {"reverse_split", "name_change", "merger", "spinoff", "split"}
)

# The three kinds whose ratio never needs a ledger read: reverse_split and
# name_change always carry their ratio out of importers/ already (spec §6),
# and a merger's is structurally absent regardless of ledger state (spec
# §6a). spinoff and split both depend on this import's own committed fills
# (see _COMPLETERS) -- which is exactly why the commit path below splits its
# printing into an EARLY pass (this set, safe even on a refused commit,
# since none of it reads anything the refusal left uncommitted) and a LATE
# pass (spinoff and split, after the commit that might supply their
# holding).
_NON_LEDGER_KINDS = frozenset({"reverse_split", "name_change", "merger"})

_CORPORATE_ACTION_HEADER = (
    "\n=== Corporate actions detected -- nothing above was written; "
    "review before running any command below ==="
)


def _print_corporate_action_section(
    batch: ImportBatch,
    ledger_notes: dict[int, tuple[tuple[Decimal, Decimal] | None, str]],
    *,
    kinds: frozenset[str] = _ALL_CORPORATE_ACTION_KINDS,
    include_cash_in_lieu: bool = True,
    header: str = _CORPORATE_ACTION_HEADER,
) -> None:
    """Spec Sec8: a clearly separated, hard-to-miss section -- one
    `corporate add` command per detected action, preceded by
    the evidence it was derived from and the venue's own description text,
    plus cash-in-lieu reported separately (D6: it is recognised, never
    applied, and must never be mistaken for something `corporate add` can
    record). Prints nothing at all when there is nothing to report, so an
    ordinary import (no corporate-action rows) is unaffected.

    `kinds` restricts which proposals this call renders. Preview (which
    never reaches a post-commit point at all -- see _preview_or_commit)
    always passes every kind. The commit path calls this twice: once EARLY,
    before routing/blocking can refuse the commit, with `kinds=
    _NON_LEDGER_KINDS` (reverse_split, name_change, merger -- none of which
    need this import's fills to have been committed, so they can still be
    reported on a refusal); and once LATE, after commit_batch runs, with
    `kinds={"spinoff", "split"}` and `include_cash_in_lieu=False` (already
    shown early) -- see _preview_or_commit for why spinoff and split alone
    must wait.

    D2/D3: never writes anything. Every command printed below is text for a
    human to read, edit and run; nothing here calls add_action.
    """
    matching = [(i, p) for i, p in enumerate(batch.corporate_actions) if p.kind in kinds]
    cash_in_lieu = batch.cash_in_lieu if include_cash_in_lieu else ()
    if not matching and not cash_in_lieu:
        return

    print(header)
    for i, p in matching:
        ratio = p.ratio
        note: str | None = None
        if p.kind in _COMPLETERS:
            ratio, note = ledger_notes.get(i, (None, "not completed"))

        print(f"\n{p.kind} ex {p.ex_date.isoformat()} -- {p.description}")
        if p.source_cusip or p.resulting_cusip:
            print(f"  cusip: {p.source_cusip or '?'} -> {p.resulting_cusip or '?'}")
        # D5: the ratio is an inference, the quantities are evidence -- print
        # both, always, not just when the ratio is missing.
        print(f"  evidence (quantities): {', '.join(str(q) for q in p.quantities)}")

        # A ratio whose two sources CONTRADICT each other is not a ratio this
        # tool may offer. Every reverse split in five years of real exports
        # lands here: the venue's text states a whole "N FOR N" while the
        # paired quantities -- one lot, with its fractional remainder cashed
        # out -- reduce to something that is right for that lot and wrong for
        # every other lot and holder. Printing either number in `--ratio`
        # would put a figure
        # nobody declared one paste away from being stored across every
        # account holding the instrument, so this renders the same visibly
        # incomplete `<FILL IN>` the merger gets, with BOTH candidates listed
        # (spec Sec6a: say so rather than silently preferring one source --
        # in the artefact the user acts on, not only in a stderr warning).
        disputed = p.ratio_source == "derived+disputed" and ratio is not None
        if disputed:
            stated = p.stated_ratio
            print(
                "  ** DISPUTED ** -- this ratio's two independent sources disagree, "
                "so neither is offered below"
            )
            print(
                f"    derived from the paired quantities: {ratio[0]}:{ratio[1]} "
                "(** APPROXIMATE **: reproduces THIS lot's share count and need not "
                "hold for any other lot or holder -- a cash-in-lieu remainder does "
                "exactly this)"
            )
            print(
                "    stated in the venue's own text: "
                + (f"{stated[0]}:{stated[1]}" if stated is not None else "(unparsed)")
            )
            print(f"  ratio: DISPUTED -- {_RATIO_SOURCE_STRENGTH['derived+disputed']}")
            print(_incomplete_reminder(p))
            print(f"  {_render_corporate_add_command(p, None)}")
            continue

        # The catch-all for a proposal flagged approximate by some route
        # OTHER than a stated/derived disagreement. importers/fidelity.py
        # sets `approximate` only together with 'derived+disputed' today, so
        # nothing reaches this on the Fidelity path -- it is kept so a future
        # importer that flags a ratio without that source still prints a
        # warning rather than silently presenting it as sound.
        if p.approximate:
            print(
                "  ** APPROXIMATE ** -- the derived ratio disagrees with the ratio "
                "the venue's own text states; verify manually before running anything"
            )

        if ratio is not None:
            strength = (
                note
                if p.kind in _COMPLETERS
                else _RATIO_SOURCE_STRENGTH.get(
                    p.ratio_source or "", _UNKNOWN_RATIO_SOURCE_STRENGTH
                )
            )
            print(f"  ratio: {ratio[0]}:{ratio[1]} ({strength})")
            print(f"  {_render_corporate_add_command(p, ratio)}")
        else:
            if p.kind == "merger":
                reason = (
                    "a merger's group is always three rows (one payout, two or more "
                    "resulting legs); a ratio can only be derived from exactly one "
                    "negative and one positive row, which a merger can never have -- "
                    "this is structural, not a parsing gap. Determine the ratio from "
                    "the venue's own statement and fill it in yourself."
                )
            else:
                reason = note or "could not be determined"
            print(f"  ratio: UNAVAILABLE -- {reason}")
            print(_incomplete_reminder(p))
            print(f"  {_render_corporate_add_command(p, None)}")

    if cash_in_lieu:
        print(
            "\n-- Cash in lieu of fractional shares: recognised, NOT applied "
            "(gap #43) --"
        )
        for desc in cash_in_lieu:
            print(f"  {desc}")


async def cmd_import(args) -> int:
    importer = get_importer(args.venue)
    batch = importer.parse(pathlib.Path(args.file).read_text())
    return await _preview_or_commit(importer.account_venue, batch, args, source="csv")


async def _preview_or_commit(venue: str, batch: ImportBatch, args, *, source: str) -> int:
    """Render the import decisions db/import_flow.py makes, and map them to an
    exit code. The three-phase body every entry point (`import`, `sync`)
    shares.

    Every DECISION this function used to make inline -- routing, each refusal,
    what a commit wrote -- now lives in db.import_flow.preview/commit. What is
    left here is printing and `return 2`, which is precisely the part no other
    caller can reuse: the HTTP import wizard consumes the same two functions
    and renders them its own way, rather than restating routing and refusal
    rules that would then be free to drift out of agreement with these. A
    second statement of a rule is a second place for it to be wrong.

    `venue` (always an importer's `.account_venue`, never its `.venue`
    identity) and `source` (the provenance stamped on every fill written here;
    keyword-only, no default) are passed straight through -- see
    db.import_flow.commit's docstring for why each is shaped that way, and
    what went wrong before it was. Keeping this one function (rather than a
    second copy of the preview/commit body for `sync`) is what the plan's "no
    second, parallel write path" constraint requires.
    """
    # main() has already rejected a malformed --account with a clean message,
    # so this parse cannot be the thing that raises here.
    account_id = UUID(args.account) if getattr(args, "account", None) else None

    # conn=None: the connection-free report. This is the ONLY thing a default
    # `deadband import` run ever asks for, and db.import_flow.preview cannot
    # open a connection of its own (it never imports create_pool).
    report = await preview(batch, venue=venue, conn=None, account_id=account_id)
    print(
        f"parsed {report.fill_count} fills, {report.cash_count} cash movements, "
        f"{report.transfer_count} transfers"
    )
    for w in report.warnings:
        print(f"  warning: {w}", file=sys.stderr)
    if report.unmapped_row_count:
        print(f"  {report.unmapped_row_count} row(s) not mapped", file=sys.stderr)

    if not args.commit:
        # Corporate-action proposals print here, before any other preview
        # diagnostic: spec Sec8 wants the section hard to miss, and it must
        # render even on a bare preview (no --commit, no --check-duplicates)
        # since that is the default way this command is run. conn=None,
        # account_id=None: preview never opens a database connection (a
        # tested invariant -- tests/test_cli.py's
        # test_preview_import_never_opens_a_database_connection), so a
        # spinoff's or split's ratio degrades to "not completed" here rather
        # than being read from the ledger; see _COMPLETERS.
        ledger_notes = await _ledger_completed_notes_for(None, None, batch)
        _print_corporate_action_section(batch, ledger_notes)

        # A single export can carry rows for more than one venue account
        # (Fidelity's account-number column, for instance). --commit routes
        # each row to its own account automatically (see db.importing.route_batch);
        # this preview-only warning is the pure, DB-free heads-up for the same
        # situation, since preview deliberately never opens a connection.
        #
        # Derived from refs_seen -- every account ref seen in the RAW rows --
        # rather than from fills/cash; see PreviewReport.rows_per_ref for why
        # a count of 0 here is the signal, not an omission.
        if len(report.refs_seen) > 1:
            for ref, n in report.rows_per_ref:
                print(f"    {ref}: {n} row(s)", file=sys.stderr)
            print(
                "  warning: this file mixes multiple account refs "
                f"({', '.join(report.refs_seen)}); --commit routes each row to "
                "its own account automatically",
                file=sys.stderr,
            )

        # --check-duplicates is the one explicit, opt-in exception to preview's
        # no-connection guarantee (see test_preview_import_never_opens_a_
        # database_connection in tests/test_cli.py, which pins the default
        # no-flag path). getattr, not args.check_duplicates: several existing
        # tests build a bare Namespace by hand without this attribute, same
        # reasoning as cmd_accounts_add's ignore_on_import getattr above. Spec
        # §7 requires preview to report what's already present; preview
        # deliberately never opens a connection on its own, so it structurally
        # cannot answer that without an explicit ask.
        if getattr(args, "check_duplicates", False):
            # C: rows with no external_ref (e.g. Coinbase) need --account to
            # be probed at all -- identical to --commit's own `needs_account`
            # check below. Checked here, before any connection is opened,
            # for the same reason --commit's version runs before its pool:
            # whether it's a problem depends only on the parsed file, not on
            # the database. Before this check existed, such rows were simply
            # dropped from the probe and it printed a count that silently
            # omitted them -- indistinguishable from "this file has no
            # duplicates" even though it was never checked.
            if report.needs_account:
                print(
                    "error: cannot check duplicates -- this file has row(s) "
                    "with no account ref to route by; pass --account to say "
                    "where they go",
                    file=sys.stderr,
                )
                return 2

            pool = await create_pool()
            try:
                async with pool.acquire() as conn:
                    # Passing a connection is what turns the probe on. It
                    # routes read-only (route_batch and probe_duplicates issue
                    # only SELECTs) using the SAME mechanism --commit uses,
                    # reused rather than reinvented so the probe can never
                    # disagree with --commit about where a row lands.
                    probed = await preview(
                        batch, venue=venue, conn=conn, account_id=account_id
                    )

                    # C: mirror --commit's own refusal exactly -- a row that
                    # routes to an unknown account ref is never probed, so
                    # printing a count without checking this first would
                    # silently omit it while looking complete.
                    # unknown_money_refs is the money-scoped set (see
                    # db.importing.RoutingPlan); a non-money unknown ref does
                    # not stand behind --commit's refusal either, so it must
                    # not stand behind this one.
                    if probed.unknown_money_refs:
                        print(
                            "error: cannot check duplicates -- unknown "
                            f"account ref(s): {', '.join(probed.unknown_money_refs)}",
                            file=sys.stderr,
                        )
                        return 2

                    # Not None: preview withholds the count only when it could
                    # not have been complete, and both of those cases (no
                    # destination for unrouted rows, unknown money-carrying
                    # refs) have just been refused above.
                    dupes = probed.duplicates
                    print(
                        f"  duplicate check: {dupes.fill_dupes} fill(s), "
                        f"{dupes.cash_dupes} cash movement(s), "
                        f"{dupes.transfer_dupes} transfer(s) already present"
                    )
            finally:
                # See the identical comment on the commit path below:
                # pool.close() must run after the `async with pool.acquire()`
                # block has exited, never from inside it, or close() deadlocks
                # waiting for a release that will never come.
                await pool.close()

        print("\npreview only — rerun with --commit to write")
        return 0

    # Rows with no external_ref at all (a venue with no per-row account
    # identifier, e.g. Coinbase) are never routed by route_batch -- they need
    # an explicit destination. Whether that's a problem depends only on the
    # parsed file, not on the database, so this check runs before the pool is
    # ever opened. db.import_flow.commit raises UnroutableRowsError on the
    # same condition for callers that skip this pre-check; the point of doing
    # it here is to not pay for a connection to learn it.
    if report.needs_account:
        print(
            "error: this file has row(s) with no account ref to route by "
            "(e.g. this venue's export carries no per-row account number); "
            "pass --account to say where they go",
            file=sys.stderr,
        )
        return 2

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            # EARLY: reverse_split, name_change and merger never need this
            # import's own fills to have been committed (their ratio is
            # either already on the proposal from importers/, or -- merger
            # -- structurally absent regardless of ledger state), so they
            # are printed here, before routing and every refusal below
            # can return 2. A refused commit writes nothing, but these three
            # kinds' proposals do not depend on anything having been
            # written, so there is no reason to withhold them from a user
            # who has to fix the refusal and re-run anyway -- see
            # _print_corporate_action_section's docstring for the split.
            # ledger_notes={}: neither spinoff nor split is in
            # _NON_LEDGER_KINDS, so _print_corporate_action_section never
            # looks one up here.
            _print_corporate_action_section(batch, {}, kinds=_NON_LEDGER_KINDS)

            try:
                # regroup=regroup_account looks redundant -- it is
                # db.import_flow.commit's own default -- but passing THIS
                # module's binding is what keeps the insert-and-regroup
                # transaction provable from the outside:
                # tests/db/test_cli.py::test_a_crash_during_regroup_leaves_no_
                # fills_through_the_real_cli patches cli.regroup_account to
                # raise, and a proof that points at a function the code no
                # longer calls proves nothing.
                result = await commit(
                    conn,
                    venue=venue,
                    batch=batch,
                    account_id=account_id,
                    source=source,
                    regroup=regroup_account,
                )
            except BlockingRowsError as exc:
                # Refuse the whole batch and write nothing -- see
                # ImportBatch.blocking for why this is neither "block on every
                # unmapped row" nor "block on none".
                print(
                    "error: refusing to commit -- row(s) below block the commit "
                    "(see each reason):",
                    file=sys.stderr,
                )
                for _ref, msg in exc.reasons:
                    print(f"  {msg}", file=sys.stderr)
                return 2
            except AccountNotFoundError as exc:
                print(f"error: no account with id {exc.account_id}", file=sys.stderr)
                return 2
            except AccountVenueMismatchError as exc:
                # A file parsed by one venue's importer must never be committed
                # to an account belonging to a different venue — that would
                # permanently attribute (e.g.) Coinbase fills to a Fidelity
                # account, with no CLI path to undo it.
                print(
                    f"error: account {exc.account_id} is a {exc.account_venue!r} "
                    f"account; refusing to commit a {exc.batch_venue!r} import to it",
                    file=sys.stderr,
                )
                return 2
            except UnknownRefsError as exc:
                # The state report comes first even on this refusal: the user
                # has to fix the unknown ref and re-run, and knowing where
                # everything else would have gone is what makes that possible.
                _print_routing_report(exc.routing)
                print(
                    "error: refusing to commit -- unknown account ref(s): "
                    f"{', '.join(exc.refs)}",
                    file=sys.stderr,
                )
                return 2
            except MixedDedupePathsError as exc:
                _print_routing_report(exc.routing)
                print(
                    "error: refusing to commit -- this batch's fills carry a venue "
                    "fill id, but the target account already holds fill(s) that "
                    "dedupe on content_hash instead:",
                    file=sys.stderr,
                )
                for mixed_account_id, legacy in exc.accounts:
                    print(
                        f"  {mixed_account_id}: {legacy} existing fill(s) with "
                        "content_hash set and venue_fill_id null",
                        file=sys.stderr,
                    )
                print(
                    "  The two dedupe indexes are disjoint, so the same trade "
                    "arriving by both paths would be inserted twice and double "
                    "the account's position and realized P&L. Remedy: delete the "
                    "older content_hash-keyed fills for this account (they are "
                    "the ones with source='csv' and venue_fill_id null) and "
                    "re-sync, or commit into a fresh account.",
                    file=sys.stderr,
                )
                return 2
            except TransferRefused as exc:
                _print_routing_report(exc.routing)
                print(
                    f"refusing import: {exc.cause} -- an outbound transfer that "
                    "the account's ledger cannot honour (importing years out of "
                    "order?); nothing was committed",
                    file=sys.stderr,
                )
                return 2

            _print_routing_report(result.routing)

            # LATE: spinoff and split, printed here after the commit above --
            # the one point in this function where either's ratio can be
            # completed against BOTH pre-existing fills and this import's own.
            # A real multi-year History export commonly carries the original
            # purchase and a later corporate action in the SAME file (see
            # tests/fixtures/fidelity/real_shape_history.csv); reading the
            # ledger before the batch was written would miss that purchase
            # entirely and report "no holding" for a ratio that is, in fact,
            # determinable. The read runs on the same connection the commit's
            # transaction has just been released on, so it sees those writes.
            #
            # A refused commit (every `return 2` above, all of which happen
            # before or inside a transaction that rolled back) never reaches
            # this section -- nothing has been written in that case, so a
            # ledger read would be no more complete here than it was at the
            # EARLY point above. reverse_split, name_change and merger do not
            # have this problem (see the EARLY call above) -- only spinoff's
            # and split's correctness trades away reporting on a refusal, and
            # on refusal the user sees the refusal's own error and re-runs; the
            # proposal reaches them on that next, successful attempt.
            #
            # History-dialect rows (the only dialect with corporate actions)
            # carry no per-row account ref, so the ENTIRE file routes to one
            # account: --account, the same one the unrouted-rows check above
            # already required whenever this batch has any unrouted fill or
            # cash movement.
            ledger_notes = await _ledger_completed_notes_for(conn, account_id, batch)
            _print_corporate_action_section(
                batch,
                ledger_notes,
                kinds=frozenset({"spinoff", "split"}),
                include_cash_in_lieu=False,
                header=(
                    "\n=== Spinoff/split ratio(s) completed against the "
                    "committed ledger ==="
                ),
            )
    finally:
        # pool.close() waits for every checked-out connection to be released.
        # It must run after the `async with pool.acquire()` block has exited
        # (or after an early `return` inside it unwound out of that `with`) —
        # never from inside it while the connection returned by acquire() is
        # still held, or close() deadlocks waiting for a release that will
        # never come from a still-open acquire block.
        await pool.close()

    print(
        f"inserted {result.fills_inserted} fills ({result.fills_skipped} already "
        f"present), {result.cash_inserted} cash movements, "
        f"{result.transfers_inserted} transfers, "
        f"{result.trades_regrouped} trades regrouped"
    )
    return 0


def _print_routing_report(routing: RoutingReport) -> None:
    """The commit path's state report: one line per account this batch reached,
    however it reached it. Printed on success AND on the refusals that happen
    after routing, since a user who has to fix a refusal and re-run needs to
    know where everything else would have gone.

    Every ref appears exactly once across the four groups -- an account that
    silently vanished from this report while the commit still reported success
    is the defect the last three groups exist to prevent.
    """
    for account_id, n in routing.mapped:
        print(f"  {account_id}: mapped, {n} row(s)")
    # These routed SUCCESSFULLY: their rows are dropped on purpose (the user
    # registered the account ignore_on_import), and this is not a failure path.
    for ref in routing.ignored_refs:
        print(f"  {ref}: ignored (ignore_on_import), skipped")
    # F: routing.unknown_refs is a superset of the money-scoped set that
    # refuses the commit -- it also includes a ref that appears ONLY in
    # refs_seen (an account whose rows are ALL unmapped and non-financial),
    # which routing used to be unable to see at all since it only looked at
    # fills/cash/blocking. Reporting the fuller set here does not change what
    # refuses the commit; that stays keyed on the money-scoped set alone.
    for ref in routing.unknown_refs:
        print(f"  {ref}: no matching account", file=sys.stderr)
    # A REGISTERED account whose rows all warned but produced no fill, cash
    # movement or blocking reason (an edge route_batch's own classification
    # doesn't reach). Named explicitly rather than let a real account silently
    # vanish from the report while the commit still reports success.
    for ref in routing.unclassified_refs:
        print(
            f"  {ref}: 0 row(s) mapped -- every row for this account "
            "failed to classify; see warnings above",
            file=sys.stderr,
        )


def _parse_sync_bound(raw: str | None) -> datetime | None:
    """--start/--end are ISO-8601 strings on the CLI; fetch_all_fills wants a
    datetime and calls .astimezone(UTC) on whichever it's given. A bound
    with no offset is anchored to UTC here rather than left for
    astimezone() to silently treat as the local zone -- the venue API's own
    sequence_timestamp is UTC, so reinterpreting a bare bound as local time
    would shift the requested window with no error at all."""
    if raw is None:
        return None
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def cmd_sync(args) -> int:
    """Fetch fills from a venue API and run them through the exact same
    preview/commit body `cmd_import` uses (`_preview_or_commit`) -- `sync`
    differs from `import` only in where the text comes from (an API call
    instead of a file on disk). Never grows its own write path.

    No `if args.venue != "coinbase"` guard here: argparse's own
    `choices=["coinbase"]` on the `venue` positional (main(), below) is the
    only thing that ever needs to reject an unknown sync venue, and it does
    so before cmd_sync is even called -- a second, hand-written check here
    could only ever agree with argparse's `choices` or silently drift out of
    sync with it, never usefully disagree. When a second venue is added,
    branch here on `args.venue` to pick its client/importer; until then
    there is nothing else for this function to check.
    """
    try:
        creds = CoinbaseCredentials.from_env()
    except RuntimeError as exc:
        # Fail loud: absent or rejected credentials must surface as an
        # error and a non-zero exit, never as a request that silently runs
        # unauthenticated and reports "0 fills found" (spec §10 gap 5).
        # Raised as SystemExit (rather than returned) so a caller driving
        # cmd_sync directly -- not through main()'s asyncio.run wrapper --
        # still gets a hard stop instead of a return code it could ignore.
        print(f"error: {exc}", file=sys.stderr)
        # `from exc` (M2, ruff B904): without it the credentials RuntimeError
        # is reported as "During handling of the above exception, another
        # exception occurred", which reads like a bug in the handler rather
        # than the cause it actually is.
        raise SystemExit(2) from exc

    text = await fetch_all_fills(
        creds,
        start=_parse_sync_bound(args.start),
        end=_parse_sync_bound(args.end),
    )
    importer = get_importer("coinbase-api")
    batch = importer.parse(text)
    # importer.account_venue ("coinbase"), not importer.venue
    # ("coinbase-api"): see importers/base.py's Importer.account_venue
    # docstring and _preview_or_commit's docstring above.
    #
    # source="api" (I2): these fills came off the REST endpoint, not a CSV.
    # commit_batch's `source` defaulted to "csv" and nothing overrode it, so
    # every fill `sync` had ever written claimed CSV provenance.
    return await _preview_or_commit(importer.account_venue, batch, args, source="api")


async def cmd_regroup(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                written = await regroup_account(conn, UUID(args.account))
    except UnknownAccountError as exc:
        # Same clean-error treatment cmd_import gives an unknown --account,
        # rather than the ValueError('None is not a valid TradeIntent')
        # traceback this used to produce with no account id in it anywhere.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except TransferError as exc:
        # Same data-refusal cmd_import's commit path already gives this: a
        # stored transfer the ledger cannot honour is a clean exit 2, never a
        # traceback. The transaction above rolled back with the exception.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited (including via the
        # early return above), never from inside it, or close() deadlocks
        # waiting for a release that will never come.
        await pool.close()
    print(f"{written} trades")
    return 0


async def cmd_transfers(args) -> int:
    """List outbound asset transfers (branch B). A stored row type with no
    read path is invisible; this exists for post-import verification."""
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT t.occurred_at, t.direction, t.quantity, t.market_value,
                       t.note, a.name AS account_name, i.symbol
                  FROM asset_transfer t
                  JOIN account a    ON a.id = t.account_id
                  JOIN instrument i ON i.id = t.instrument_id
                 WHERE ($1::uuid IS NULL OR t.account_id = $1)
                 ORDER BY t.occurred_at, t.id
                """,
                UUID(args.account) if args.account else None,
            )
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    if not rows:
        print("no transfers")
        return 0
    for t in rows:
        mv = t["market_value"]
        print(
            f"{t['occurred_at']:%Y-%m-%d}  {t['account_name']:<12} "
            f"{t['symbol']:<8} {t['direction']:<4} qty={t['quantity']}  "
            f"mv={mv if mv is not None else '--'}"
            + (f"  note={t['note']}" if t["note"] else "")
        )
    return 0


async def cmd_trades(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            rows = await list_trades(conn, UUID(args.account) if args.account else None)
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    for t in rows:
        print(
            f"{t['opened_at']:%Y-%m-%d}  {t['primary_underlying'] or '?':<8} "
            f"{t['direction']:<6} {t['status']:<6} "
            f"pnl={t['realized_pnl'] or 0:>12}  intent={t['intent']}"
        )
    return 0


# Display-only scale bounds for `deadband positions`. NOTHING below this
# comment changes what ledger/ computes or what the database stores -- the
# pure layer keeps its full 50-digit precision and the numerics keep theirs;
# only the string handed to print() is bounded.
#
# Why a bound is needed at all: `cost_basis` is a division (weighted notional
# / quantity) evaluated at ctx.prec = 50, so an ordinary two-lot position
# whose weighted average does not terminate (1 @ 10 + 2 @ 20) renders a
# 50-digit basis and, downstream, a 28-digit unrealized. Those digits assert a
# precision the inputs never had and wrap the row off a normal terminal.
#
# Why 8 dp and not 2: a 2-dp display quantum would print a satoshi-scale
# crypto price or quantity as "0.00" -- a silently wrong number, which is the
# outcome this project ranks worst. 8 dp covers every price and quantity scale
# the importers actually produce.
_DISPLAY_QUANT = Decimal("1E-8")

# ...and a floor, so a genuine zero renders "0.00" rather than "0". The
# unmarked-position placeholder is "--"; a real zero has to be visibly a
# number, since mark_price_chk permits a genuine 0 price.
_DISPLAY_MIN_DP = 2


def _fmt_decimal(value: Decimal) -> str:
    """Render a Decimal for a positions row: bounded scale, no exponent.

    Trailing zeros beyond two decimal places are trimmed, so an exact 25
    prints "25.00" and not "25.00000000".

    Two escape hatches, both deliberately preferring a wide-but-true column
    over a narrow-but-false one:

    * a value too large to quantize (InvalidOperation) is printed in full;
    * a non-zero value that would round to zero at 8 dp is printed in full,
      because "0.00" for a position that is not flat is exactly the silent
      lie the bound exists to avoid.
    """
    try:
        q = value.quantize(_DISPLAY_QUANT)
    except InvalidOperation:  # magnitude too large for the display scale
        return str(value)
    if q == 0:
        # `value != 0` means rounding, not the value, produced the zero.
        return str(value) if value != 0 else "0.00"
    text = format(q, "f")
    if "." in text:
        whole, _, frac = text.rstrip("0").partition(".")
        text = f"{whole}.{frac.ljust(_DISPLAY_MIN_DP, '0')}"
    return text


async def cmd_positions(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            positions = await open_positions(
                conn, UUID(args.account) if args.account else None
            )
            marks = await latest_marks(conn, [p.instrument_id for p in positions])
    finally:
        # See cmd_import's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()

    for p in positions:
        mark = marks.get(p.instrument_id)
        # Gate on unvaluable_reason, NEVER on direction and NEVER by catching
        # unrealized_pnl's NotImplementedError(SPREAD): a position can carry a
        # real, single-valued direction and still be unvaluable for another
        # reason (e.g. an unknown quantity on one contributing trade), and
        # catching the exception here would also swallow a future genuine
        # bug in unrealized_pnl itself. A position with a reason set is still
        # printed -- never filtered out -- because a position missing from a
        # position listing is this project's recurring silent-loss shape.
        if p.unvaluable_reason is not None:
            unreal, mark_col = f"n/a ({p.unvaluable_reason})", "--"
            # The quantity and cost basis go to "--" too, not just the mark
            # and unrealized columns. For a mixed-direction group `quantity`
            # is the sum of MAGNITUDES (long 10 + short 4 = 14: not the net,
            # not either leg, not gross exposure in any direction) and
            # `cost_basis` averages a long basis with a short one. For an
            # "open quantity unknown" group it is a partial sum over only the
            # priced contributors. Both are fabricated figures in the two
            # columns a reader parses first, and the "n/a (reason)"
            # disclaimer sits four fields to their right where it reads as
            # "we can't price this", not "the 14 is meaningless too".
            #
            # The row itself is still printed -- a position missing from a
            # position listing is this project's recurring silent-loss shape.
            # Blanking the numbers is the opposite of hiding the row: it
            # leaves the symbol, the reason, and nothing that could be
            # mistaken for a holding.
            qty_col = basis_col = "--"
        elif mark is None:
            # Absent from `marks`, not a zero -- db.marks.latest_marks never
            # reports a zero for an unmarked instrument (mark_price_chk
            # permits a genuine 0.00, so a placeholder must be visibly
            # different from that, not just "0.00" again).
            unreal, mark_col = "--", "--"
            qty_col, basis_col = _fmt_decimal(p.quantity), _fmt_decimal(p.cost_basis)
        else:
            price, as_of = mark
            unreal = _fmt_decimal(
                unrealized_pnl(p.quantity, p.cost_basis, price, p.multiplier, p.direction)
            )
            # The mark's age rides along in the same column as its price: a
            # month-old mark must never render identically to one from a
            # minute ago, so the as_of date is always shown, not just the
            # price.
            mark_col = f"{_fmt_decimal(price)} @{as_of:%Y-%m-%d}"
            qty_col, basis_col = _fmt_decimal(p.quantity), _fmt_decimal(p.cost_basis)
        estimated = " ~" if p.is_estimated else "  "
        # 21, not 10: an OCC option symbol is up to 21 characters
        # ("SPY   260821C00500000"), and at width 10 every later column on an
        # option row shifted right by whatever the symbol overflowed by.
        # Deliberately widened rather than truncated -- a truncated contract
        # symbol names a DIFFERENT contract (a different strike or expiry)
        # just as plausibly as the real one, and a misread strike is a wrong
        # position, whereas a wide column is only ugly. Anything longer than
        # 21 still overflows, loudly, for the same reason.
        # Account name, not just id: positions now group by (account,
        # instrument) rather than instrument alone (a taxable and a
        # retirement account's cost basis are not fungible), and --account
        # filters that grouping rather than changing what a row means, so an
        # unscoped listing can show the same symbol more than once, once per
        # account -- the account column is what tells those rows apart.
        # 15 wide, left-justified like the symbol column, and never
        # truncated for the same reason the symbol column isn't: a
        # truncated account name can read as a different, shorter-named
        # account that happens to exist, which is a wrong answer dressed as
        # a real one, whereas an overflowing column is only ugly. An
        # explicit space follows it (unlike the symbol column, which relies
        # on the estimated marker's own leading space) so a name at or past
        # the 15-char width still can't run straight into the quantity
        # column with no gap at all.
        print(
            f"{p.symbol:<21}{estimated} {p.account_name:<15} {qty_col:>14} {basis_col:>14} "
            f"{mark_col:>22} {unreal}"
        )
    if not positions:
        print("no open positions")
    return 0


# latest_marks (db/marks.py) treats the newest as_of as "the current price"
# with nothing else checking plausibility -- a fat-fingered year or a bad
# backfill would otherwise silently become today's price and produce a wrong
# unrealized figure with no signal at all. The tolerance absorbs clock skew
# between this box and the database, and the fact that "now" isn't identically
# defined on two machines, without opening the door to a meaningfully wrong
# future date. Two minutes comfortably covers ordinary clock drift for a
# command that is typed by hand, not fired in a tight loop.
_MARK_FUTURE_TOLERANCE = timedelta(minutes=2)


async def cmd_marks_set(args) -> int:
    # The clock lives here, in the I/O layer -- db/marks.py and everything
    # under ledger/ are clock-free by design. This single `now` anchors both
    # the omitted-as_of default and the future-date guard below, so the two
    # measure against the exact same instant.
    now = datetime.now(UTC)

    # Decimal("abc") raises decimal.InvalidOperation, which does NOT descend
    # from ValueError -- same class of gotcha the --account UUID parsing in
    # main() works around below (see its comment): a bare `except ValueError`
    # would let this crash through uncaught instead of becoming a clean
    # message. Decimal("NaN") and Decimal("Infinity") construct successfully
    # and slip past that catch entirely -- is_finite() is this codebase's
    # established check for catching them afterward (see
    # importers/fidelity.py, importers/coinbase_api.py); left unchecked,
    # mark_price_chk would refuse them as an uncaught
    # asyncpg.CheckViolationError instead of a clean CLI error. Parsed before
    # opening the pool: whether this is a problem depends only on the
    # argument, never on the database.
    try:
        price = Decimal(args.price)
    except InvalidOperation:
        print(f"error: --price {args.price!r} is not a valid number", file=sys.stderr)
        return 2
    if not price.is_finite():
        print(f"error: --price {args.price!r} must be a finite number", file=sys.stderr)
        return 2

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            if args.symbol:
                try:
                    instrument_id = await resolve_instrument_by_symbol(conn, args.symbol)
                except ValueError as exc:
                    print(
                        f"error: {exc} -- pass --natural-key instead of --symbol "
                        "to name the exact instrument",
                        file=sys.stderr,
                    )
                    return 2
            else:
                instrument_id = await conn.fetchval(
                    "SELECT id FROM instrument WHERE natural_key = $1", args.natural_key
                )
                if instrument_id is None:
                    print(
                        f"error: no instrument with natural_key {args.natural_key!r}",
                        file=sys.stderr,
                    )
                    return 2

            as_of = datetime.fromisoformat(args.as_of) if args.as_of else now

            # A naive (timezone-less) as_of must be caught HERE, before the
            # future-date comparison just below -- `as_of > now + tolerance`
            # between an offset-naive and an offset-aware datetime raises a
            # raw, uncaught TypeError ("can't compare offset-naive and
            # offset-aware datetimes"), never reaching set_mark's own
            # ValueError for exactly this case. Checking first means a
            # fat-fingered timestamp with no offset always gets a clean
            # message instead of a traceback.
            if as_of.tzinfo is None:
                print(
                    f"error: --as-of {args.as_of!r} has no UTC offset "
                    "(e.g. append +00:00 or Z)",
                    file=sys.stderr,
                )
                return 2

            # Refuse before writing: an ambiguous symbol, a naive as_of
            # (above), or a future-dated as_of (below) must never half-apply.
            # All of resolution and validation happens before set_mark is
            # ever called.
            if as_of > now + _MARK_FUTURE_TOLERANCE:
                print(
                    f"error: --as-of {as_of.isoformat()} is in the future "
                    f"(tolerance: {_MARK_FUTURE_TOLERANCE})",
                    file=sys.stderr,
                )
                return 2

            await set_mark(conn, instrument_id, price, as_of)
    finally:
        # See cmd_trades's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()
    return 0


# --- hand-entered fills (Task 3, entry-import write path) ------------------
#
# `fills add`/`fills rm` are the CLI's own record-and-undo pair for a fill
# nobody's export ever produced -- a broker phoning in a manual correction,
# a private-loan repayment, whatever doesn't arrive as a file. A later task
# adds HTTP endpoints that call add_manual_fills/delete_manual_fill directly;
# this is the first, and remains a real, caller of them.


async def cmd_fills_add(args) -> int:
    # Issue #27: an instrument was minted with symbol='' and became an
    # unnamed position that renders as a blank row in every position/P&L
    # view. Manual entry is a second way to reach upsert_instrument with a
    # caller-supplied symbol -- importers/ never pass one this raw, unvalidated
    # -- so it needs its own guard. Checked before the pool even opens:
    # whether the symbol is blank depends only on the argument, never on the
    # database, and the failing test for this asserts create_pool is never
    # called at all.
    symbol = (args.symbol or "").strip()
    if not symbol:
        print("error: --symbol must not be blank", file=sys.stderr)
        return 2

    # Decimal("abc") raises InvalidOperation, not ValueError -- see
    # cmd_marks_set's identical comment. Decimal("NaN") and
    # Decimal("Infinity") construct fine and slip past that catch entirely;
    # is_finite() is this codebase's established second check (importers/
    # fidelity.py, importers/coinbase_api.py, cmd_marks_set above). All three
    # numbers are parsed before the pool opens, for the same reason as the
    # symbol check above.
    try:
        quantity = Decimal(args.quantity)
        price = Decimal(args.price)
        fee = Decimal(args.fee)
    except InvalidOperation as exc:
        print(f"error: not a valid number: {exc}", file=sys.stderr)
        return 2
    if not all(v.is_finite() for v in (quantity, price, fee)):
        print("error: quantity, price and fee must be finite numbers", file=sys.stderr)
        return 2
    # fill.quantity's only CHECK is `quantity > 0` (db/schema.sql) -- a
    # negative or zero hand-entered quantity would either be rejected by
    # Postgres with an opaque CheckViolationError or, for a manual entry
    # relying on --side to carry direction, silently invert it. Refused here,
    # before the pool opens, same reasoning as every other argument check
    # above.
    if quantity <= 0:
        print("error: --quantity must be positive", file=sys.stderr)
        return 2

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            account_id = UUID(args.account)
            if await get_account(conn, account_id) is None:
                print(f"error: no account {account_id}", file=sys.stderr)
                return 2
            async with conn.transaction():
                # EQUITY is a placeholder asset class for a hand-entered
                # symbol this command has no other way to classify -- upsert_
                # instrument keys on natural_key, so a symbol that already
                # exists under a different asset class resolves to THAT
                # instrument rather than minting a second, conflicting one.
                instrument_id = await upsert_instrument(
                    conn,
                    Instrument(
                        id=None,
                        asset_class=AssetClass.EQUITY,
                        symbol=symbol.upper(),
                        quote_currency="USD",
                    ),
                )
                fill = Fill(
                    id=uuid4(),
                    account_id=account_id,
                    instrument_id=instrument_id,
                    executed_at=datetime.fromisoformat(args.executed_at),
                    side=Side(args.side),
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    fee_currency=args.fee_currency,
                    source=FillSource.MANUAL,
                    # Neither dedupe key applies to a hand-entered fill: no
                    # venue issued a venue_fill_id, and content_hash's
                    # (executed_at, symbol, side, quantity, price)-plus-index
                    # shape would collapse two genuinely separate manual
                    # entries into one. add_manual_fills (db/fills.py)
                    # enforces both as a precondition, not just a convention.
                    venue_fill_id=None,
                    is_estimated=False,
                )
                (fill_id,) = await add_manual_fills(conn, [fill])
                # A fill with no trade behind it is invisible to every
                # position/P&L view that reads from trade rather than fill --
                # regroup_account is what turns this one insert into
                # something the rest of the system can see.
                await regroup_account(conn, account_id)
            print(fill_id)
            return 0
    finally:
        # See cmd_marks_set's identical comment: pool.close() must run after
        # the `async with pool.acquire()` block has exited, never from
        # inside it, or close() deadlocks waiting for a release that will
        # never come.
        await pool.close()


async def cmd_fills_rm(args) -> int:
    try:
        fill_id = UUID(args.id)
    except ValueError:
        print(f"error: --id {args.id!r} is not a UUID", file=sys.stderr)
        return 2
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            account_id = await conn.fetchval(
                "SELECT account_id FROM fill WHERE id = $1", fill_id
            )
            if account_id is None:
                print(f"error: no fill {fill_id}", file=sys.stderr)
                return 1
            async with conn.transaction():
                # delete_manual_fill's own WHERE clause carries the
                # source='manual' check -- it returns False for a fill that
                # exists but was imported, same as for one that doesn't exist
                # at all. The account_id lookup just above is what lets this
                # branch tell the two apart and print the right message for
                # each.
                if not await delete_manual_fill(conn, fill_id):
                    print(
                        f"error: fill {fill_id} was imported, not hand-entered; "
                        "imported fills are immutable",
                        file=sys.stderr,
                    )
                    return 1
                # Deleting a fill without regrouping would leave stale trade
                # rows behind -- the account's position/P&L would still
                # reflect a fill that's gone.
                await regroup_account(conn, account_id)
            return 0
    finally:
        # See cmd_fills_add's identical comment: pool.close() must run after
        # the `async with pool.acquire()` block has exited, never from
        # inside it, or close() deadlocks waiting for a release that will
        # never come.
        await pool.close()


def _parse_as_of(raw: str) -> datetime | None:
    """Parse `--as-of` for `snapshot add` and `reconcile`: a bare date becomes
    midnight UTC, a timestamp is taken as written, and anything else -- or a
    timestamp with no UTC offset -- is refused. Returns None after printing the
    refusal to stderr; the caller turns that into `return 2`. The flag name is
    hardcoded rather than a parameter because both callers spell it `--as-of`
    and both refusal messages were already byte-identical; a third caller under
    a different flag name would be the moment to parametrise it, not before.

    ONE parser for both commands, deliberately. They used to carry
    near-identical copies, and the copies are exactly how the two drifted apart
    once already: `snapshot add` accepted the bare date README.md's own worked
    example passes, `reconcile` did not, and the documented two-line invocation
    exited 2 on its second line. `cmd_marks_set` is intentionally NOT a caller
    -- it accepts a timestamp only, and widening it to bare dates would be a
    behaviour change, not a refactor.

    The property the timestamp fallback depends on is that
    `date.fromisoformat` rejects anything carrying a TIME COMPONENT -- verified
    on this interpreter (3.12) for `2026-07-31T12:00`, `2026-07-31 12:00` and
    `2026-07-31T12:00+00:00`, all ValueError. That, not "it accepts only
    YYYY-MM-DD", is what makes the fallthrough sound: since 3.11 it also
    accepts `20260801` and `2026-W31-1`, and the older comment here claimed
    otherwise. Both of those are legitimate ways to name a day and correctly
    become midnight UTC, so the widening costs nothing; what would break the
    two-step is a time-carrying string being swallowed by the first branch and
    never reaching the tz guard, and that cannot happen.
    """
    try:
        return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=UTC)
    except ValueError:
        pass

    try:
        as_of = datetime.fromisoformat(raw)
    except ValueError:
        print(f"error: --as-of {raw!r} is not a valid date or timestamp", file=sys.stderr)
        return None

    # Same TypeError hazard cmd_marks_set's identical comment describes: an
    # offset-naive datetime compared against an offset-aware one downstream
    # (`as_of > now + tolerance` in snapshot add, latest_snapshot's own
    # `as_of <= $2` bind in reconcile) raises an uncaught TypeError or an
    # asyncpg error rather than reaching a clean refusal. A bare DATE is
    # exempt -- it is given UTC above rather than implying an unnamed
    # wall-clock zone the way a bare timestamp does.
    if as_of.tzinfo is None:
        print(
            f"error: --as-of {raw!r} has no UTC offset "
            "(e.g. append +00:00 or Z, or pass a bare date)",
            file=sys.stderr,
        )
        return None
    return as_of


async def cmd_snapshot_add(args) -> int:
    # The clock lives here, in the I/O layer -- db/snapshots.py is
    # clock-free by design, same reasoning as cmd_marks_set's identical
    # comment. This anchors the future-date guard below.
    now = datetime.now(UTC)

    # Same InvalidOperation/is_finite guards cmd_marks_set already has for
    # --price, applied to both broker figures: Decimal("abc") raises
    # InvalidOperation (not a ValueError), and Decimal("NaN") /
    # Decimal("Infinity") construct successfully and would otherwise reach
    # the database as a broker figure. Both parsed before opening the pool --
    # whether this is a problem depends only on the arguments, never on the
    # database.
    try:
        total_equity = Decimal(args.equity)
    except InvalidOperation:
        print(f"error: --equity {args.equity!r} is not a valid number", file=sys.stderr)
        return 2
    if not total_equity.is_finite():
        print(f"error: --equity {args.equity!r} must be a finite number", file=sys.stderr)
        return 2

    try:
        cash_balance = Decimal(args.cash)
    except InvalidOperation:
        print(f"error: --cash {args.cash!r} is not a valid number", file=sys.stderr)
        return 2
    if not cash_balance.is_finite():
        print(f"error: --cash {args.cash!r} must be a finite number", file=sys.stderr)
        return 2

    # A bare date ("2026-07-31") is the ordinary way to enter a statement date
    # and becomes midnight UTC; a naive TIMESTAMP is refused, matching
    # marks_set exactly, because unlike a bare date it silently implies a
    # wall-clock zone nobody named. Both rules -- and the refusal messages --
    # live in _parse_as_of, shared with cmd_reconcile so the two commands
    # cannot read the same string differently again.
    as_of = _parse_as_of(args.as_of)
    if as_of is None:
        return 2

    # Same reasoning as cmd_marks_set's identical guard: latest_snapshot
    # treats the newest as_of as current, so a fat-fingered year would
    # silently become the figure every reconciliation compares against.
    # Reuses cmd_marks_set's tolerance constant rather than defining a
    # second one -- both commands are typed by hand, not fired in a loop,
    # and absorb the same clock skew for the same reason.
    if as_of > now + _MARK_FUTURE_TOLERANCE:
        print(
            f"error: --as-of {as_of.isoformat()} is in the future "
            f"(tolerance: {_MARK_FUTURE_TOLERANCE})",
            file=sys.stderr,
        )
        return 2

    account_id = UUID(args.account)

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            # Same get_account-then-check-None shape cmd_reconcile uses for
            # its own --account (`cli.py`, step 1 below). Without it an
            # unknown id reached account_snapshot.account_id's foreign key
            # and escaped as a raw asyncpg.ForeignKeyViolationError
            # traceback, since main() catches only OSError -- the worst of
            # the three behaviours docs/known-gaps.md gap #26 compares.
            account = await get_account(conn, account_id)
            if account is None:
                print(f"error: no account with id {account_id}", file=sys.stderr)
                return 2

            await add_snapshot(
                conn,
                account_id,
                as_of,
                cash_balance=cash_balance,
                total_equity=total_equity,
                note=args.note,
            )
    finally:
        # See cmd_trades's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()

    # Spec §7: "snapshot add writes one row and prints what it stored." Not
    # decoration -- `add_snapshot`'s ON CONFLICT DO UPDATE means re-adding the
    # same (account, as_of) silently OVERWRITES a stored broker figure, which
    # is the edit path gap #21 describes and the table keeps no history of.
    # Echoing the stored figures is what lets the typist see a fat-fingered
    # 523.40 before `reconcile` reports it as drift days later. Printed after
    # the write, never before: it must report what the database accepted.
    print(
        f"snapshot stored for account {account_id}: as of {as_of.isoformat()}, "
        f"equity {total_equity}, cash {cash_balance}"
    )
    return 0


async def cmd_reconcile(args) -> int:
    """Compare the ledger against a stored broker-statement snapshot and
    report one trustworthy verdict (spec §7). This is the command the whole
    branch exists for -- see ledger/reconcile.py for the pure comparison and
    its Drift.verdict field, which is THE thing rendered below.
    """
    # The clock lives here, in the I/O layer -- same reasoning as
    # cmd_marks_set's and cmd_snapshot_add's identical comments.
    now = datetime.now(UTC)

    as_of = now
    if args.as_of:
        # Shared with cmd_snapshot_add -- see _parse_as_of. "2026-08-01" is the
        # ordinary way to name a statement date and must mean the same thing in
        # both commands: README.md's own worked example passes a bare date to
        # `snapshot add` on one line and to `reconcile` on the next, and before
        # the two parsers were unified the second line exited 2.
        parsed = _parse_as_of(args.as_of)
        if parsed is None:
            return 2
        as_of = parsed

    # Same InvalidOperation/is_finite guards cmd_marks_set and cmd_snapshot_add
    # already have for their own Decimal arguments.
    tolerance = Decimal("0.01")
    if args.tolerance is not None:
        try:
            tolerance = Decimal(args.tolerance)
        except InvalidOperation:
            print(
                f"error: --tolerance {args.tolerance!r} is not a valid number",
                file=sys.stderr,
            )
            return 2
        if not tolerance.is_finite():
            print(
                f"error: --tolerance {args.tolerance!r} must be a finite number",
                file=sys.stderr,
            )
            return 2
        # is_finite() above rejects NaN/Infinity but not a negative number.
        # A negative tolerance makes `abs(difference) <= tolerance`
        # unsatisfiable, so EVERY run -- even a perfectly reconciled account
        # -- would report DRIFT: a confidently wrong verdict produced from a
        # silently accepted bad input, the same failure class the
        # mixed-currency refusal below exists to avoid. Checked here, before
        # the pool is ever opened, same as every other argument guard above.
        if tolerance < 0:
            print(
                f"error: --tolerance {args.tolerance!r} must not be negative "
                "-- a negative tolerance would make every comparison read as drift",
                file=sys.stderr,
            )
            return 2

    account_id = UUID(args.account)

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            # 1. Resolve the account. Unknown id refuses, exit 2 -- same
            # get_account-then-check-None shape cmd_import already uses for
            # its own --account, rather than letting a foreign-key violation
            # surface later as a raw traceback.
            account = await get_account(conn, account_id)
            if account is None:
                print(f"error: no account with id {account_id}", file=sys.stderr)
                return 2

            # 2. latest_snapshot. None refuses, exit 2 -- reporting "zero
            # drift" against nothing is the silent-success shape this whole
            # command exists to avoid.
            snap_row = await latest_snapshot(conn, account_id, as_of)
            if snap_row is None:
                print(
                    f"error: no snapshot on or before {as_of.isoformat()} for "
                    f"account {account_id} -- record one with `snapshot add`",
                    file=sys.stderr,
                )
                return 2
            snapshot = Snapshot(
                account_id=account_id,
                as_of=snap_row["as_of"],
                cash_balance=snap_row["cash_balance"],
                total_equity=snap_row["total_equity"],
            )

            # 3-4. open_positions, then partition on unvaluable_reason --
            # NEVER on direction. A group can agree on a single direction and
            # still be unvaluable for another reason (ledger/positions.py's
            # own OpenPosition docstring), so a non-None direction here is not
            # a signal that pricing is safe.
            open_pos = await open_positions(conn, account_id)
            positions: list[Position] = []
            unvaluable: list[UnvaluableRef] = []
            for p in open_pos:
                if p.unvaluable_reason is None:
                    # ENFORCED, not merely documented. The comment on
                    # `direction=` below argues the invariant holds today, and
                    # it does -- but if it ever stops holding, nothing here
                    # notices: `None is Direction.SHORT` is simply False inside
                    # ledger/reconcile.py, so a direction-less position is
                    # valued as a LONG. That is the exact silent 2x-market-value
                    # equity error this branch just spent a fix wave removing,
                    # and it would return with the cash line still agreeing to
                    # the cent -- the shape that sends a reader hunting a
                    # phantom. This repo's precedent for that class of hazard is
                    # to crash rather than default (Instrument.__post_init__ on
                    # an option's contract_multiplier; Position.direction given
                    # no default at all).
                    #
                    # An explicit `raise`, NEVER `assert`: `python -O` strips
                    # asserts, and a guard that vanishes under an optimisation
                    # flag is exactly the one that must not.
                    if p.direction is None:
                        raise AssertionError(
                            f"open position {p.instrument_id} ({p.symbol}) has "
                            "unvaluable_reason None but direction None -- "
                            "ledger/positions.py must record an unvaluable_reason "
                            "for every position whose direction it leaves unset; "
                            "valuing it here would silently price a short as a long"
                        )
                    positions.append(
                        Position(
                            instrument_id=p.instrument_id,
                            quantity=p.quantity,
                            cost_basis=p.cost_basis,
                            multiplier=p.multiplier,
                            # Load-bearing, not bookkeeping: `quantity` is an
                            # unsigned magnitude for a short as much as a
                            # long, so reconcile() needs this to know a short
                            # SUBTRACTS from equity (see Position.direction).
                            # Dropping it here valued shorts as assets --
                            # equity wrong by twice the market value with the
                            # cash line agreeing exactly, since account_cash
                            # is already direction-aware.
                            #
                            # `p.direction` is Direction | None in general,
                            # but never None on this branch:
                            # ledger/positions.py appends an
                            # unvaluable_reason for BOTH cases that leave it
                            # unset (spread, mixed direction; :79-83 vs
                            # :110-112), so `unvaluable_reason is None`
                            # implies direction is exactly LONG or SHORT.
                            direction=p.direction,
                        )
                    )
                else:
                    unvaluable.append(
                        UnvaluableRef(
                            instrument_id=p.instrument_id,
                            symbol=p.symbol,
                            reason=p.unvaluable_reason,
                        )
                    )

            # 5. latest_marks for the valuable instrument ids only, mapped to
            # JUST the price. latest_marks returns a (price, timestamp) tuple
            # per instrument (db/marks.py) -- reconcile() wants a bare
            # Mapping[UUID, Decimal], so passing the tuple straight through
            # would misvalue every marked position. An instrument absent from
            # this dict (never present with a zero -- a genuine 0 mark is
            # legal) falls back to cost basis inside reconcile() itself.
            raw_marks = await latest_marks(conn, [p.instrument_id for p in positions])
            marks = {instrument_id: price for instrument_id, (price, _as_of) in raw_marks.items()}

            # 6. account_cash; MixedCurrencyError refuses, exit 2.
            try:
                computed_cash = await account_cash(conn, account_id)
            except MixedCurrencyError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
    finally:
        # See cmd_trades's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()

    # 7. reconcile() -> Drift. Pure, no I/O, no clock.
    drift = reconcile(
        snapshot, positions, marks, computed_cash, unvaluable=unvaluable, tolerance=tolerance
    )

    # 8. Render by verdict -- THE field callers render (see Drift's own
    # docstring). is_within_tolerance answers only "do the numbers agree" and
    # is a component, never the answer: rendering it alone would print a
    # clean pass on an account with unvalued positions.
    print(f"account {account_id}")
    # Two DIFFERENT clocks, deliberately labelled apart: drift.as_of is the
    # STATEMENT's date (snapshot.as_of, ledger/reconcile.py), but
    # computed_cash, open_positions and latest_marks above all read CURRENT
    # ledger state -- open_positions and latest_marks take no `as_of` at all.
    # A single "as of <statement date>" header above numbers that are
    # actually current would misrepresent a week of ordinary trading since
    # the statement as drift "as of" a date before any of it happened -- the
    # same phantom-hunt shape the unvaluable-exclusion message above exists
    # to prevent. `now` was captured at the very top of this function, so it
    # is the same instant the future-date guards above measured against.
    print(f"  statement as of {drift.as_of.isoformat()}")
    print(f"  ledger as of    {now.isoformat()}")
    print(f"  verdict: {drift.verdict.value}")
    print(
        f"  equity: computed {drift.computed_equity}  reported {drift.reported_equity}  "
        f"diff {drift.equity_difference}"
    )
    print(
        f"  cash:   computed {drift.computed_cash}  reported {drift.reported_cash}  "
        f"diff {drift.cash_difference}"
    )
    if drift.unmarked_instruments:
        print(
            f"  {len(drift.unmarked_instruments)} position(s) valued at cost basis -- "
            "no mark on file"
        )
    if drift.unvaluable_positions:
        # The output must explain the alarming number: computed_equity above
        # EXCLUDES these positions entirely (they were never turned into a
        # Position), so a large equity_difference here is expected, not
        # necessarily a defect -- saying so is the difference between a
        # useful report and a phantom hunt.
        print(
            f"  {len(drift.unvaluable_positions)} position(s) excluded from "
            "computed equity above (not included in the totals -- cannot be priced):"
        )
        for u in drift.unvaluable_positions:
            print(f"    {u.symbol}: {u.reason}")

    # Exhaustive on purpose: every verdict is matched by name and anything
    # unmatched crashes. The chain used to end in a bare `return 1` that
    # printed the UNRELIABLE narration, so deleting the DRIFT branch would
    # have relabelled every genuine drift as "could not be priced" -- a wrong
    # explanation attached to a real number -- with nothing failing. A future
    # ReconcileVerdict member would have inherited the same mislabelling
    # silently; now it fails loudly at the one place that has to be updated.
    if drift.verdict == ReconcileVerdict.OK:
        return 0
    elif drift.verdict == ReconcileVerdict.DRIFT:
        print("drift: the ledger and the statement disagree outside tolerance", file=sys.stderr)
        return 1
    elif drift.verdict == ReconcileVerdict.UNRELIABLE:
        print(
            "unreliable: one or more positions could not be priced, so this verdict "
            "cannot be trusted as a clean pass or a clean drift",
            file=sys.stderr,
        )
        return 1
    else:
        raise AssertionError(f"unhandled verdict {drift.verdict}")


# --- corporate actions (spec 2026-08-15, §5-§6) --------------------------
#
# `add` and `remove` PREVIEW by default and write only with --commit, mirroring
# `import`. Two-stage validation, deliberately:
#
#   1. everything decidable from the flags alone runs before any connection is
#      opened, so a mistyped ratio, ex-date or a spinoff with no basis
#      allocation never depends on the database to be refused;
#   2. the rest -- symbol resolution, CorporateAction.__post_init__ and the
#      duplicate check -- runs on a connection but strictly before any write.
#
# The full "build the action before opening the pool" shape is not reachable:
# `resulting_instrument_id` is a UUID only the database can supply. What the
# spec actually requires is that refusals write nothing and open no write
# transaction, and stage 2 preserves that.

# The types CorporateAction.__post_init__ requires a resulting instrument for.
# Named from the same three members that constructor checks, so the flag-level
# refusal below and the constructor cannot drift apart.
_RESULTING_INSTRUMENT_TYPES = {ActionType.MERGER, ActionType.SPINOFF, ActionType.SYMBOL_CHANGE}

# The one type that uses --basis-allocation, named for the same reason the set
# above is: CorporateAction.__post_init__ requires it for a spinoff and reads it
# for nothing else, so the flag-level refusal below cannot drift from it.
_BASIS_ALLOCATION_TYPES = {ActionType.SPINOFF}

def _parse_ex_date(raw: str) -> date | None:
    """`--ex-date` -> a plain date. NOT `_parse_as_of`: an ex-date is a
    calendar day the exchange declares, never a timestamp, and
    `corporate_action.ex_date` is a DATE column. Returns None after printing
    the refusal, the same shape `_parse_as_of` has."""
    try:
        return date.fromisoformat(raw)
    except ValueError:
        print(f"error: --ex-date {raw!r} is not a valid ISO-8601 date", file=sys.stderr)
        return None


def _parse_ratio(raw: str) -> tuple[Decimal, Decimal] | None:
    """`--ratio NEW:OLD` -> (ratio_numerator, ratio_denominator).

    That is the direction `adjust_fills` consumes -- a quantity is scaled by
    numerator / denominator -- so a 1-for-6 reverse split is `1:6` and takes
    1,800 shares to 300, and a 3-for-1 forward split is `3:1`. Stating it is
    not pedantry: inverting the pair turns that reverse split into a 6x forward
    split, leaving the position wrong by a factor of 36 with every individual
    step still looking plausible.

    Returns None after printing the refusal to stderr; the caller turns that
    into `return 2` -- the same shape `_parse_as_of` above already has.

    Decimal("abc") raises decimal.InvalidOperation, which does NOT descend from
    ValueError, and Decimal("NaN")/Decimal("Infinity") construct successfully
    and slip past that catch entirely: the same InvalidOperation/is_finite pair
    cmd_marks_set and cmd_snapshot_add already carry. The positivity check
    duplicates CorporateAction.__post_init__ on purpose -- the constructor
    cannot run until a connection has resolved the symbols, and a ratio of
    `1:0` is a typing mistake that should never need a database to be caught.
    """
    parts = raw.split(":")
    if len(parts) != 2:
        print(f"error: --ratio {raw!r} must be NEW:OLD, e.g. 1:6", file=sys.stderr)
        return None
    values: list[Decimal] = []
    for part in parts:
        try:
            value = Decimal(part)
        except InvalidOperation:
            print(f"error: --ratio component {part!r} is not a valid number", file=sys.stderr)
            return None
        if not value.is_finite():
            print(f"error: --ratio component {part!r} must be a finite number", file=sys.stderr)
            return None
        if value <= 0:
            print(f"error: --ratio component {part!r} must be positive", file=sys.stderr)
            return None
        values.append(value)
    return values[0], values[1]


def _print_effect(headline: str, preview: EffectPreview) -> None:
    """Render `db.corporate.preview_effect`'s CUMULATIVE diff (spec §5).

    Cumulative, not the proposed action against raw fills: the numbers below
    are what would change given everything already stored, which is the only
    framing that stays honest for interacting actions on one instrument.

    `_fmt_decimal` is reused from `positions` for the same reason it exists
    there -- an adjusted price is a division evaluated at 50 digits of
    precision, so an unbounded one would wrap the line off a terminal while
    asserting precision the inputs never had.
    """
    print(headline)
    if preview.fills_changed == 0:
        # Spec §6's last row: an action on an instrument with no fills (or none
        # before its ex-date) is ALLOWED -- a legitimately pre-recorded future
        # action -- so this reports that nothing is affected rather than
        # refusing. Saying so explicitly is what stops a silent, empty preview
        # from reading as a successful adjustment.
        print("  no fills affected")
        return
    print(f"  {preview.fills_changed} fill(s) affected across {preview.accounts} account(s)")
    for before, after in preview.samples:
        print(
            f"    {_fmt_decimal(before.quantity)} @ {_fmt_decimal(before.price)}"
            f"  ->  {_fmt_decimal(after.quantity)} @ {_fmt_decimal(after.price)}"
        )
    # A minted fill has no "before" half, so it cannot ride in the arrow form
    # above. Rendered on its own line rather than as "-- -> qty @ price": a
    # spinoff's child is the new POSITION the action creates, which is the most
    # visible thing about it, and the preview said nothing about it at all until
    # this line existed. The resulting symbol is not repeated here -- it is
    # already on the command line the user just typed, and preview_effect deals
    # in instrument ids, not symbols.
    for fill in preview.created:
        print(f"    new: {_fmt_decimal(fill.quantity)} @ {_fmt_decimal(fill.price)}")


async def _regroup_holders(conn, instrument_id: UUID) -> int:
    """Regroup EVERY account holding `instrument_id`; returns how many.

    Spec C7. Positions are read from materialised `trade` rows, so an action
    that is stored but never regrouped leaves every holder reporting a stale
    quantity -- silently, since nothing about the position says it predates the
    action. Every holder rather than the first: a corporate action is global
    (the table has no `account_id`, correctly -- a split affects every holder),
    so regrouping one account would leave the others pre-split.

    Called only from inside the caller's `async with conn.transaction()`, so
    the write and every regroup it invalidates commit or roll back together.
    """
    account_ids = [
        r["account_id"]
        for r in await conn.fetch(
            "SELECT DISTINCT account_id FROM fill WHERE instrument_id = $1", instrument_id
        )
    ]
    for account_id in account_ids:
        await regroup_account(conn, account_id)
    return len(account_ids)


async def cmd_corporate_add(args) -> int:
    action_type = ActionType(args.type)

    # --- stage 1: flag-level only, before any connection is opened ---
    ratio = _parse_ratio(args.ratio)
    if ratio is None:
        return 2
    numerator, denominator = ratio

    ex_date = _parse_ex_date(args.ex_date)
    if ex_date is None:
        return 2

    # Which flags each type USES. Both directions are checked, because a flag
    # the type does not use is not harmless:
    #
    #   `--type split --resulting-symbol ZXCB` used to be accepted and stored.
    #   `actions_with_ids_for_instruments` (db/corporate.py) matches on
    #   `resulting_instrument_id` as well as `instrument_id`, so that row joins
    #   ZXCB's action set, enters `_ordered_actions`' dependency graph, and can
    #   raise `ValueError: circular corporate-action dependency` out of
    #   `adjust_fills` -- inside `regroup_account`, for EVERY account holding
    #   either instrument, on every regroup including `import --commit`, naming
    #   neither the offending action nor how to remove it.
    if action_type in _RESULTING_INSTRUMENT_TYPES and args.resulting_symbol is None:
        print(f"error: --type {action_type} requires --resulting-symbol", file=sys.stderr)
        return 2
    if action_type is ActionType.SPINOFF and args.basis_allocation is None:
        print("error: --type spinoff requires --basis-allocation", file=sys.stderr)
        return 2
    if args.resulting_symbol is not None and action_type not in _RESULTING_INSTRUMENT_TYPES:
        print(
            f"error: --type {action_type} does not use --resulting-symbol (only "
            "merger, spinoff and symbol_change name a resulting instrument). Storing it "
            "anyway would put this action into the resulting instrument's own action set "
            "-- db/corporate.py matches on resulting_instrument_id -- where it can raise "
            "`circular corporate-action dependency` from inside every later regroup of "
            "every account holding either instrument, including `import --commit`.",
            file=sys.stderr,
        )
        return 2
    if args.basis_allocation is not None and action_type not in _BASIS_ALLOCATION_TYPES:
        print(
            f"error: --type {action_type} does not use --basis-allocation (only a "
            "spinoff moves a fraction of cost basis to another instrument). Storing it "
            "anyway would record a figure nothing reads, which `corporate list` then "
            "prints as though the basis had been reallocated.",
            file=sys.stderr,
        )
        return 2

    basis_allocation: Decimal | None = None
    if args.basis_allocation is not None:
        # Same InvalidOperation/is_finite pair as --ratio above, and the
        # is_finite half is load-bearing here: an ORDERING comparison against
        # a Decimal NaN raises InvalidOperation rather than returning False
        # (verified on this interpreter), so __post_init__'s `0 <= x <= 1`
        # range check would blow up with an exception that is not a ValueError
        # and would therefore escape the constructor's `except ValueError`
        # below as a traceback instead of a clean refusal.
        try:
            basis_allocation = Decimal(args.basis_allocation)
        except InvalidOperation:
            print(
                f"error: --basis-allocation {args.basis_allocation!r} is not a valid number",
                file=sys.stderr,
            )
            return 2
        if not basis_allocation.is_finite():
            print(
                f"error: --basis-allocation {args.basis_allocation!r} must be a finite number",
                file=sys.stderr,
            )
            return 2

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            # --- stage 2: needs a connection, still strictly before any write ---
            try:
                instrument_id = await resolve_instrument_by_symbol(conn, args.symbol)
                resulting_instrument_id = (
                    await resolve_instrument_by_symbol(conn, args.resulting_symbol)
                    if args.resulting_symbol
                    else None
                )
            except ValueError as exc:
                # resolve_instrument_by_symbol refuses an unknown symbol AND an
                # ambiguous one (instrument.symbol is not unique), naming every
                # candidate in the latter case -- reused rather than reinvented
                # so `corporate` and `marks set` cannot disagree about which
                # instrument a symbol means.
                print(f"error: {exc}", file=sys.stderr)
                return 2

            try:
                action = CorporateAction(
                    instrument_id=instrument_id,
                    action_type=action_type,
                    ex_date=ex_date,
                    ratio_numerator=numerator,
                    ratio_denominator=denominator,
                    resulting_instrument_id=resulting_instrument_id,
                    basis_allocation=basis_allocation,
                )
            except ValueError as exc:
                # The invariants only the database can decide -- chiefly that a
                # resulting instrument may not be the source instrument, which
                # needs both UUIDs. A clean message rather than the traceback
                # main() would otherwise let through (it deliberately does not
                # wrap domain ValueErrors).
                print(f"error: {exc}", file=sys.stderr)
                return 2

            existing = await find_duplicate(conn, instrument_id, ex_date, action_type)
            if existing is not None:
                # The same 1:6 split entered twice is a 1:36 restatement, and
                # every individual step of it looks plausible. There is no
                # UNIQUE constraint backing this (adding one is a migration and
                # out of scope), so this application-level guard is the only
                # thing standing between a double keypress and a silently
                # wrong position. The existing id is named so it can be
                # inspected with `corporate list` or dropped with
                # `corporate remove`.
                print(
                    f"error: a {action_type} on {args.symbol} with ex-date "
                    f"{ex_date.isoformat()} is already recorded as {existing}",
                    file=sys.stderr,
                )
                return 2

            # Computed BEFORE the write, always: previewing after storing the
            # action would diff the new state against itself and print "no
            # fills affected" for a change that had just been made.
            preview = await preview_effect(conn, instrument_id, adding=action)
            _print_effect(
                f"{args.symbol} — {action_type} {numerator}:{denominator}, "
                f"ex {ex_date.isoformat()}",
                preview,
            )

            if not args.commit:
                print("\npreview only — rerun with --commit to write")
                return 0

            # ONE transaction over the write and every regroup it invalidates:
            # a crash between them would otherwise leave a stored action whose
            # effect has reached some accounts' trades and not others.
            try:
                async with conn.transaction():
                    action_id = await add_action(conn, action, args.note)
                    regrouped = await _regroup_holders(conn, instrument_id)
            except TransferError as exc:
                # Recording this action re-adjusts every holder's fills, and a
                # stored transfer can become impossible against the adjusted
                # view (e.g. a mis-dated ex-date rescales the fills but not a
                # post-ex-date transfer). The transaction rolled back: nothing
                # was recorded. Refuse cleanly and name the conflict.
                print(
                    "error: recording this action makes a stored transfer "
                    f"impossible to apply -- {exc}; nothing was recorded",
                    file=sys.stderr,
                )
                return 2
    finally:
        # See cmd_trades's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited (including via every
        # early return above), never from inside it, or close() deadlocks
        # waiting for a release that will never come.
        await pool.close()

    print(f"recorded {action_id}; regrouped {regrouped} account(s)")
    return 0


async def cmd_corporate_list(args) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            instrument_id = None
            if args.symbol:
                try:
                    instrument_id = await resolve_instrument_by_symbol(conn, args.symbol)
                except ValueError as exc:
                    print(f"error: {exc}", file=sys.stderr)
                    return 2
            rows = await list_actions(conn, instrument_id)
            # The stored rows carry instrument UUIDs, and a listing of nothing
            # but UUIDs cannot do the job spec §5 gives this command -- finding
            # the id to hand to `corporate remove`, for the instrument you
            # meant. One lookup for the whole listing, rather than a join
            # pushed into db/corporate.py, which would make the storage layer
            # answer a display question.
            ids = {r["instrument_id"] for r in rows} | {
                r["resulting_instrument_id"] for r in rows if r["resulting_instrument_id"]
            }
            symbols = {
                r["id"]: r["symbol"]
                for r in await conn.fetch(
                    "SELECT id, symbol FROM instrument WHERE id = ANY($1::uuid[])", list(ids)
                )
            }
    finally:
        # See cmd_trades's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()

    for r in rows:
        line = (
            f"{r['id']}  {r['ex_date']:%Y-%m-%d}  "
            f"{symbols.get(r['instrument_id'], '?'):<8} {r['action_type']:<14} "
            f"{r['ratio_numerator']}:{r['ratio_denominator']}"
        )
        if r["resulting_instrument_id"] is not None:
            line += f"  -> {symbols.get(r['resulting_instrument_id'], '?')}"
        if r["basis_allocation"] is not None:
            line += f"  basis {r['basis_allocation']}"
        print(line)
    return 0


async def cmd_corporate_remove(args) -> int:
    # main()'s UUID guard covers --account only, so a mistyped id would
    # otherwise reach asyncpg as a bad bind and surface as a traceback. Parsed
    # before the pool: whether it is a UUID depends only on the argument.
    try:
        action_id = UUID(args.id)
    except ValueError as exc:
        print(f"error: {args.id!r} is not a valid corporate action id: {exc}", file=sys.stderr)
        return 2

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            # Fetched first for two reasons at once: an unknown id must be
            # refused before anything is deleted (db.corporate.remove_action's
            # own docstring -- False means unknown, and the caller refuses
            # rather than reporting a successful no-op), and both the preview
            # and the regroup below need the instrument the action applies to.
            row = await conn.fetchrow(
                "SELECT action_type, ex_date, instrument_id FROM corporate_action WHERE id = $1",
                action_id,
            )
            if row is None:
                print(f"error: no corporate action with id {action_id}", file=sys.stderr)
                return 2
            instrument_id = row["instrument_id"]

            preview = await preview_effect(conn, instrument_id, removing=action_id)
            _print_effect(
                f"removing {action_id}: {row['action_type']}, "
                f"ex {row['ex_date']:%Y-%m-%d}",
                preview,
            )

            if not args.commit:
                print("\npreview only — rerun with --commit to write")
                return 0

            try:
                async with conn.transaction():
                    # remove_action returns False for an unknown id; that case
                    # was already refused above, on this same connection, so
                    # the only way to see it here is a concurrent deleter --
                    # not a case worth a second refusal path inside an open
                    # transaction, where `return` would COMMIT rather than
                    # roll back.
                    await remove_action(conn, action_id)
                    regrouped = await _regroup_holders(conn, instrument_id)
            except TransferError as exc:
                # The mirror of cmd_corporate_add's refusal: undoing an action
                # can equally strand a stored transfer against the re-adjusted
                # view. Rolled back; nothing was removed.
                print(
                    "error: removing this action makes a stored transfer "
                    f"impossible to apply -- {exc}; nothing was removed",
                    file=sys.stderr,
                )
                return 2
    finally:
        # See cmd_trades's identical comment: pool.close() must run after the
        # `async with pool.acquire()` block has exited, never from inside it,
        # or close() deadlocks waiting for a release that will never come.
        await pool.close()

    print(f"removed {action_id}; regrouped {regrouped} account(s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="deadband")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending database migrations").set_defaults(fn=cmd_migrate)

    p_accounts = sub.add_parser("accounts")
    p_accounts.set_defaults(fn=cmd_accounts)
    accounts_sub = p_accounts.add_subparsers(dest="accounts_command")
    p_accounts_add = accounts_sub.add_parser("add", help="create a new account")
    p_accounts_add.add_argument("--name", required=True)
    p_accounts_add.add_argument("--venue", required=True)
    p_accounts_add.add_argument(
        "--account-type", required=True, choices=["cash", "margin", "funded", "wallet"]
    )
    p_accounts_add.add_argument("--external-ref", default=None)
    p_accounts_add.add_argument(
        "--default-intent", default="trade", choices=["trade", "investment", "mixed"]
    )
    p_accounts_add.add_argument(
        "--ignore-on-import",
        action="store_true",
        help=(
            "skip this account's rows on import instead of refusing the whole "
            "commit for an account you don't intend to import (e.g. a "
            "retirement plan with no instrument identity)"
        ),
    )
    p_accounts_add.set_defaults(fn=cmd_accounts_add)

    p_import = sub.add_parser("import", help="parse a venue export")
    p_import.add_argument("venue", choices=list_importers())
    p_import.add_argument("file")
    p_import.add_argument(
        "--account",
        help=(
            "account UUID for rows with no venue-supplied account ref. "
            "Coinbase never carries one. Fidelity is dialect-dependent: the "
            "Activity & Orders export carries its own per-row account "
            "number and routes automatically, but the multi-year History "
            "export -- the only dialect that contains corporate actions -- "
            "has no account column at all and needs this just as much as "
            "Coinbase does"
        ),
    )
    p_import.add_argument("--commit", action="store_true", help="write to the database")
    p_import.add_argument(
        "--check-duplicates",
        action="store_true",
        help=(
            "preview only: open a READ-ONLY database connection and report "
            "how many rows are already present. Plain preview (without this "
            "flag) deliberately never touches the database at all -- this is "
            "an explicit opt-in exception, not a change to preview's default"
        ),
    )
    p_import.set_defaults(fn=cmd_import)

    p_sync = sub.add_parser("sync", help="fetch from a venue API and import")
    p_sync.add_argument("venue", choices=["coinbase"])
    p_sync.add_argument(
        "--account", required=True, help="account UUID: the API carries no per-row account ref"
    )
    p_sync.add_argument("--start", help="ISO-8601 lower bound on sequence_timestamp")
    p_sync.add_argument("--end", help="ISO-8601 upper bound on sequence_timestamp")
    p_sync.add_argument("--commit", action="store_true", help="write to the database")
    p_sync.set_defaults(fn=cmd_sync)

    p_regroup = sub.add_parser("regroup")
    p_regroup.add_argument("--account", required=True)
    p_regroup.set_defaults(fn=cmd_regroup)

    p_trades = sub.add_parser("trades")
    p_trades.add_argument("--account")
    p_trades.set_defaults(fn=cmd_trades)

    p_transfers = sub.add_parser("transfers", help="outbound asset transfers (ACAT)")
    transfers_sub = p_transfers.add_subparsers(dest="transfers_cmd", required=True)
    p_transfers_list = transfers_sub.add_parser("list", help="list stored transfers")
    p_transfers_list.add_argument("--account")
    p_transfers_list.set_defaults(fn=cmd_transfers)

    p_positions = sub.add_parser(
        "positions", help="open positions, with unrealized P&L where marked"
    )
    p_positions.add_argument("--account")
    p_positions.set_defaults(fn=cmd_positions)

    p_marks = sub.add_parser("marks", help="manual price marks")
    marks_sub = p_marks.add_subparsers(dest="marks_command", required=True)
    p_marks_set = marks_sub.add_parser("set", help="record a price mark for an instrument")
    group = p_marks_set.add_mutually_exclusive_group(required=True)
    group.add_argument("--symbol", help="instrument symbol; refused if it is ambiguous")
    group.add_argument("--natural-key", help="exact instrument natural key")
    # The unit matters and is not guessable: for an option the correct input
    # is the per-share premium (2.50), not the per-contract cost (250). The
    # contract multiplier is applied downstream by unrealized_pnl, so entering
    # the per-contract figure produces a silently 100x wrong unrealized P&L.
    p_marks_set.add_argument(
        "--price",
        required=True,
        help=(
            "price per unit, excluding the contract multiplier, "
            "in the instrument's quote currency"
        ),
    )
    p_marks_set.add_argument(
        "--as-of", default=None, help="ISO-8601 timestamp; defaults to now (UTC)"
    )
    p_marks_set.set_defaults(fn=cmd_marks_set)

    p_fills = sub.add_parser("fills", help="hand-entered fills")
    fills_sub = p_fills.add_subparsers(dest="fills_cmd", required=True)
    p_fills_add = fills_sub.add_parser("add", help="record a fill by hand")
    p_fills_add.add_argument("--account", required=True)
    p_fills_add.add_argument("--symbol", required=True)
    p_fills_add.add_argument("--side", required=True, choices=["buy", "sell"])
    p_fills_add.add_argument("--quantity", required=True)
    p_fills_add.add_argument("--price", required=True)
    p_fills_add.add_argument("--fee", default="0")
    p_fills_add.add_argument("--fee-currency", default="USD")
    p_fills_add.add_argument("--executed-at", required=True, help="ISO-8601 instant")
    p_fills_add.set_defaults(fn=cmd_fills_add)
    p_fills_rm = fills_sub.add_parser("rm", help="delete a hand-entered fill")
    p_fills_rm.add_argument("--id", required=True)
    p_fills_rm.set_defaults(fn=cmd_fills_rm)

    p_snapshot = sub.add_parser("snapshot", help="broker statement figures")
    snap_sub = p_snapshot.add_subparsers(dest="snapshot_command", required=True)
    p_snap_add = snap_sub.add_parser("add", help="record a statement's equity and cash")
    p_snap_add.add_argument("--account", required=True)
    p_snap_add.add_argument("--as-of", required=True, help="ISO-8601 date or timestamp")
    p_snap_add.add_argument("--equity", required=True, help="total equity the broker reports")
    p_snap_add.add_argument("--cash", required=True, help="cash balance the broker reports")
    p_snap_add.add_argument("--note", default=None)
    p_snap_add.set_defaults(fn=cmd_snapshot_add)

    p_reconcile = sub.add_parser(
        "reconcile", help="compare the ledger against a statement snapshot"
    )
    p_reconcile.add_argument("--account", required=True)
    p_reconcile.add_argument("--as-of", default=None, help="ISO-8601; defaults to now")
    p_reconcile.add_argument("--tolerance", default=None, help="default 0.01")
    p_reconcile.set_defaults(fn=cmd_reconcile)

    p_corp = sub.add_parser("corporate", help="corporate actions")
    corp_sub = p_corp.add_subparsers(dest="corporate_command", required=True)

    p_corp_add = corp_sub.add_parser("add", help="record a corporate action")
    # Drawn from ActionType rather than a literal list, so a new member of the
    # enum is offered by the CLI instead of being silently unreachable.
    p_corp_add.add_argument("--type", required=True, choices=[t.value for t in ActionType])
    p_corp_add.add_argument("--symbol", required=True, help="refused if it is ambiguous")
    p_corp_add.add_argument("--ex-date", required=True, help="ISO-8601 date")
    # The direction is not guessable and inverting it is a factor-of-36 error
    # on a 1:6 reverse split with every step still looking plausible, so it is
    # spelled out here as well as in _parse_ratio.
    p_corp_add.add_argument(
        "--ratio",
        required=True,
        help="NEW:OLD — a quantity is scaled by NEW/OLD, so a 1-for-6 reverse split is 1:6",
    )
    p_corp_add.add_argument(
        "--resulting-symbol",
        default=None,
        help="the instrument produced; required for merger, spinoff and symbol_change",
    )
    p_corp_add.add_argument(
        "--basis-allocation",
        default=None,
        help="spinoff only: fraction of cost basis moved to the spun-off instrument (0-1)",
    )
    p_corp_add.add_argument("--note", default=None)
    p_corp_add.add_argument("--commit", action="store_true", help="write to the database")
    p_corp_add.set_defaults(fn=cmd_corporate_add)

    p_corp_list = corp_sub.add_parser("list", help="show stored corporate actions")
    p_corp_list.add_argument("--symbol", default=None)
    p_corp_list.set_defaults(fn=cmd_corporate_list)

    p_corp_rm = corp_sub.add_parser("remove", help="delete a corporate action")
    p_corp_rm.add_argument("id")
    p_corp_rm.add_argument("--commit", action="store_true", help="write to the database")
    p_corp_rm.set_defaults(fn=cmd_corporate_remove)

    args = parser.parse_args()
    # `import --commit` no longer requires --account at parse time: whether
    # it's needed depends on whether the parsed file has any row with no
    # account ref to route by, which isn't known until the file is read (see
    # cmd_import). Enforced there instead, before any database connection is
    # opened.

    # A malformed --account UUID is a genuine user-input mistake, same class as
    # a typo'd file path below — but it must be told apart from a domain
    # invariant violation (Fill.__post_init__, group_fills' id=None rejection,
    # instrument_natural_key, CorporateAction.__post_init__) that also happens
    # to raise ValueError. Parsing it here, before the try/except, means the
    # broad `except ValueError` below is never needed (and never added back) to
    # catch it.
    if getattr(args, "account", None):
        try:
            UUID(args.account)
        except ValueError as exc:
            print(f"error: --account is not a valid UUID: {exc}", file=sys.stderr)
            return 2

    # A typo'd file path is the most likely first mistake a user makes; a raw
    # traceback for it is unfriendly and can leak a full local path, so
    # OSError (FileNotFoundError et al.) gets a clean one-line message.
    # Everything else — including every domain invariant violation above, and
    # anything from the database layer — is deliberately left unwrapped, so a
    # real bug surfaces as a full traceback (with line numbers and the
    # offending row) instead of being disguised as a clean user error.
    try:
        return asyncio.run(args.fn(args))
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
