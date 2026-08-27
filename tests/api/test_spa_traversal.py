"""SPA fallback containment (api/app.py's `spa` catch-all route).

Confirmed-live arbitrary-file-read, found and fixed on this branch, predating
it: pathlib's `/` operator DISCARDS its left operand when the right is
absolute, so a request for `//etc/hostname` made the route parameter
`path == "/etc/hostname"`, and `_WEB_DIST / path` silently became
`/etc/hostname` -- the old `".." not in path` guard never saw a `..`, and
`.is_file()` was true, so the file was served with HTTP 200 to any caller who
could reach the port. Verified twice: locally and through the deployed proxy.

These tests build the app directly with `create_app()` rather than going
through tests/api/conftest.py's `client`/`api_app` fixtures: the SPA
catch-all never touches app.state.pool, so exercising it needs no database
connection and no TEST_PG_DSN -- these tests run in every environment, not
just ones with a DB configured.

They DO need web/dist to actually exist (the SPA route is only registered
when it does) -- skip explicitly, with a reason, rather than passing green
on an environment that built the app without it."""

from __future__ import annotations

import pathlib

import httpx
import pytest

from api.app import _WEB_DIST, create_app

pytestmark = pytest.mark.skipif(
    not _WEB_DIST.is_dir(),
    reason="web/dist not built in this environment -- SPA route isn't even "
    "registered, so there is nothing here to test",
)

# A real on-disk asset the catch-all itself serves (not the /assets/ mount,
# which is a separate StaticFiles app with its own, already-safe, path
# resolution) -- proves the fix still serves legitimate top-level dist files
# like favicon.svg.
_REAL_TOP_LEVEL_ASSET = "favicon.svg"


@pytest.fixture
async def spa_client():
    app = create_app(enable_writes=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _get_raw(client: httpx.AsyncClient, raw_path: str) -> httpx.Response:
    """GET a literal path, including a leading "//".

    httpx's URL parser treats a string starting with "//" passed to
    `.get()` as a network-path reference (RFC 3986 §4.2) and silently
    rewrites it against the client's own host, discarding what came after
    the slashes -- `client.get("//etc/hostname")` never actually sends
    "//etc/hostname" to the app; it sends "/hostname". Confirmed with a
    debug ASGI middleware while writing this test: the exploit only reaches
    the app when the double slash is embedded in a URL that already carries
    a scheme and host, e.g. "http://test//etc/hostname" -- httpx then
    parses "//etc/hostname" as the path rather than as a fresh authority.
    This is exactly what a raw HTTP request line (`GET //etc/hostname
    HTTP/1.1`), or the live proxy, sends -- so this helper is what makes
    these tests representative of the real exploit rather than of httpx's
    own client-side URL normalization."""
    return await client.get(f"{client.base_url}{raw_path}")


def _index_html_body() -> str:
    return (_WEB_DIST / "index.html").read_text()


async def test_double_slash_absolute_path_does_not_read_host_file(spa_client):
    """The exact confirmed exploit. Must assert on the BODY: this route
    returns 200 either way (vulnerable code serves /etc/hostname with 200;
    the fix serves index.html with 200), so a status-only assertion would
    pass against the vulnerable code and prove nothing."""
    r = await _get_raw(spa_client, "//etc/hostname")
    assert r.status_code == 200
    assert r.text == _index_html_body()
    hostname_on_disk = pathlib.Path("/etc/hostname").read_text()
    assert r.text != hostname_on_disk


async def test_double_slash_absolute_path_inside_dist_is_also_refused(spa_client):
    """Absolute is absolute: an absolute path that happens to land back
    inside dist is not a reason to special-case it. The leading-"/" reject
    fires before containment is even checked."""
    target = _WEB_DIST / _REAL_TOP_LEVEL_ASSET
    assert target.is_file(), "fixture asset missing from web/dist"
    r = await _get_raw(spa_client, f"/{target}")
    assert r.status_code == 200
    assert r.text == _index_html_body()


async def test_legitimate_top_level_asset_is_still_served(spa_client):
    r = await spa_client.get(f"/{_REAL_TOP_LEVEL_ASSET}")
    assert r.status_code == 200
    assert r.text == (_WEB_DIST / _REAL_TOP_LEVEL_ASSET).read_text()
    assert r.text != _index_html_body()


async def test_client_router_path_falls_through_to_index(spa_client):
    r = await spa_client.get("/trades")
    assert r.status_code == 200
    assert r.text == _index_html_body()


async def test_encoded_traversal_lands_on_index(spa_client):
    r = await spa_client.get("/..%2f..%2f..%2fetc%2fhostname")
    assert r.status_code == 200
    assert r.text == _index_html_body()
    hostname_on_disk = pathlib.Path("/etc/hostname").read_text()
    assert r.text != hostname_on_disk
