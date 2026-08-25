"""Spec section 6: the published instance has no write surface at all.

This is the ONLY control standing between a shared tailnet and unauthenticated
writes, so it is pinned here rather than left to the deployment. Note what is
deliberately NOT tested: a source-address check. The deployment proxies every
path to the local port, so the proxy is the client and request.client.host
reads 127.0.0.1 for remote callers -- such a check would pass for exactly the
requests it exists to stop.
"""

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
