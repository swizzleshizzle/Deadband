"""Every write route requires tailnet identity; no read route does.

Walks the routes rather than listing them, so a write endpoint added later
without the dependency fails here instead of shipping unauthenticated.
All logins invented.
"""

from fastapi.routing import APIRoute, iter_route_contexts

from api.app import create_app
from api.identity import require_trusted_identity
from tests.api.test_write_pool import _READ_ONLY_POST_PATHS
from tests.conftest import requires_db

pytestmark = requires_db

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The read-only POST exception is imported, not restated, from
# tests/api/test_write_pool.py -- see the comment there.


def test_every_write_route_requires_identity():
    app = create_app(enable_writes=True)
    for rc in iter_route_contexts(app.routes):
        if not isinstance(rc.original_route, APIRoute) or not rc.path.startswith("/api/"):
            continue
        deps = {d.call.__name__ for d in rc.dependant.dependencies if d.call is not None}
        writes = bool(rc.methods & _WRITE_METHODS) and rc.path not in _READ_ONLY_POST_PATHS
        if writes:
            assert require_trusted_identity.__name__ in deps, (
                f"{sorted(rc.methods)} {rc.path} writes without an identity check"
            )
