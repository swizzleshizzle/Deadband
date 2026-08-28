"""Parsing for values that arrive over HTTP, shared by every write route.

One home, deliberately. cli.py's `_parse_as_of` docstring records what
happened when `snapshot add` and `reconcile` each carried their own
near-identical copy: they drifted, and the README's own documented two-line
invocation exited 2 on its second line. The API has four routes that parse the
same three kinds of value; they get one parser, not four.

Every refusal here is an HTTPException(422) rather than a ValueError, because
every caller is a FastAPI handler and the alternative is each of them wrapping
these in try/except to say the same thing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException

from db.marks import MARK_FUTURE_TOLERANCE


def parse_decimal(raw: str, field: str) -> Decimal:
    """Exact decimal or 422. Mirrors cmd_marks_set's guards exactly.

    Two separate hazards, both real: Decimal("abc") raises InvalidOperation,
    which does NOT descend from ValueError, so a bare `except ValueError`
    lets it escape as a 500. And Decimal("NaN")/Decimal("Infinity")
    CONSTRUCT successfully and slip past that catch entirely -- is_finite()
    is this codebase's established second check (importers/fidelity.py,
    importers/coinbase_api.py, cli.py). Without it a NaN reaches
    account_snapshot, which has no CHECK constraints to stop it.
    """
    try:
        value = Decimal(raw)
    except InvalidOperation:
        raise HTTPException(422, f"{field}: {raw!r} is not a valid number") from None
    if not value.is_finite():
        raise HTTPException(422, f"{field}: {raw!r} must be a finite number")
    return value


def parse_instant(raw: str, field: str) -> datetime:
    """A timestamp that must carry a UTC offset. Bare dates are NOT accepted.

    Matches `marks set`, which takes a timestamp only -- widening it to bare
    dates would be a behaviour change, not a convenience (cli.py's
    _parse_as_of says so in as many words about cmd_marks_set).

    The offset requirement is not pedantry: comparing an offset-naive
    datetime against an offset-aware one downstream raises a raw TypeError
    ("can't compare offset-naive and offset-aware datetimes") that never
    reaches a clean refusal.
    """
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        raise HTTPException(422, f"{field}: {raw!r} is not a valid timestamp") from None
    if value.tzinfo is None:
        raise HTTPException(
            422, f"{field}: {raw!r} has no UTC offset (e.g. append +00:00 or Z)"
        )
    return value


def parse_as_of(raw: str, field: str) -> datetime:
    """A bare date becomes midnight UTC; a timestamp is taken as written.

    The API-side twin of cli.py's `_parse_as_of`, and it must stay that way:
    `snapshot add` accepts the bare date the README's worked example passes,
    so the form over it has to as well.

    The property the fallthrough depends on is that `date.fromisoformat`
    REJECTS anything carrying a time component -- verified on 3.12 for
    "2026-07-31T12:00", "2026-07-31 12:00" and "2026-07-31T12:00+00:00", all
    ValueError. That, not "it accepts only YYYY-MM-DD", is what makes this
    sound: since 3.11 it also accepts "20260801" and "2026-W31-1", both
    legitimate ways to name a day that correctly become midnight UTC. What
    would break it is a time-carrying string being swallowed by the first
    branch and never reaching parse_instant's offset guard, and that cannot
    happen.

    A bare date is exempt from the offset requirement because it is GIVEN
    UTC here, rather than implying an unnamed wall-clock zone the way a bare
    timestamp does.
    """
    try:
        return datetime.combine(date.fromisoformat(raw), time.min, tzinfo=UTC)
    except ValueError:
        pass
    return parse_instant(raw, field)


def refuse_future(value: datetime, now: datetime, field: str) -> None:
    """Refuse an as_of beyond MARK_FUTURE_TOLERANCE ahead of `now`.

    `now` is a parameter rather than read here so the caller's single
    `datetime.now(UTC)` anchors both the omitted-as_of default and this
    guard, and the two measure against the exact same instant -- the same
    reason cmd_marks_set takes its clock at the top of the function.
    """
    if value > now + MARK_FUTURE_TOLERANCE:
        raise HTTPException(
            422,
            f"{field}: {value.isoformat()} is in the future "
            f"(tolerance: {MARK_FUTURE_TOLERANCE})",
        )
