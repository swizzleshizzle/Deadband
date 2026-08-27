"""The read pool must stay read-only, and every route must draw from the pool
its HTTP method implies (spec section 2)."""

from fastapi.routing import APIRoute, iter_route_contexts

from api.app import create_app
from api.deps import get_conn, get_write_conn
from tests.conftest import requires_db

pytestmark = requires_db

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# POST /api/imports/preview is a narrow, explicit exception to "POST implies
# get_write_conn": it takes an upload (so it must be POST -- GET cannot carry
# a multipart body) but db/import_flow.py's `preview` never writes (see its
# docstring), so it declares get_conn on purpose. Keyed on the exact path
# rather than loosening `writes` for POST generally, so a real write route
# that forgets get_write_conn is still caught by the assertions below.
_READ_ONLY_POST_PATHS = {"/api/imports/preview"}


def _dependency_names(dependant) -> set[str]:
    return {d.call.__name__ for d in dependant.dependencies if d.call is not None}


def test_every_route_uses_the_pool_its_method_implies():
    """A POST that forgets get_write_conn would silently run inside a read-only
    transaction; a GET that reaches for the write pool quietly gives up the
    guarantee D3 exists to provide. Both fail here rather than in review."""
    # enable_writes=True: without it there is nothing on the write side of the
    # split to check, and this test would pass by finding zero POST/PUT/PATCH/
    # DELETE routes rather than by verifying they use get_write_conn.
    app = create_app(enable_writes=True)
    # app.include_router() wraps each router in an internal _IncludedRouter,
    # so app.routes no longer flattens to the included APIRoute objects
    # directly (fastapi>=0.141). iter_route_contexts() recurses through that
    # wrapping; without it this loop silently inspects zero routes.
    for route_context in iter_route_contexts(app.routes):
        # original_route is used ONLY as the APIRoute type filter below. Its
        # .path is the router-relative, unprefixed path (a router included
        # with prefix="/api" would make the "/api/" check below false for
        # every route, skipping this guard entirely), and its .dependant
        # excludes router-level dependencies (a write pool wired via
        # include_router(deps=[...]) rather than a route decorator would be
        # invisible to it). route_context.path/.methods/.dependant read the
        # effective route instead (RouteContext.__getattr__ forwards to it),
        # which is what actually decides what runs at request time. This
        # guard was already found silently vacuous once from using
        # original_route -- see tests/api/test_write_gating.py for the same
        # pattern.
        if not isinstance(route_context.original_route, APIRoute):
            continue
        if not route_context.path.startswith("/api/"):
            continue
        deps = _dependency_names(route_context.dependant)
        if not deps & {get_conn.__name__, get_write_conn.__name__}:
            continue
        writes = (
            bool(route_context.methods & _WRITE_METHODS)
            and route_context.path not in _READ_ONLY_POST_PATHS
        )
        expected = get_write_conn.__name__ if writes else get_conn.__name__
        forbidden = get_conn.__name__ if writes else get_write_conn.__name__
        path = route_context.path
        methods = sorted(route_context.methods)
        assert expected in deps, f"{methods} {path} must use {expected}"
        assert forbidden not in deps, f"{methods} {path} must not use {forbidden}"
