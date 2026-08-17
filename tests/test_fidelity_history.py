"""The History dialect: no Account/Account Number columns, a Cash Balance
column instead. Every existing fixture before this file was Activity & Orders
-- the only dialect the multi-year exports (and every corporate action) use.

See tests/fixtures/fidelity/real_shape_history.csv for the fixture's shape:
a BOM, two blank preamble lines, the header on line 3, a trailing legal
disclaimer block -- and, unique to this file, the corporate-action row
shapes: a reverse split and a name change as FROM/TO pairs, a three-row
merger, a single-row spinoff distribution, and a cash-in-lieu-of-fractional-
shares row. All values are fabricated.

The `#REOR` reorganisation reference scheme, verified against the real
exports (not invented -- spec §5 forbids guessing at a format): a
reorganisation reference is a shared BASE plus a per-leg trailing digit, e.g.
`M9990000010001` and `M9990000010000` are two legs of ONE event because they
share the base `M999000001`, not because the two full references are equal.
Rows belonging to one event share the base; they do NOT carry one identical
reference. Checked directly against the real exports: 15 referenced rows, 13
distinct full references, but only 7 distinct bases once the trailing digit
is dropped. Observed leg digits are 0, 1, 2, and 4 -- NOT a contiguous run
from zero, and no guarantee one exists for every event, so a grouping parser
(Task 2) must not assume a leg digit predicts its position within the event
or that all of 0..n are present. This fixture's own rows follow that same
base+leg scheme (e.g. the reverse split's `M9990000010001`/`M9990000010000`
share base `M999000001`; the merger's three legs share base `M999000003`),
so Task 2 can be built and tested against it directly.
"""

import pathlib
from decimal import Decimal

from importers.fidelity import FidelityImporter, _reor_base
from ledger.types import Side

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "fidelity" / "real_shape_history.csv"


def _batch():
    return FidelityImporter().parse(FIXTURE.read_text(encoding="utf-8"))


def test_the_history_dialect_parses_at_all():
    """The multi-year history export is a different dialect from every existing
    fixture -- no Account/Account Number columns, a Cash Balance column instead.
    Nothing covered it before this file, which is why external_ref=None went
    unnoticed."""
    batch = _batch()
    assert batch.fills
    assert batch.cash


def test_history_rows_carry_no_account_ref():
    """Not a defect -- the account is in the filename, not the file. Pinned
    because `import --account` is the only way these rows route, and a future
    change that started inventing a ref would silently route them somewhere."""
    batch = _batch()
    assert {f.external_ref for f in batch.fills} == {None}
    assert batch.refs_seen == ()


def test_corporate_action_rows_do_not_block_the_import():
    """Gap #33's actual acceptance test. These rows carry a nonzero quantity or
    amount and matched no rule before this task, so the money-carrying-unmapped
    policy blocked the whole import -- which is why two accounts could never be
    imported."""
    assert _batch().blocking == ()


def test_corporate_action_rows_produce_exactly_the_ordinary_rows_worth_of_output():
    """Ruling B: the brief's original version of this test asserted that no
    fill's venue_fill_id contains "REOR" -- which passes trivially, since
    corporate-action rows produce no fills at all (this importer never sets
    venue_fill_id anywhere), and would keep passing even if a future change
    started turning them into fills, which is exactly the regression this test
    exists to catch.

    Asserted instead on the EXACT count the fixture's two ordinary rows (one
    BUY, one dividend) produce: one fill, one cash movement. The other nine
    rows -- the reverse split pair, the name-change pair, the three-row
    merger, the spinoff, and the cash-in-lieu row -- are recognised and
    deferred, not recorded. A count is falsifiable in both directions: it
    fails if a corporate-action row starts producing a fill or cash movement,
    and it fails if the ordinary rows stop producing theirs."""
    batch = _batch()
    assert len(batch.fills) == 1
    assert len(batch.cash) == 1

    fill = batch.fills[0]
    assert fill.instrument.symbol == "ZXCO"
    assert fill.side is Side.BUY
    assert fill.quantity == Decimal("50")

    cash = batch.cash[0]
    assert cash.kind == "dividend"
    assert cash.amount == Decimal("6.25")


def test_reor_base_matches_the_verified_docstring_examples():
    """Pins the #REOR base extraction against what this file's own module
    docstring (above) states was checked directly against the real exports:
    a reference's suffix is three constant zeros plus one varying digit, so
    two legs of one event share every character except the last."""
    assert _reor_base("M9990000010001") == _reor_base("M9990000010000") == "M999000001000"
    assert (
        _reor_base("M9990000030002")
        == _reor_base("M9990000030001")
        == _reor_base("M9990000030000")
        == "M999000003000"
    )


def test_each_reorganisation_becomes_one_proposal():
    """Grouping is on the venue's own #REOR reference -- Fidelity stating which
    rows are one event -- not on inference from date and CUSIP."""
    kinds = [p.kind for p in _batch().corporate_actions]
    assert sorted(kinds) == ["merger", "name_change", "reverse_split", "spinoff"]


def test_the_three_row_merger_is_one_proposal_not_three():
    """A merger arrives as three rows. Grouping on the REOR reference handles
    that without a special case; grouping on (date, cusip) would not."""
    merger = next(p for p in _batch().corporate_actions if p.kind == "merger")
    assert len(merger.quantities) == 3


def test_the_single_row_spinoff_is_one_proposal():
    """A spinoff has no negative leg -- it adds the child without removing the
    parent. Gap #33 and the previous design both call these FROM/TO pairs; that
    is true of the other three types and false of this one."""
    spinoff = next(p for p in _batch().corporate_actions if p.kind == "spinoff")
    assert len(spinoff.quantities) == 1


def test_reverse_split_cusip_pair_is_old_to_new_not_inverted():
    """ZXC000001 is the pre-split entity and ZXC000002 the post-split one --
    verified by cross-referencing each row's own ISIN in its Description
    against its paren-adjacent CUSIP in the fixture text. An earlier version
    of this code read the token before #REOR (the FROM/TO verb's
    COUNTERPARTY, not the row's own entity) and reported this pair
    backwards."""
    rs = next(p for p in _batch().corporate_actions if p.kind == "reverse_split")
    assert rs.source_cusip == "ZXC000001"
    assert rs.resulting_cusip == "ZXC000002"


def test_name_change_cusip_pair_is_old_to_new_not_inverted():
    """Same defect, same fix, the other two-row shape."""
    nc = next(p for p in _batch().corporate_actions if p.kind == "name_change")
    assert nc.source_cusip == "ZXC000002"
    assert nc.resulting_cusip == "ZXC000003"


def test_merger_source_resolves_but_resulting_stays_blank():
    """The PAYOUT row is the merger's single negative leg, so its
    paren-adjacent CUSIP resolves source_cusip unambiguously -- the earlier
    (before-#REOR-token) reading had this as None because PAYOUT rows never
    carry a token in that position. resulting_cusip stays None: the two FROM
    legs go to two DIFFERENT resulting companies, so there is no single
    value to report -- spec §7 wants that blank, never a guess."""
    merger = next(p for p in _batch().corporate_actions if p.kind == "merger")
    assert merger.source_cusip == "ZXC000001"
    assert merger.resulting_cusip is None


def test_reverse_split_description_is_captured_verbatim():
    """Task 3 parses the stated ratio out of this text (spec §6a) -- if
    grouping normalised, truncated, or re-cased it, that parse would lose
    its input. Pinned against the fixture's own text, including its
    original casing and punctuation ("#", "/"), so a regression that
    upper-cases or strips characters cannot pass silently."""
    fixture_text = FIXTURE.read_text(encoding="utf-8")
    row7_description = (
        "ZEPHYR EXPLORATION CO COM ISIN #ZX0000000013 SEDOL #BZX0002 "
        "1 FOR 3 R/S INTO ZEPHYR EXPLORATION CO"
    )
    assert row7_description in fixture_text, "fixture text this test anchors on has changed"

    rs = next(p for p in _batch().corporate_actions if p.kind == "reverse_split")
    assert row7_description in rs.description


def _fixture_with_a_stray_reorganisation_leg() -> str:
    """The real fixture text plus one extra corporate-action row whose #REOR
    reference pairs with nothing else in the file -- a lone leg, so its group
    has the wrong row count for its kind (one row where reverse_split expects
    two) and must be reported as unrecognised rather than coerced.

    Built by string manipulation on the fixture text, not a fourth fixture
    file. The base `M999000004` (see the module docstring's #REOR scheme) is
    new -- 004, where the real rows use 001-003 -- so it cannot collide with
    an existing group. All values fabricated, same ZXCO/ZXC00000N family the
    rest of this fixture uses.
    """
    text = FIXTURE.read_text(encoding="utf-8")
    stray_row = (
        '05/20/2024,REVERSE SPLIT R/S FROM ZXC000006#REOR M9990000040000 '
        'ZEPHYR EXPLORATION CO COM (ZXC000007) (Cash),"",ZEPHYR EXPLORATION '
        'CO COM ISIN #ZX0000000099 SEDOL #BZX0099,Cash,"",3,"","","",0.00,'
        '992.03,""\n'
    )
    marker = (
        '05/12/2024,MERGER MER PAYOUT #REOR M9990000030000 ZEPHYR '
        'EXPLORATION CO COM (ZXC000001) (Cash),"",ZEPHYR EXPLORATION CO COM '
        'ISIN #ZX0000000013 SEDOL #BZX0002 *REORGANIZATION*,Cash,"","-26",'
        '"","","","-14.22",992.03,""\n'
    )
    assert marker in text, "fixture row this helper anchors on has changed"
    return text.replace(marker, marker + stray_row, 1)


def test_a_group_of_an_unexpected_shape_is_reported_not_coerced():
    """Forcing an unknown shape into the nearest match is how a wrong ratio gets
    proposed with confidence."""
    batch = FidelityImporter().parse(_fixture_with_a_stray_reorganisation_leg())
    assert any("unrecognised" in w.lower() for w in batch.warnings)


def test_cash_in_lieu_is_reported_separately_from_the_proposals():
    """It moves real cash and needs gap #35's arithmetic. Listing it beside the
    proposals would imply an action the user can record."""
    batch = _batch()
    assert batch.cash_in_lieu
    assert not any(p.kind == "cash_in_lieu" for p in batch.corporate_actions)


# --- Task 3: derivation --------------------------------------------------
#
# NOTE on the reverse-split expectation below: the brief that accompanied this
# task asserted (Decimal(1), Decimal(6)). That is wrong for this fixture --
# the reverse split here is 51 shares -> 17 shares, and its own description
# (pinned above by test_reverse_split_description_is_captured_verbatim) says
# "1 FOR 3 R/S", not "1 FOR 6". gcd(17, 51) == 17, so 17/17 : 51/17 reduces to
# 1:3, agreeing with the stated text exactly. (Decimal(1), Decimal(6)) would
# fail against this fixture's own data and was never run against it -- the
# fixture came after the brief's text was written. Derived here from the
# fixture and the export, not from the brief.


def test_a_reverse_split_ratio_is_derived_and_reduced():
    """NEW:OLD, reduced to the smallest integer pair -- the direction
    adjust_fills consumes. Inverting it turns a reverse split into a forward
    one and is wrong by the square of the ratio. 17 new : 51 old reduces to
    1:3, which also agrees with the "1 FOR 3 R/S" stated in the description
    (spec §6a) -- two independent sources agreeing is the strongest evidence
    available that this is right."""
    split = next(p for p in _batch().corporate_actions if p.kind == "reverse_split")
    assert split.ratio == (Decimal(1), Decimal(3))
    assert split.approximate is False


def test_a_name_change_ratio_is_one_to_one():
    change = next(p for p in _batch().corporate_actions if p.kind == "name_change")
    assert change.ratio == (Decimal(1), Decimal(1))
    assert change.ratio_source == "constant"


def test_a_spinoff_carries_no_ratio_out_of_the_importer():
    """Not derivable from the file: the row carries only the child shares, and
    the ratio needs the parent holding at the ex-date. cli.py completes it."""
    spinoff = next(p for p in _batch().corporate_actions if p.kind == "spinoff")
    assert spinoff.ratio is None


def test_a_merger_with_two_different_resulting_entities_carries_no_ratio():
    """This fixture's merger (spec §5) has ONE negative leg (26 shares given
    up) and TWO positive legs of two DIFFERENT resulting companies (9 shares
    of one, 4 of another) -- summing 9+4 into "13 new shares" would be adding
    shares of two unrelated securities together and reporting a ratio against
    them as if they were fungible, which is exactly the confidently-wrong
    number this design exists to prevent. test_merger_source_resolves_but_
    resulting_stays_blank already established resulting_cusip is None for the
    identical reason (no single resulting entity); ratio must follow the same
    "ambiguous -> blank, never a guess" rule, not silently pick one leg or
    sum across entities."""
    merger = next(p for p in _batch().corporate_actions if p.kind == "merger")
    assert merger.ratio is None


def test_every_proposal_keeps_the_quantities_it_derived_from():
    """The ratio is an inference; the quantities are the evidence. A reverse
    split whose quantities do not reduce cleanly -- the cash-in-lieu case -- is
    exactly when a human needs to see both."""
    for proposal in _batch().corporate_actions:
        assert proposal.quantities


def _fixture_with_a_fractional_split() -> str:
    """The real fixture text with the reverse split's TO-leg quantity changed
    from -51 to -50 -- one share's worth short of an exact 1:3 conversion, as
    if a fractional remainder were paid out as cash in lieu instead of
    converting. The description text is left untouched, so it still states
    "1 FOR 3 R/S" -- only the quantity evidence changes, which is exactly the
    disagreement spec §6a says the proposal must surface.

    Built by string manipulation on the fixture text, not a fourth fixture
    file. All values fabricated, same ZXCO/ZXC00000N family the rest of this
    fixture uses.
    """
    text = FIXTURE.read_text(encoding="utf-8")
    original_line = (
        '03/10/2024,REVERSE SPLIT R/S TO ZXC000002#REOR M9990000010000 ZEPHYR '
        'EXPLORATION CO COM ISIN #ZX0000000013 1 FOR 3 R/S INTO ZEPHYR '
        'EXPLORATION CO (ZXC000001) (Cash),"",ZEPHYR EXPLORATION CO COM ISIN '
        '#ZX0000000013 SEDOL #BZX0002 1 FOR 3 R/S INTO ZEPHYR EXPLORATION CO,'
        'Cash,"","-51","","","",0.00,1006.25,""\n'
    )
    assert original_line in text, "fixture row this helper anchors on has changed"
    amended_line = original_line.replace('"-51"', '"-50"')
    return text.replace(original_line, amended_line, 1)


def test_a_ratio_that_does_not_reduce_cleanly_is_flagged():
    """Fractional remainders are paid out as cash in lieu, so raw quantities
    need not be an exact multiple. Silently rounding would propose a confident
    wrong ratio. gcd(17, 50) == 1, so the derived pair (17, 50) disagrees with
    the "1 FOR 3" stated in the description -- that disagreement, not the
    gcd reduction alone, is what flags this as approximate (see spec §6a:
    reducing a coprime pair by their own gcd always "succeeds" trivially, so
    only a second, independent source can show the result is wrong)."""
    batch = FidelityImporter().parse(_fixture_with_a_fractional_split())
    split = next(p for p in batch.corporate_actions if p.kind == "reverse_split")
    assert split.approximate is True
    assert split.ratio == (Decimal(17), Decimal(50))
    assert any(
        "disagrees" in w.lower() and "reverse_split" in w.lower()
        for w in batch.warnings
    ), "disagreement between stated and derived ratio must be surfaced in warnings"


def test_stated_and_derived_ratios_agree_for_every_paired_action_in_the_fixture():
    """The real fixture's reverse split states "1 FOR 3" in its description
    and its quantities (17, 51) reduce to the same 1:3 -- the two independent
    sources spec §6a wants cross-checked. Pinning the agreement here means a
    future change to either the parser or the fixture that breaks the
    cross-check shows up as a red test, not as a silently-preferred number."""
    batch = _batch()
    split = next(p for p in batch.corporate_actions if p.kind == "reverse_split")
    assert split.ratio_source == "derived"
    assert split.approximate is False
    assert not any("disagrees" in w.lower() for w in batch.warnings)
