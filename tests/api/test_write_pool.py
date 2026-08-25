"""The read pool must stay read-only, and every route must draw from the pool
its HTTP method implies (spec section 2)."""

from fastapi.routing import APIRoute

from api.app import create_app
from api.deps import get_conn, get_write_conn
from tests.conftest import requires_db

pytestmark = requires_db

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _dependency_names(route: APIRoute) -> set[str]:
    return {d.call.__name__ for d in route.dependant.dependencies if d.call is not None}


def test_every_route_uses_the_pool_its_method_implies():
    """A POST that forgets get_write_conn would silently run inside a read-only
    transaction; a GET that reaches for the write pool quietly gives up the
    guarantee D3 exists to provide. Both fail here rather than in review."""
    app = create_app()
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        deps = _dependency_names(route)
        if not deps & {get_conn.__name__, get_write_conn.__name__}:
            continue
        writes = bool(route.methods & _WRITE_METHODS)
        expected = get_write_conn.__name__ if writes else get_conn.__name__
        forbidden = get_conn.__name__ if writes else get_write_conn.__name__
        assert expected in deps, f"{sorted(route.methods)} {route.path} must use {expected}"
        assert forbidden not in deps, f"{sorted(route.methods)} {route.path} must not use {forbidden}"
