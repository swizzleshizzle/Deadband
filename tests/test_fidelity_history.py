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
reference.

Re-derived (final fix wave) with the SHIPPED regex, `importers/fidelity.py`'s
`_REOR_RE` = `#REOR\\s+(\\S+)`, applied to every line of every real export --
naming the regex because the figures previously recorded here came from
ad-hoc patterns that were never reconciled against the code's own, and were
all four wrong:

* **11** rows carry a reference this regex matches. (13 lines contain the
  string `#REOR` at all; the other two are the `#REORL...` cash-in-lieu
  spelling, which has no whitespace after `#REOR` and so does not match --
  harmless, since cash-in-lieu rows are never grouped.)
* **11** distinct full references -- i.e. every matched row has its own,
  never two rows sharing one string. That is the finding this whole scheme
  rests on, and it is what makes equality-on-the-full-token unusable.
* **5** distinct bases once the trailing character is dropped, i.e. five real
  reorganisation events across five years.
* Observed leg digits are **0, 1 and 2**, and within each event they ARE a
  contiguous run from zero (four events of `0,1`, one of `0,1,2`). An
  earlier version of this docstring claimed a digit `4` and "NOT contiguous";
  no `4` occurs. The caution still stands as a caution -- five events is far
  too small a sample to promise contiguity, and nothing in the grouping code
  relies on it -- but it is stated here as unverified, not as observed.

This fixture's own rows follow that same base+leg scheme (e.g. the reverse
split's `M9990000010001`/`M9990000010000` share base `M999000001`; the
merger's three legs share base `M999000003`), so Task 2 can be built and
tested against it directly.

The fixture's CUSIP tokens (`99900Z101` and friends) are fabricated, but
carry the real SHAPE: nine alphanumerics. An earlier fixture used
`ZXC000001`, letters-then-digits, and the parser's pattern was fitted to it
-- matching zero of the 15 corporate-action rows in the real exports, so the
CUSIP-orientation tests below were green against a token form the parser
never actually met.
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
    """99900Z101 is the pre-split entity and 99900Z209 the post-split one --
    verified by cross-referencing each row's own ISIN in its Description
    against its paren-adjacent CUSIP in the fixture text. An earlier version
    of this code read the token before #REOR (the FROM/TO verb's
    COUNTERPARTY, not the row's own entity) and reported this pair
    backwards."""
    rs = next(p for p in _batch().corporate_actions if p.kind == "reverse_split")
    assert rs.source_cusip == "99900Z101"
    assert rs.resulting_cusip == "99900Z209"


def test_name_change_cusip_pair_is_old_to_new_not_inverted():
    """Same defect, same fix, the other two-row shape."""
    nc = next(p for p in _batch().corporate_actions if p.kind == "name_change")
    assert nc.source_cusip == "99900Z209"
    assert nc.resulting_cusip == "99900Z307"


def test_the_cusip_pattern_matches_the_shape_the_real_exports_actually_use():
    """The token shape, pinned directly on the pattern rather than only
    through the fixture -- this is the assertion that would have caught the
    defect the fixture could not.

    A CUSIP is nine alphanumerics. The pattern shipped before the final fix
    wave required letters-then-digits, fitted to a fabricated `ZXC000001`,
    and matched ZERO of the 15 corporate-action rows in the real exports:
    `source_cusip`/`resulting_cusip` were therefore always None in
    production while three orientation tests certified them. Both real
    orderings are asserted here -- digits-first (every real row) and
    letters-first (a CINS, a foreign issuer's CUSIP) -- so re-narrowing to
    either one reddens this.

    The negative cases are what the paren anchor buys: "(Cash)" appears on
    EVERY row of this dialect, and a parenthesised bare ticker appears on
    real spinoff, name-change and cash-in-lieu rows. Neither is nine
    characters, so neither can be mistaken for an identifier.
    """
    from importers.fidelity import _PAREN_CUSIP_RE

    assert _PAREN_CUSIP_RE.findall("COM (POST REV SPLIT) (99900Z209) (Cash)") == ["99900Z209"]
    assert _PAREN_CUSIP_RE.findall("SOME FOREIGN CO (G9900Z101) (Cash)") == ["G9900Z101"]
    assert _PAREN_CUSIP_RE.findall("MERGER MER PAYOUT (Cash)") == []
    assert _PAREN_CUSIP_RE.findall("DISTRIBUTION SPINOFF FROM:(ZXCO ) (Cash)") == []
    # Eight and ten characters are not CUSIPs and must not match either --
    # "nine" is the rule, not "long enough".
    assert _PAREN_CUSIP_RE.findall("(99900Z10) (99900Z1010) (Cash)") == []


def test_merger_source_resolves_but_resulting_stays_blank():
    """The PAYOUT row is the merger's single negative leg, so its
    paren-adjacent CUSIP resolves source_cusip unambiguously -- the earlier
    (before-#REOR-token) reading had this as None because PAYOUT rows never
    carry a token in that position. resulting_cusip stays None: the two FROM
    legs go to two DIFFERENT resulting companies, so there is no single
    value to report -- spec §7 wants that blank, never a guess."""
    merger = next(p for p in _batch().corporate_actions if p.kind == "merger")
    assert merger.source_cusip == "99900Z101"
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
    an existing group. All values fabricated, same ZXCO/999xxZnnn CUSIP family the
    rest of this fixture uses.
    """
    text = FIXTURE.read_text(encoding="utf-8")
    stray_row = (
        '05/20/2024,REVERSE SPLIT R/S FROM 99900Z600#REOR M9990000040000 '
        'ZEPHYR EXPLORATION CO COM (99900Z708) (Cash),"",ZEPHYR EXPLORATION '
        'CO COM ISIN #ZX0000000099 SEDOL #BZX0099,Cash,"",3,"","","",0.00,'
        '992.03,""\n'
    )
    marker = (
        '05/12/2024,MERGER MER PAYOUT #REOR M9990000030000 ZEPHYR '
        'EXPLORATION CO COM (99900Z101) (Cash),"",ZEPHYR EXPLORATION CO COM '
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
    """It moves real cash that nothing applies (gap #43, the same arithmetic
    gap #35 tracks one layer up for merger cash). Listing it beside the
    proposals would imply an action the user can record."""
    batch = _batch()
    assert batch.cash_in_lieu
    assert not any(p.kind == "cash_in_lieu" for p in batch.corporate_actions)


def test_a_spinoff_carries_the_parent_ticker_its_own_row_states():
    """"DISTRIBUTION SPINOFF FROM:(ZXCO )" -- the parent is a fact the row
    supplies, with the CHILD (ZXCWS) in the Symbol column. Before the final
    fix wave nothing captured it, and cli.py had to identify the parent by
    elimination instead: "the account's sole LONG holding at the ex-date",
    which is ambiguous on every real account checked (gap #47 as corrected).

    Asserted as the PARENT, not merely as "some symbol": the row's own
    Symbol column holds the child, so a change that read that column instead
    would still produce a plausible-looking ticker and a ratio computed
    against the wrong instrument."""
    spinoff = next(p for p in _batch().corporate_actions if p.kind == "spinoff")
    assert spinoff.parent_symbol == "ZXCO"
    assert spinoff.parent_symbol != "ZXCWS"


def test_only_a_spinoff_carries_a_parent_ticker():
    """Every other kind identifies its sides by CUSIP and quantity sign. A
    name change's resulting leg and a cash-in-lieu row both carry a
    parenthesised ticker of their own in the real exports, and neither is a
    spinoff parent -- reading one as such would hand cli.py a parent for an
    action that has none."""
    for proposal in _batch().corporate_actions:
        if proposal.kind != "spinoff":
            assert proposal.parent_symbol is None


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


def test_stated_ratio_does_not_mistake_a_clock_time_for_a_ratio():
    """Spec §6a (corrected): every digit:digit match across the real exports
    -- all 11 of them -- turned out to be the "Date downloaded ... HH:MM pm"
    footer timestamp, not a ratio; the form "N:N" does not occur at all. A
    colon-form parser would therefore have no real occurrence to justify it,
    and would actively misfire on ordinary time-like text. Calls
    _parse_stated_ratio directly (not through a fixture row) so this holds
    regardless of whether such text ever reaches a real corporate-action
    description -- the exposing input the reviewer named."""
    from importers.fidelity import _parse_stated_ratio

    assert _parse_stated_ratio("PAYOUT SETTLED AT 02:31 PM (Cash)") is None
    assert _parse_stated_ratio("Date downloaded 09/12/2026 02:31 pm") is None
    # The one form that IS real still parses, so this isn't just "always None".
    assert _parse_stated_ratio("1 FOR 3 R/S INTO ZEPHYR EXPLORATION CO") == (
        Decimal(1),
        Decimal(3),
    )


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
    sum across entities.

    Beyond this fixture's particular shape, it is structural (spec §6,
    corrected): a merger's group is always exactly 3 rows
    (_EXPECTED_LEG_COUNT), while deriving a ratio requires exactly 1
    negative and 1 positive row -- 2 rows total. 3 != 2, so no merger this
    importer recognises can ever produce a derived ratio. ratio_source
    reflects that too -- None, not 'derived', since nothing was derived."""
    merger = next(p for p in _batch().corporate_actions if p.kind == "merger")
    assert merger.ratio is None
    assert merger.ratio_source is None


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
    file. All values fabricated, same ZXCO/999xxZnnn CUSIP family the rest of this
    fixture uses.
    """
    text = FIXTURE.read_text(encoding="utf-8")
    original_line = (
        '03/10/2024,REVERSE SPLIT R/S TO 99900Z209#REOR M9990000010000 ZEPHYR '
        'EXPLORATION CO COM ISIN #ZX0000000013 1 FOR 3 R/S INTO ZEPHYR '
        'EXPLORATION CO (99900Z101) (Cash),"",ZEPHYR EXPLORATION CO COM ISIN '
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
    cross-check shows up as a red test, not as a silently-preferred number.

    ratio_source == 'derived+confirmed', NOT the bare 'derived' a
    single-source (no stated text found) case also produces -- that
    distinction is the whole point of this test. Deleting the
    _parse_stated_ratio call entirely would still leave ratio == (1, 3) and
    approximate == False (nothing to disagree with), so THIS assertion, not
    those, is what proves the cross-check actually ran rather than merely
    never having found anything to disagree with."""
    batch = _batch()
    split = next(p for p in batch.corporate_actions if p.kind == "reverse_split")
    assert split.ratio_source == "derived+confirmed"
    assert split.approximate is False
    assert not any("disagrees" in w.lower() for w in batch.warnings)
    assert split.stated_ratio == (Decimal(1), Decimal(3))


def test_a_disputed_ratio_carries_both_candidates_not_just_the_derived_one():
    """The disagreement case, pinned on the PROPOSAL rather than only on a
    warning string. Two things were wrong before the final fix wave, and both
    were invisible from this fixture until someone ran the real exports:

    * ratio_source was the bare 'derived', whose consumer-facing sentence
      says "no independent confirmation was found in the venue's own text".
      Confirmation WAS found here -- the description states "1 FOR 3" -- and
      it disagreed. 'derived+disputed' is a distinct value precisely so that
      sentence can stop being false on every real reverse split.
    * The stated ratio existed only inside a warning string bound for
      stderr, so the one number needed to adjudicate the disagreement never
      reached the stdout section a user acts on (D5).

    Asserting both members of the pair, not just their inequality: a
    regression that carried the DERIVED ratio into `stated_ratio` would keep
    "two candidates present" true while making them the same number twice.
    """
    batch = FidelityImporter().parse(_fixture_with_a_fractional_split())
    split = next(p for p in batch.corporate_actions if p.kind == "reverse_split")
    assert split.ratio_source == "derived+disputed"
    assert split.ratio == (Decimal(17), Decimal(50))
    assert split.stated_ratio == (Decimal(1), Decimal(3))
    assert split.approximate is True


def test_a_single_source_ratio_is_plain_derived_with_no_stated_candidate():
    """The other side of the distinction above: a reverse split whose
    description states no ratio at all. 'derived' must mean exactly this --
    one source existed -- and `stated_ratio` must be None rather than a
    copy of the derived pair, or the consumer would print two "independent"
    candidates that are one number wearing two hats."""
    text = _fixture_with_a_fractional_split()
    # Strip the stated ratio out of the TO leg's Description only. The
    # quantities are untouched, so the derived pair is still (17, 50).
    text = text.replace("1 FOR 3 R/S INTO ZEPHYR EXPLORATION CO", "R/S INTO ZEPHYR EXPLORATION CO")
    batch = FidelityImporter().parse(text)
    split = next(p for p in batch.corporate_actions if p.kind == "reverse_split")
    assert split.ratio == (Decimal(17), Decimal(50))
    assert split.ratio_source == "derived"
    assert split.stated_ratio is None
    assert split.approximate is False


def test_a_zero_over_zero_stated_ratio_does_not_crash_the_parse():
    """`_reduce_ratio`'s `divisor == 0` guard is load-bearing, not dead
    code. `_reduce_ratio(*stated)` is called on text-parsed values, so a
    description containing "0 FOR 0" reaches it directly from the venue's own
    text -- gcd(0, 0) is 0, and without the guard the reduction divides by
    zero and raises out of parse(), which spec §7 forbids (degrade, never
    fail). Exercised both at the unit and through a whole file, since the
    guard only matters because the end-to-end path can reach it."""
    from importers.fidelity import _reduce_ratio

    assert _reduce_ratio(Decimal(0), Decimal(0)) == (Decimal(0), Decimal(0))

    text = FIXTURE.read_text(encoding="utf-8").replace(
        "1 FOR 3 R/S INTO ZEPHYR EXPLORATION CO", "0 FOR 0 R/S INTO ZEPHYR EXPLORATION CO"
    )
    batch = FidelityImporter().parse(text)
    split = next(p for p in batch.corporate_actions if p.kind == "reverse_split")
    # The quantities still say 1:3; the venue's own text now says nothing
    # usable, so the two disagree -- reported, not crashed, and not rounded.
    assert split.ratio == (Decimal(1), Decimal(3))
    assert split.ratio_source == "derived+disputed"


def test_a_non_finite_corporate_action_quantity_warns_instead_of_crashing():
    """Decimal("NaN") CONSTRUCTS fine, so it slips past the branch's
    `except InvalidOperation` and only detonates later, on the `< 0` / `> 0`
    comparisons in _derive_cusip_pair and _derive_quantity_ratio -- raising
    out of parse() itself. This was the only numeric branch in the importer
    without the `is_finite()` guard its fill and cash twins both have (and
    which migration 002_reject_non_finite_numerics.sql exists for).

    The whole import must still succeed (spec §7: degrade, never fail), the
    row must be named in a warning rather than silently dropped, and the
    group it belonged to must NOT be proposed off its surviving leg alone --
    a one-legged "reverse split" is exactly the confidently-wrong proposal
    _EXPECTED_LEG_COUNT exists to refuse."""
    text = FIXTURE.read_text(encoding="utf-8").replace('"-51"', '"NaN"')
    batch = FidelityImporter().parse(text)

    assert any("non-finite" in w.lower() for w in batch.warnings)
    assert not any(p.kind == "reverse_split" for p in batch.corporate_actions)
    # The rest of the file is unaffected -- one bad row, not one bad import.
    assert any(p.kind == "merger" for p in batch.corporate_actions)
    assert batch.blocking == ()
