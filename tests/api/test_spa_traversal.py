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

They used to also need web/dist to actually exist (the SPA route is only
registered when it does), and skipped explicitly when it didn't. That made
CI's python job -- where `pnpm build` never runs, so web/dist is absent --
skip all five of these tests silently under the repo's "DB tests skipped"
guard, which doesn't distinguish an explicit skip from a missing build
artifact. Explicit-and-absent is still absent for a live vulnerability's
regression tests. Fixed by building a throwaway dist look-alike per test in
a `tmp_path` fixture and monkeypatching `api.app._WEB_DIST` to point at it
before calling `create_app()` -- `_WEB_DIST` is a module-level constant read
by `create_app()` at call time (see api/app.py), so patching the module
attribute is enough; no skip, ever, in any environment."""

from __future__ import annotations

import pathlib

import httpx
import pytest

import api.app as app_module
from api.app import create_app

# Recognisable content baked into the throwaway index.html/assets so
# assertions compare against known strings instead of the real web/dist
# (which may not even exist in this environment, and shouldn't need to).
_INDEX_MARKER = "deadband-spa-traversal-test-index-marker"
_TOP_LEVEL_ASSET_NAME = "top-level-asset.txt"
_TOP_LEVEL_ASSET_BODY = "deadband-spa-traversal-test-top-level-asset-body"
_NESTED_ASSET_BODY = "deadband-spa-traversal-test-nested-asset-body"


@pytest.fixture
def tmp_dist(tmp_path: pathlib.Path) -> pathlib.Path:
    """A throwaway web/dist look-alike, built fresh per test:

    - index.html carrying a marker string unique to this test module, so
      "the SPA fallback served" can be asserted on the response BODY, not
      just a 200 status (this route returns 200 whether it serves
      index.html or, pre-fix, an arbitrary host file).
    - one top-level asset file -- what the `spa` catch-all itself serves
      for a real on-disk path, as opposed to the separate `/assets/`
      StaticFiles mount.
    - an assets/ subdirectory with one file -- create_app() unconditionally
      mounts `StaticFiles(directory=_WEB_DIST / "assets")` whenever
      `_WEB_DIST` exists, and Starlette refuses to mount a directory that
      isn't there, so this has to exist for create_app() to succeed at all.
    """
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(f"<!doctype html><html>{_INDEX_MARKER}</html>")
    (dist / _TOP_LEVEL_ASSET_NAME).write_text(_TOP_LEVEL_ASSET_BODY)
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text(_NESTED_ASSET_BODY)
    return dist


@pytest.fixture
async def spa_client(tmp_dist: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_module, "_WEB_DIST", tmp_dist)
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


def _index_html_body(tmp_dist: pathlib.Path) -> str:
    return (tmp_dist / "index.html").read_text()


async def test_double_slash_absolute_path_does_not_read_host_file(spa_client, tmp_dist):
    """The exact confirmed exploit. Must assert on the BODY: this route
    returns 200 either way (vulnerable code serves /etc/hostname with 200;
    the fix serves index.html with 200), so a status-only assertion would
    pass against the vulnerable code and prove nothing."""
    r = await _get_raw(spa_client, "//etc/hostname")
    assert r.status_code == 200
    assert r.text == _index_html_body(tmp_dist)
    assert _INDEX_MARKER in r.text
    hostname_on_disk = pathlib.Path("/etc/hostname").read_text()
    assert r.text != hostname_on_disk


async def test_double_slash_absolute_path_inside_dist_is_also_refused(spa_client, tmp_dist):
    """Absolute is absolute: an absolute path that happens to land back
    inside dist is not a reason to special-case it. The leading-"/" reject
    fires before containment is even checked."""
    target = tmp_dist / _TOP_LEVEL_ASSET_NAME
    assert target.is_file(), "fixture asset missing from throwaway dist"
    r = await _get_raw(spa_client, f"/{target}")
    assert r.status_code == 200
    assert r.text == _index_html_body(tmp_dist)


async def test_legitimate_top_level_asset_is_still_served(spa_client, tmp_dist):
    r = await spa_client.get(f"/{_TOP_LEVEL_ASSET_NAME}")
    assert r.status_code == 200
    assert r.text == _TOP_LEVEL_ASSET_BODY
    assert r.text != _index_html_body(tmp_dist)


async def test_client_router_path_falls_through_to_index(spa_client, tmp_dist):
    r = await spa_client.get("/trades")
    assert r.status_code == 200
    assert r.text == _index_html_body(tmp_dist)


async def test_encoded_traversal_lands_on_index(spa_client, tmp_dist):
    r = await spa_client.get("/..%2f..%2f..%2fetc%2fhostname")
    assert r.status_code == 200
    assert r.text == _index_html_body(tmp_dist)
    hostname_on_disk = pathlib.Path("/etc/hostname").read_text()
    assert r.text != hostname_on_disk
