"""The History dialect: no Account/Account Number columns, a Cash Balance
column instead. Every existing fixture before this file was Activity & Orders
-- the only dialect the multi-year exports (and every corporate action) use.

See tests/fixtures/fidelity/real_shape_history.csv for the fixture's shape:
a BOM, two blank preamble lines, the header on line 3, a trailing legal
disclaimer block -- and, unique to this file, the corporate-action row
shapes: a reverse split and a name change as FROM/TO pairs, a three-row
merger, a single-row spinoff distribution, a cash-in-lieu-of-fractional-
shares row, and -- since Task 2 (importer-blocking-verbs) -- a plain
single-row DISTRIBUTION (a share distribution, proposed as a "split"). All
values are fabricated.

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
from datetime import date
from decimal import Decimal

from importers.fidelity import FidelityImporter, _reor_base
from ledger.types import Side

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "fidelity"
FIXTURE = _FIXTURE_DIR / "real_shape_history.csv"


def _read_fixture(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")


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

    Asserted instead on the EXACT count the fixture's four ordinary rows (one
    BUY, one dividend, and -- since Task 1 -- one retirement rollover deposit
    and one early-distribution withdrawal) produce: one fill, three cash
    movements. The other ten rows -- the reverse split pair, the
    name-change pair, the three-row merger, the spinoff, the cash-in-lieu
    row, and -- since Task 2 -- the plain DISTRIBUTION share-distribution
    row -- are recognised and deferred, not recorded. A count is falsifiable
    in both directions: it fails if a corporate-action row starts producing
    a fill or cash movement, and it fails if the ordinary rows stop
    producing theirs."""
    batch = _batch()
    assert len(batch.fills) == 1
    # 4, not 3, since branch B: the fixture's ACAT pair adds a transfer_out
    # cash residual (an ordinary row, not corporate-action output) alongside
    # the share leg counted in batch.transfers below.
    assert len(batch.cash) == 4
    assert len(batch.transfers) == 1

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
    rows are one event -- not on inference from date and CUSIP.

    Five kinds, not four, since Task 2 (importer-blocking-verbs): the
    fixture's plain DISTRIBUTION row (no #REOR token, grouped by the
    (ex-date, symbol) fallback key) now also proposes a "split"."""
    kinds = [p.kind for p in _batch().corporate_actions]
    assert sorted(kinds) == ["merger", "name_change", "reverse_split", "spinoff", "split"]


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


# --- Task 2: a plain DISTRIBUTION is a share distribution ------------------


def test_a_plain_distribution_is_proposed_as_a_split():
    """A DISTRIBUTION with no SPINOFF marker delivers SHARES, not money --
    the export's Amount column on this row is the market value of the shares
    received, verified against the real exports by cash-balance continuity.
    So it belongs to the split family, and the ratio is NOT derivable from
    the row: the row states what was received, never what it was received
    on. cli completes that from the ledger (Task 3)."""
    batch = _batch()
    splits = [p for p in batch.corporate_actions if p.kind == "split"]
    assert len(splits) == 1
    p = splits[0]
    assert p.ex_date == date(2026, 3, 6)
    assert p.quantities == (Decimal("40"),)
    assert p.ratio is None, "not derivable from the row alone"
    assert p.subject_symbol == "ZXDS"
    assert not [m for _, m in batch.blocking if "DISTRIBUTION" in m]


def test_a_spinoff_is_still_a_spinoff_not_a_share_distribution():
    """Ordering guard. classify() is startswith + first-match-wins and
    "DISTRIBUTION" is a proper prefix of "DISTRIBUTION SPINOFF", so a
    share_distribution rule placed BEFORE spinoff_distribution silently
    reclassifies every spinoff in every export. This is unlike the existing
    corporate-action block, whose comment records that its position in RULES
    is not load-bearing -- that comment does not cover this rule."""
    batch = _batch()
    kinds = [p.kind for p in batch.corporate_actions]
    assert "spinoff" in kinds
    assert kinds.count("split") == 1


def test_a_distribution_with_no_quantity_still_blocks():
    """D5. A DISTRIBUTION carrying zero quantity has never been observed in
    the real exports, and proposing a split derived from no shares would be
    a guess dressed as a derivation. Two observed rows is thin evidence to
    generalise a verb from; this guard is what keeps the generalisation
    honest.

    Asserts the SPECIFIC reject message the D5 guard itself produces, not a
    looser "mentions DISTRIBUTION or the date" check -- the looser form would
    pass on any blocking message that merely mentions the verb, including one
    produced by an unrelated row, so it could pass while the guard did
    nothing.

    The extra row is joined onto the fixture text without leaving a blank
    CSV line: the fixture (unlike a hand-written one) does not end in a
    trailing newline, so `.rstrip("\\n")` before appending "\\n" + the row
    keeps the join from producing an empty row, which would otherwise yield
    an extra unmapped warning and perturb the very counts this test reads.
    """
    extra_row = (
        "03/07/2026,DISTRIBUTION ZXDS HOLDINGS SPON ADS EA... (ZXDS) (Cash),"
        "ZXDS,ZXDS HOLDINGS SPON ADS EACH REP 1 ORD SHS,Cash,,0,,,,25,3903.55,"
    )
    base = FIXTURE.read_text(encoding="utf-8")
    text = base.rstrip("\n") + "\n" + extra_row + "\n"
    # The fixture's own disclaimer block already contains blank lines
    # (paragraph breaks) -- a blanket "no blank line anywhere" check would
    # be wrong. What must hold is that THIS join introduces no NEW one.
    assert text.count("\n\n") == base.count("\n\n"), "join must not introduce a blank CSV line"

    batch = FidelityImporter().parse(text)
    assert len([p for p in batch.corporate_actions if p.kind == "split"]) == 1
    assert any("no positive quantity" in m for _, m in batch.blocking)


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


def test_a_rollover_cash_check_is_a_deposit():
    """A retirement rollover is cash arriving from outside the ledger. It
    carries a zero quantity and a real amount, which is exactly the shape
    that blocks an import while unmapped -- three such rows across the real
    exports are why two accounts could not be imported."""
    batch = _batch()
    deposits = [c for c in batch.cash if c.kind == "deposit" and c.amount == Decimal("1500")]
    assert len(deposits) == 1
    assert deposits[0].occurred_at.date() == date(2026, 3, 4)
    assert not [m for _, m in batch.blocking if "ROLLOVER" in m]


def test_an_early_distribution_is_a_withdrawal():
    """Money leaving a retirement account. Recorded as a positive amount
    under an outflow kind -- see importers.base.OUTFLOW_KINDS, which is why
    the export's own negative sign must NOT leak through."""
    batch = _batch()
    withdrawals = [
        c for c in batch.cash if c.kind == "withdrawal" and c.amount == Decimal("500")
    ]
    assert len(withdrawals) == 1
    assert withdrawals[0].amount > 0, "OUTFLOW_KINDS amounts are always positive"
    assert not [m for _, m in batch.blocking if "EARLY DIST" in m]


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


# --- Task 4: broker amendment clusters ------------------------------------
#
# A Fidelity amendment is THREE rows: the original trade, a `BUY CANCEL ...
# CANCELLED TRADE as of <date>` reversing it, and a `YOU BOUGHT ...
# CORRECTED CONFIRM as of <date>` re-booking it with the corrected figures.
# The net truth is ONE trade, on the as-of date, at the corrected fee.
#
# tests/fixtures/fidelity/amendment_cluster.csv holds that cluster plus the
# real closing sell that falls chronologically BETWEEN the original and its
# two amendment legs -- the ordering hazard is not reproducible without it,
# since the amendment legs carry a Run Date nineteen days after the trade
# they amend.
#
# Every figure in that fixture is fabricated. The brief's own draft values
# were not: its prices, amounts and cash balances matched a real cluster in
# the (gitignored) imports/ directory field-for-field, so they were replaced
# with invented ones that appear nowhere in it. See the task report.


def test_an_amendment_cluster_nets_to_one_fill():
    """Original -> cancel -> correction is ONE buy, at the corrected fee, on
    the as-of date. Asserting the FILL COUNT is the point: before this
    existed the importer emitted a third fill for this contract, because
    classify() returns None for the CORR row but the dedicated YOU BOUGHT
    branch matched it anyway. A test that only asserted the import stops
    refusing would pass while the duplicate persisted."""
    batch = FidelityImporter().parse(_read_fixture("amendment_cluster.csv"))
    buys = [f for f in batch.fills if f.side is Side.BUY]
    sells = [f for f in batch.fills if f.side is Side.SELL]
    assert len(buys) == 1, "the cancelled original and its cancel both vanish"
    assert len(sells) == 1
    assert buys[0].executed_at.date() == date(2026, 1, 2), "dated to the as-of, not the run date"
    assert buys[0].fee == Decimal("0.68"), "0.65 commission + the CORRECTED 0.03"
    assert not batch.blocking


def test_a_netting_is_reported():
    """A netting that happens silently is indistinguishable from rows being
    dropped."""
    batch = FidelityImporter().parse(_read_fixture("amendment_cluster.csv"))
    assert any("netted" in w.lower() for w in batch.warnings)


def test_a_cancel_with_no_matching_original_is_not_netted():
    """D4: degrade to blocking, never to guessing. The matcher is fitted to a
    single real cluster, so refusing to act is the failure mode it is allowed
    to have."""
    lines = _read_fixture("amendment_cluster.csv").splitlines()
    header, rows = lines[0], lines[1:]
    # drop the original (last row) -- the cancel now matches nothing
    text = "\n".join([header] + rows[:-1]) + "\n"
    batch = FidelityImporter().parse(text)
    assert any("CANCEL" in m for _, m in batch.blocking)


def test_an_ambiguous_match_is_not_netted():
    """Two identical originals mean the cancel cannot say which it reverses.
    Ambiguous is treated as no match."""
    lines = _read_fixture("amendment_cluster.csv").splitlines()
    text = "\n".join(lines + [lines[-1]]) + "\n"   # duplicate the original
    batch = FidelityImporter().parse(text)
    assert any("CANCEL" in m for _, m in batch.blocking)


# The action text carries the security's NAME, not just its symbol, so a bare
# `CORR` substring test would fire on any holding whose name happens to
# contain those four letters -- CORRIDOR, CORRECTIONS, CorEnergy's own
# `CORR` ticker. The real exports already carry ordinary option trades with
# an `as of` token (as well as REINVESTMENT, FEE CHARGED and DIVIDEND
# RECEIVED rows with one), so "carries an as-of" is nowhere near enough to
# identify an amendment leg on its own: only the two-word phrase is.
#
# This is the one hazard no other test in this file can see, because it costs
# nothing while no such name is held -- and then silently suppresses a real
# original the first time one is. Under a bare-`CORR` match the decoy below
# is mistaken for the cluster's missing correction, the cancel and the
# original are both suppressed, and the import stops complaining about a
# cancel it never reconciled.


def test_a_row_merely_containing_corr_is_not_a_correction():
    lines = _read_fixture("amendment_cluster.csv").splitlines()
    header, rows = lines[0], lines[1:]
    decoy = (
        "01/16/2026,YOU BOUGHT OPENING TRANSACTION CALL (ZXCO) ZXCO CORRIDOR "
        "TRUST JAN 10 26 $300 (100 SHS) (Cash) as of 2026-01-02,"
        "-ZXCO260110C300,CALL (ZXCO) ZXCO CORRIDOR TRUST JAN 10 26 $300 "
        "(100 SHS),Cash,3.15,2,0.65,0.04,,-630.69,746.75,01/20/2026"
    )
    assert "CORR" in decoy and "CORRECTED CONFIRM" not in decoy
    # rows[1:] drops the genuine correction, so the only row left that
    # contains "CORR" is the decoy.
    text = "\n".join([header] + rows[1:] + [decoy]) + "\n"

    batch = FidelityImporter().parse(text)
    assert any("CANCEL" in m for _, m in batch.blocking), (
        "with no genuine correction the cluster must not net, and its cancel "
        "must keep blocking"
    )
    decoy_fills = [f for f in batch.fills if f.quantity == Decimal("2")]
    assert len(decoy_fills) == 1
    assert decoy_fills[0].executed_at.date() == date(2026, 1, 16), (
        "an ordinary trade keeps its own Run Date -- it is not a correction "
        "to be re-dated to the as-of it happens to carry"
    )


# --- Fix round 1: two ways the matcher could still fail OPEN ---------------
#
# Both were found by review, not by the tests above, and both end in a
# duplicated or vanished fill rather than a refusal -- which is the one
# outcome D4 does not allow. The four rows of the cluster fixture are
# recombined by hand here rather than given fixtures of their own: what is
# under test is the MATCHER's arithmetic on leg combinations, and a file per
# combination would put four near-identical CSVs in the tree with the real
# distinction buried in a money column.


def _cluster_rows() -> tuple[str, str, str, str, str]:
    """(header, correction, cancel, sell, original) from the shipped cluster
    fixture, in the file's own row order."""
    lines = _read_fixture("amendment_cluster.csv").splitlines()
    header, correction, cancel, sell, original = lines
    assert "CORRECTED CONFIRM" in correction and "CANCELLED TRADE" in cancel
    return header, correction, cancel, sell, original


def test_one_correction_cannot_be_consumed_by_two_cancels():
    """Cancels are keyed on the FULL (symbol, as-of, |quantity|, price)
    tuple; corrections on (symbol, as-of) alone. Two cancels differing only
    in price are therefore two distinct cancel entries that each look up the
    SAME correction, each see exactly one of it, and each net -- consuming
    one correction twice and deleting a second, unrelated original outright.

    Uniqueness inside each leg's own bucket is not uniqueness ACROSS the
    cancels competing for one correction, and the tell that the guard is
    missing is two netting notes naming the same correction line. Four
    money-carrying rows collapsed to one fill here, with nothing blocking.
    """
    header, correction, cancel, sell, original = _cluster_rows()
    # A second, independent pair on the same symbol and as-of date, at a
    # different price. Fabricated, like every other figure in this file.
    original_2 = original.replace(
        ",2.5,1,0.65,0.12,,-250.77,903.11,", ",3.5,1,0.65,0.12,,-350.77,553.11,"
    )
    cancel_2 = cancel.replace(
        ",2.5,-1,0.65,-0.12,,250.77,1628.21,", ",3.5,-1,0.65,-0.12,,350.77,1978.98,"
    )
    assert original_2 != original and cancel_2 != cancel, "the price edit must have applied"
    text = (
        "\n".join([header, correction, cancel, cancel_2, sell, original, original_2]) + "\n"
    )

    batch = FidelityImporter().parse(text)

    assert not [w for w in batch.warnings if w.startswith("netted an amendment cluster")], (
        "ambiguous across cancels is still ambiguous -- nothing may net"
    )
    assert [m for _, m in batch.blocking if "CANCEL" in m], "the cancels must block"
    assert [m for _, m in batch.blocking if "CORRECTED CONFIRM" in m], (
        "and so must the correction they could not agree on"
    )
    # Nothing was deleted: both originals and the sell are still fills, which
    # is what fails when one cancel nets away an original it never reversed.
    assert len([f for f in batch.fills if f.side is Side.BUY]) == 2
    assert len([f for f in batch.fills if f.side is Side.SELL]) == 1


def test_a_correction_with_no_cancel_blocks_instead_of_duplicating():
    """The third degrade-to-blocking case, and the only one that used to fail
    OPEN rather than closed.

    An unplaced CANCEL or original falls through to the unmapped path, which
    blocks on its own. A CORRECTED CONFIRM does not: it leads with
    "YOU BOUGHT", so the dedicated trade branch claims it before classify()
    is ever consulted and emits a fill. A lone correction therefore produced
    a SECOND fill for the trade it exists to restate -- three fills, zero
    warnings, zero blocking, and no way for a human to know.

    This is the exact defect the whole task was written to close, surviving
    in the one arrangement the netting pass declines to act on.
    """
    header, correction, cancel, sell, original = _cluster_rows()
    text = "\n".join([header, correction, sell, original]) + "\n"

    batch = FidelityImporter().parse(text)

    assert [m for _, m in batch.blocking if "CORRECTED CONFIRM" in m]
    # The original and the sell still import; only the correction is refused.
    assert len(batch.fills) == 2
    assert [f for f in batch.fills if f.side is Side.BUY][0].fee == Decimal("0.77"), (
        "the surviving buy is the ORIGINAL, at its own fee -- not the "
        "correction masquerading as one"
    )


# --- Fix round 2 (Finding 3): an AS OF date that isn't a real calendar date -
#
# _AS_OF_RE (importers/fidelity.py) validates the SHAPE of the as-of token --
# four digits, two digits, two digits -- but not the calendar: "AS OF
# 2026-02-30" matches it. date.fromisoformat() used to run on that match
# unguarded, raising ValueError out of parse() itself -- a crash, where D4's
# posture is "degrade to blocking, never to guessing" for every other
# malformed value this module encounters. A corrupted or hand-edited export
# date is exactly the malformed-but-plausible shape this whole task exists to
# survive.


def test_an_as_of_date_that_fails_the_calendar_does_not_crash_the_parser():
    """The fix treats this exactly like the sibling `except ValueError:
    continue` a few lines above it (a candidate original's unparsable Run
    Date): the row is simply not nettable, and falls through to the ordinary
    row loop. CANCELLED TRADE matches no rule in RULES, so an un-netted one
    already takes the "unhandled action" path there -- reject() -- which
    names the row's own action text (so the bad date is visible to a human)
    and blocks it, since it carries a real (nonzero) quantity.
    """
    header, correction, cancel, sell, original = _cluster_rows()
    bad_cancel = cancel.replace("as of 2026-01-02", "as of 2026-02-30")
    assert bad_cancel != cancel, "the date edit must have applied"
    text = "\n".join([header, correction, bad_cancel, sell, original]) + "\n"

    batch = FidelityImporter().parse(text)  # must not raise

    assert not [w for w in batch.warnings if w.startswith("netted an amendment cluster")], (
        "an unparseable as-of date must not net -- there is no valid date to "
        "key the match on"
    )
    assert any("2026-02-30" in message for _, message in batch.blocking), (
        "the malformed row must be named, not merely dropped"
    )


def test_a_valid_as_of_date_still_nets_after_the_calendar_guard():
    """Regression guard for the fix above: catching an invalid calendar date
    must not quietly disable amendment handling for every OTHER, valid one.
    Same fixture and same assertions as test_an_amendment_cluster_nets_to_one_fill,
    kept as a second, independent check tied specifically to this fix."""
    header, correction, cancel, sell, original = _cluster_rows()
    text = "\n".join([header, correction, cancel, sell, original]) + "\n"

    batch = FidelityImporter().parse(text)

    buys = [f for f in batch.fills if f.side is Side.BUY]
    assert len(buys) == 1, "the cancelled original and its cancel both vanish"
    assert buys[0].executed_at.date() == date(2026, 1, 2), "dated to the as-of, not the run date"
    assert any("netted" in w.lower() for w in batch.warnings)
    assert not batch.blocking
