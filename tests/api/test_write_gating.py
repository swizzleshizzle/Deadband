"""Spec section 6: DEADBAND_ENABLE_WRITES gates whether write routes exist in
the route table at all.

The published instance now serves writes, so this flag is no longer the only
thing standing between a shared tailnet and unauthenticated writes -- every
write route also verifies caller identity (api/identity.py), a second,
independent layer covered by test_write_identity.py. What is pinned here is
narrower: registration-level gating -- with the flag absent or empty, no
write route is registered at all; with it set, the write routes appear (and
an explicit `enable_writes` argument overrides the environment either way).

The flag is also parsed rather than merely tested for emptiness. Until
2026-09-09 it was `bool(os.environ.get(...))`, so `=0` and `=false` both read
as ENABLED -- an operator zeroing the value to turn writes off got the exact
opposite (known-gap #61). Only an affirmative token enables writes now, and
anything unrecognised disables them, because for an opt-in flag the safe
direction to fail is off.

Note what is deliberately NOT tested, here or anywhere in this codebase: a
source-address check. The deployment proxies every path to the local port, so
the proxy is the client and request.client.host reads 127.0.0.1 for remote
callers -- such a check would pass for exactly the requests it exists to
stop.
"""

import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from api.app import create_app


def _write_paths(app) -> set[str]:
    # app.include_router() wraps each router in an internal _IncludedRouter,
    # so app.routes no longer flattens to the included APIRoute objects
    # directly (fastapi>=0.141). iter_route_contexts() recurses through that
    # wrapping; without it this always returns an empty set and every test
    # below would pass vacuously regardless of what create_app() registered.
    return {
        rc.path
        for rc in iter_route_contexts(app.routes)
        if isinstance(rc.original_route, APIRoute)
        and rc.methods & {"POST", "PUT", "PATCH", "DELETE"}
    }


def test_writes_are_absent_by_default(monkeypatch):
    monkeypatch.delenv("DEADBAND_ENABLE_WRITES", raising=False)
    assert _write_paths(create_app()) == set()


def test_writes_are_absent_when_the_flag_is_empty(monkeypatch):
    monkeypatch.setenv("DEADBAND_ENABLE_WRITES", "")
    assert _write_paths(create_app()) == set()


def test_writes_are_present_when_enabled(monkeypatch):
    monkeypatch.setenv("DEADBAND_ENABLE_WRITES", "1")
    assert "/api/fills" in _write_paths(create_app())


def test_explicit_argument_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("DEADBAND_ENABLE_WRITES", "1")
    assert _write_paths(create_app(enable_writes=False)) == set()


# --- gap #61: the flag is parsed, not merely tested for emptiness ----------
#
# Every case below passes vacuously under the old `bool(os.environ.get(...))`
# for the DISABLING half only -- which is the half that matters, since that
# implementation read all of these as enabled. Restoring it turns every
# `_DISABLING` case red.

_DISABLING = ["0", "false", "False", "FALSE", "no", "off", "OFF", "  0  ", "banana", "  "]
_ENABLING = ["1", "true", "True", "TRUE", "yes", "on", "ON", "  1  ", "\tyes\n"]


@pytest.mark.parametrize("value", _DISABLING)
def test_a_negative_or_unrecognised_flag_leaves_writes_absent(monkeypatch, value):
    """`=0` meaning "enabled" was the actual defect. Anything unrecognised
    joins it on the disabled side: writes are opt-in, so an unparseable value
    must not be the thing that opens them."""
    monkeypatch.setenv("DEADBAND_ENABLE_WRITES", value)
    assert _write_paths(create_app()) == set()


@pytest.mark.parametrize("value", _ENABLING)
def test_an_affirmative_flag_registers_the_write_routes(monkeypatch, value):
    """The other half of the guard: narrowing what counts as enabled must not
    have narrowed it to nothing. Without this, deleting the affirmative set
    entirely would still leave the tests above green."""
    monkeypatch.setenv("DEADBAND_ENABLE_WRITES", value)
    assert "/api/fills" in _write_paths(create_app())
