"""Tailnet identity on write routes.

Every login here is INVENTED. The real allowlist lives in the deployment's
gitignored env file -- the values are personal email addresses and this
repository is public.
"""

import pytest
from fastapi import HTTPException

from api.identity import require_trusted_identity


class _Req:
    """Minimal stand-in for a Request: the dependency reads headers and
    nothing else, and that narrowness is the point."""

    def __init__(self, **headers):
        self.headers = {k.replace("_", "-").lower(): v for k, v in headers.items()}


def _check(monkeypatch, allowlist, **headers):
    if allowlist is None:
        monkeypatch.delenv("DEADBAND_TRUSTED_LOGINS", raising=False)
    else:
        monkeypatch.setenv("DEADBAND_TRUSTED_LOGINS", allowlist)
    return require_trusted_identity(_Req(**headers))


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
