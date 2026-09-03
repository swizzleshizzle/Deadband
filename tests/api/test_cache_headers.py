"""Cache-Control on the static surface.

With no Cache-Control at all, browsers apply HEURISTIC caching -- roughly a
tenth of the age since Last-Modified. That is the worse half of both worlds:
content-hashed assets that could be cached for a year get re-fetched, and
index.html, which must never be cached, is served stale and keeps pointing at
the previous deploy's bundle. A deploy then appears not to have landed until
someone hard-refreshes.

These use a throwaway web/dist rather than the real one, following
tests/api/test_spa_traversal.py: CI's python job never runs `pnpm build`, so a
test that needed the real directory would SKIP there -- and a silently skipped
security-adjacent test is this repo's documented worst failure mode.
"""

from __future__ import annotations

import pathlib

import httpx
import pytest

import api.app as app_module
from api.app import create_app

_INDEX_MARKER = "deadband-cache-headers-test-index"
_ASSET_BODY = "deadband-cache-headers-test-asset"


@pytest.fixture
def tmp_dist(tmp_path: pathlib.Path) -> pathlib.Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text(f"<!doctype html><html>{_INDEX_MARKER}</html>")
    assets = dist / "assets"
    assets.mkdir()
    # Named the way Vite names them: content-hashed, which is what makes
    # caching one forever safe.
    (assets / "index-AbCdEf12.js").write_text(_ASSET_BODY)
    return dist


@pytest.fixture
async def client(tmp_dist: pathlib.Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_module, "_WEB_DIST", tmp_dist)
    app = create_app(enable_writes=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_index_html_must_be_revalidated(client):
    """index.html names the hashed bundles, so a cached copy pins the whole
    app to a previous deploy. `no-cache` means "revalidate before reusing",
    not "do not store" -- the ETag makes that a cheap 304."""
    r = await client.get("/")
    assert r.status_code == 200
    assert _INDEX_MARKER in r.text
    assert r.headers["cache-control"] == "no-cache"


async def test_the_spa_fallback_route_also_revalidates(client):
    """A client-router path (/trades) serves index.html through the same
    fallback and must carry the same header -- a refresh on a deep link is
    how the stale copy would otherwise be acquired."""
    r = await client.get("/trades")
    assert r.status_code == 200
    assert _INDEX_MARKER in r.text
    assert r.headers["cache-control"] == "no-cache"


async def test_hashed_assets_are_immutable(client):
    """A new build writes a new filename, so these bytes can never change."""
    r = await client.get("/assets/index-AbCdEf12.js")
    assert r.status_code == 200
    assert r.text == _ASSET_BODY
    cc = r.headers["cache-control"]
    assert "immutable" in cc
    assert "max-age=31536000" in cc


async def test_index_still_sends_an_etag_so_revalidation_is_cheap(client):
    """`no-cache` is only affordable because the conditional request can come
    back 304 with no body. Without a validator every load would re-download
    the document."""
    r = await client.get("/")
    assert r.headers.get("etag")
    again = await client.get("/", headers={"if-none-match": r.headers["etag"]})
    assert again.status_code == 304


async def test_api_responses_are_not_given_asset_caching(client):
    """The immutable header is scoped to the /assets mount. A JSON endpoint
    picking it up would pin a client to one payload for a year."""
    r = await client.get("/api/health")
    assert "immutable" not in r.headers.get("cache-control", "")
