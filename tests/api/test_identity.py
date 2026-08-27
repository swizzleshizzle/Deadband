"""Tailnet identity on write routes.

Every login here is INVENTED. The real allowlist lives in the deployment's
gitignored env file -- the values are personal email addresses and this
repository is public.
"""

import pytest
from fastapi import HTTPException

from api.identity import require_trusted_identity


class _Headers:
    """Minimal stand-in for Starlette's Headers: case-insensitive, and able
    to hold more than one value per name so the multi-header case is real
    rather than shaped to fit a dict-backed stub."""

    def __init__(self, items):
        self._items = [(k.lower(), v) for k, v in items]

    def get(self, key, default=None):
        key = key.lower()
        for k, v in self._items:
            if k == key:
                return v
        return default

    def getlist(self, key):
        key = key.lower()
        return [v for k, v in self._items if k == key]


class _Req:
    """Minimal stand-in for a Request: the dependency reads headers and
    nothing else, and that narrowness is the point."""

    def __init__(self, _extra=None, **headers):
        items = [(k.replace("_", "-").lower(), v) for k, v in headers.items()]
        if _extra:
            items.extend(_extra)
        self.headers = _Headers(items)


def _check(monkeypatch, allowlist, _extra=None, **headers):
    if allowlist is None:
        monkeypatch.delenv("DEADBAND_TRUSTED_LOGINS", raising=False)
    else:
        monkeypatch.setenv("DEADBAND_TRUSTED_LOGINS", allowlist)
    return require_trusted_identity(_Req(_extra=_extra, **headers))


def test_a_login_on_the_allowlist_is_accepted(monkeypatch):
    got = _check(monkeypatch, "alice@example.invalid", Tailscale_User_Login="alice@example.invalid")
    assert got == "alice@example.invalid"


def test_a_login_not_on_the_allowlist_is_refused(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _check(monkeypatch, "alice@example.invalid", Tailscale_User_Login="mallory@example.invalid")
    assert exc.value.status_code == 403


def test_a_missing_identity_header_is_refused(monkeypatch):
    """No header means the request did not come through the proxy -- it
    reached the app's local port directly. Deny it."""
    with pytest.raises(HTTPException) as exc:
        _check(monkeypatch, "alice@example.invalid")
    assert exc.value.status_code == 403


def test_an_unset_allowlist_refuses_rather_than_permits(monkeypatch):
    """The whole point. DEADBAND_ENABLE_WRITES reads '=0' as enabled (gap #61);
    this must not repeat that shape. Absent config is a refusal, and a
    DISTINCT one, so an operator can tell misconfiguration from denial."""
    with pytest.raises(HTTPException) as exc:
        _check(monkeypatch, None, Tailscale_User_Login="alice@example.invalid")
    assert exc.value.status_code == 503


def test_an_empty_allowlist_refuses(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        _check(monkeypatch, "   ", Tailscale_User_Login="alice@example.invalid")
    assert exc.value.status_code == 503


def test_the_allowlist_tolerates_whitespace_and_case(monkeypatch):
    got = _check(
        monkeypatch,
        " Alice@Example.Invalid , bob@example.invalid ",
        Tailscale_User_Login="alice@example.invalid",
    )
    assert got == "alice@example.invalid"


def test_x_forwarded_for_is_never_consulted(monkeypatch):
    """Pins the prohibition. A caller-supplied source address must not be able
    to stand in for identity, no matter how plausible it looks."""
    with pytest.raises(HTTPException) as exc:
        _check(
            monkeypatch,
            "alice@example.invalid",
            X_Forwarded_For="203.0.113.1",
            Tailscale_User_Name="Alice",
        )
    assert exc.value.status_code == 403


def test_a_whitespace_only_header_never_matches_a_trailing_comma_slot(monkeypatch):
    """A trailing comma in the allowlist produces an empty string when split.
    That empty string must never occupy an allowlist slot that a
    whitespace-only header could normalize down to and match. Confirmed by
    mutation: this fails if the `if entry.strip()` filter in api/identity.py
    is removed -- see the fix report for the exact result."""
    with pytest.raises(HTTPException) as exc:
        _check(monkeypatch, "alice@example.invalid,,", Tailscale_User_Login="   ")
    assert exc.value.status_code == 403


def test_the_returned_login_is_normalized(monkeypatch):
    """The return value becomes an actor string on ledger writes (Task 2).
    Whitespace or case variation on an otherwise-identical login must not be
    able to split one person's audit trail into several distinct strings."""
    got = _check(
        monkeypatch,
        "alice@example.invalid",
        Tailscale_User_Login=" Alice@Example.Invalid ",
    )
    assert got == "alice@example.invalid"


def test_multiple_identity_headers_is_refused(monkeypatch):
    """Whether the proxy replaces or appends an identity header is not pinned
    anywhere in this repository. If a caller could send their own copy and
    the proxy appends rather than replaces, `Headers.get` would silently
    return whichever value sorts first. Two values of any kind -- caller-
    supplied, proxy-duplicated, doesn't matter which -- must be refused
    rather than have one picked."""
    with pytest.raises(HTTPException) as exc:
        _check(
            monkeypatch,
            "alice@example.invalid",
            Tailscale_User_Login="alice@example.invalid",
            _extra=[("tailscale-user-login", "mallory@example.invalid")],
        )
    assert exc.value.status_code == 403


def test_a_non_ascii_login_is_refused(monkeypatch):
    """str.lower() folds some non-ASCII characters onto ASCII ones -- e.g.
    U+212A KELVIN SIGN folds to 'k' -- which could let a lookalike glyph
    pass as a match. Real tailnet logins are ASCII email addresses."""
    with pytest.raises(HTTPException) as exc:
        _check(
            monkeypatch,
            "mike@example.invalid",
            Tailscale_User_Login="mi\u212ae@example.invalid",  # Kelvin sign, not ASCII K
        )
    assert exc.value.status_code == 403
