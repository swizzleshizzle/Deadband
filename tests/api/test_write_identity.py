"""Every write route requires tailnet identity; no read route does.

The route-walking test below walks the routes rather than listing them, so a
write endpoint added later without the dependency fails here instead of
shipping unauthenticated. It is structural, though: it proves the dependency
is DECLARED, not that it is ENFORCED when a real request comes through ASGI.
The tests after it close that gap by driving actual HTTP requests through
`anonymous_client` (no identity header at all -- see tests/api/conftest.py)
and asserting a refusal, so an override, a swallowed exception, or a
duplicate route registration would fail here even though it would leave the
structural check green. All logins invented.
"""

from uuid import uuid4

import httpx
import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from api.app import create_app
from api.identity import require_trusted_identity
from tests.api.test_write_pool import _READ_ONLY_POST_PATHS
from tests.conftest import requires_db

pytestmark = requires_db

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# The read-only POST exception is imported, not restated, from
# tests/api/test_write_pool.py -- see the comment there.

_FIDELITY_CSV = (
    "Run Date,Account Number,Action,Symbol,Description,Quantity,Price,Commission,Fees,Amount\n"
    "01/15/2026,X12345678,YOU BOUGHT,SPY,SPDR S&P 500 ETF TRUST,10,500.00,0.00,0.00,-5000.00\n"
)


def _leg():
    return {
        "symbol": "ZZI", "side": "buy", "quantity": "1", "price": "1",
        "fee": "0", "fee_currency": "USD", "executed_at": "2026-06-01T15:30:00Z",
    }


async def _post_fills(client):
    # A syntactically valid, never-created account id: identity is checked
    # before the handler ever queries for it, so a 403/503 here cannot be
    # confused with the 404 a real-but-missing account would also produce.
    return await client.post(
        "/api/fills", json={"account_id": str(uuid4()), "fills": [_leg()]}
    )


async def _delete_fill(client):
    return await client.delete(f"/api/fills/{uuid4()}")


async def _post_marks(client):
    # A syntactically valid, never-created instrument id: identity is checked
    # before the handler queries for it, so a 403/503 here cannot be confused
    # with the 404 a real-but-missing instrument would produce.
    return await client.post(
        "/api/marks",
        json={"as_of": "2026-06-01T15:30:00Z",
              "marks": [{"instrument_id": str(uuid4()), "price": "1"}]},
    )


async def _commit_import(client):
    return await client.post(
        "/api/imports/commit",
        files={"file": ("e.csv", _FIDELITY_CSV, "text/csv")},
        data={"venue": "fidelity"},
    )


async def _preview_import(client):
    return await client.post(
        "/api/imports/preview",
        files={"file": ("e.csv", _FIDELITY_CSV, "text/csv")},
        data={"venue": "fidelity"},
    )


_WRITE_REQUESTS = [
    pytest.param(_post_fills, id="post-fills"),
    pytest.param(_delete_fill, id="delete-fill"),
    pytest.param(_commit_import, id="commit-import"),
    pytest.param(_post_marks, id="post-marks"),
]


def test_every_write_route_requires_identity():
    """Structural: the dependency is declared on every write route."""
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
        else:
            # The other half of the claim in this test's name: a read route
            # (or the exempt read-only POST) must NOT carry this dependency.
            # A blanket `dependencies=[...]` on the whole app would satisfy
            # the write-side assertion above without this half catching it.
            assert require_trusted_identity.__name__ not in deps, (
                f"{sorted(rc.methods)} {rc.path} is not a write route but requires identity"
            )


@pytest.mark.parametrize("make_request", _WRITE_REQUESTS)
async def test_no_identity_header_refuses_over_http(anonymous_client, make_request):
    """The gap the structural test above cannot see: a real ASGI request with
    no identity header at all -- exactly what a caller that never went
    through the authenticating proxy looks like -- must be refused, not
    merely have the dependency present in the route's declaration."""
    r = await make_request(anonymous_client)
    assert r.status_code == 403


@pytest.mark.parametrize("make_request", _WRITE_REQUESTS)
async def test_a_non_allowlisted_login_refuses_over_http(api_app, make_request):
    """`client`'s default header names a login the fixture's allowlist
    already admits (tests/api/conftest.py); this sends a DIFFERENT invented
    login the allowlist was never told about."""
    transport = httpx.ASGITransport(app=api_app)
    headers = {"Tailscale-User-Login": "mallory@example.invalid"}
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers=headers
    ) as untrusted_client:
        r = await make_request(untrusted_client)
    assert r.status_code == 403


@pytest.mark.parametrize("make_request", _WRITE_REQUESTS)
async def test_an_unset_allowlist_refuses_over_http(client, monkeypatch, make_request):
    """Fail-closed end to end: even a request carrying the fixture's own
    trusted header (`client`'s default) is refused -- with the DISTINCT 503,
    not 403 -- once the allowlist itself is unset, matching
    tests/api/test_identity.py's unit-level version of the same rule."""
    monkeypatch.delenv("DEADBAND_TRUSTED_LOGINS", raising=False)
    r = await make_request(client)
    assert r.status_code == 503


async def test_preview_import_is_still_reachable_without_identity(anonymous_client):
    """POST /api/imports/preview writes nothing (spec section 6 / the
    _READ_ONLY_POST_PATHS exemption above) and must stay reachable with no
    identity header at all -- proving the identity requirement was added to
    the write routes specifically, not accidentally blanket-applied."""
    r = await _preview_import(anonymous_client)
    assert r.status_code == 200
