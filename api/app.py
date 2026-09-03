"""The Deadband read-only API (spec 2026-08-19-read-only-api-design).

Binds 127.0.0.1 only, and the READ routes carry no auth code of their own
(spec D1) -- the network path in front of them is what gates them, same as
always. That is no longer true of the app as a whole: when write routes are
enabled (below), every one of them requires a verified caller identity (spec
2026-08-24-entry-import-design.md §6, api/identity.py). Run it with:

    uv run uvicorn api.app:app --host 127.0.0.1 --port 8000

Serves web/dist/ as static files when present, so one localhost port is the
whole app; in dev the Vite server proxies /api here instead."""

from __future__ import annotations

import os
import pathlib

from fastapi import FastAPI, Request, Response

from api.accounts import router as accounts_router
from api.dashboard import router as dashboard_router
from api.health import router as health_router
from api.serialization import DeadbandJSONResponse
from api.trades import router as trades_router

_WEB_DIST = pathlib.Path(__file__).resolve().parents[1] / "web" / "dist"

# `no-cache` does NOT mean "do not store" -- it means "revalidate before
# reusing". index.html is the one document that names the content-hashed
# bundles, so serving a stale copy pins the whole app to a previous deploy.
# Revalidation is cheap: FileResponse already sends an ETag, so an unchanged
# file costs a 304 with no body.
_NO_CACHE = {"cache-control": "no-cache"}


def create_app(enable_writes: bool | None = None) -> FastAPI:
    if enable_writes is None:
        enable_writes = bool(os.environ.get("DEADBAND_ENABLE_WRITES"))
    app = FastAPI(
        title="deadband",
        default_response_class=DeadbandJSONResponse,
        # No interactive docs: the API is a private contract with web/, not a
        # surface to explore; /docs would be the only thing resembling an
        # exposed interface on a box whose posture is localhost-only.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.pool = None
    app.state.write_pool = None
    app.include_router(health_router)
    app.include_router(trades_router)
    app.include_router(dashboard_router)
    app.include_router(accounts_router)
    # Write routes exist ONLY when explicitly enabled. When the flag is unset
    # these endpoints are absent and return 404 to every request -- or 405
    # when web/dist is mounted below, because the SPA catch-all is GET-only
    # and still path-matches a write verb. When the flag IS set, every write
    # route additionally requires a verified caller identity (api/identity.py,
    # spec 2026-08-24-entry-import-design.md §6) -- the flag alone is no
    # longer the only thing standing between this app and an unauthenticated
    # write. Registered before the SPA catch-all.
    if enable_writes:
        from api.accounts import write_router as accounts_write_router
        from api.fills import router as fills_router
        from api.imports import router as imports_router
        from api.marks import router as marks_router
        from api.snapshots import router as snapshots_router

        app.include_router(fills_router)
        # POST /api/imports/preview writes nothing (db/import_flow.py's
        # `preview` never opens a transaction), but it belongs to the same
        # import feature as the write routes above and is gated with them
        # rather than being reachable on the published read-only instance --
        # the published unit has no legitimate use for an import wizard at all.
        app.include_router(imports_router)
        # GET /api/marks is a read, but it exists to serve the entry screen's
        # marks table and is gated with the writes it feeds -- the same
        # reasoning applied to POST /api/imports/preview above. It declares
        # get_conn, so the read-pool guarantee is unaffected.
        app.include_router(marks_router)
        # GET /api/accounts/{account_id}/snapshot is the identical case: a
        # read, but one that exists only to serve the Snapshot entry screen's
        # "already exists for this date" warning, so it is gated with the
        # write it feeds rather than published on the read-only instance.
        app.include_router(snapshots_router)
        # PATCH /api/accounts/{id} lives on its own router because the account
        # READ router above is registered unconditionally -- a write hung off
        # that one would be reachable on the published read-only instance.
        app.include_router(accounts_write_router)

    if _WEB_DIST.exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        class _ImmutableAssets(StaticFiles):
            """Vite writes content-hashed filenames into /assets, so a given
            URL's bytes can never change -- a new build produces a new name.
            They are therefore safe to cache forever, and `immutable` tells the
            browser not to even revalidate.

            Without any Cache-Control at all (the previous behaviour) browsers
            fall back to HEURISTIC caching, roughly a tenth of the age since
            Last-Modified. That is the worse half of both worlds: assets that
            could be cached for a year are re-fetched, and index.html -- which
            must never be cached -- is served stale, pointing at a bundle from
            the previous deploy. A deploy then appeared not to land until a
            manual hard refresh.
            """

            def file_response(self, *args, **kwargs):
                response = super().file_response(*args, **kwargs)
                response.headers["cache-control"] = "public, max-age=31536000, immutable"
                return response

        app.mount("/assets", _ImmutableAssets(directory=_WEB_DIST / "assets"), name="assets")

        # SPA fallback, not StaticFiles(html=True): the client router owns
        # /trades and friends, so a refresh there must serve index.html
        # rather than 404. Registered last -- every /api route wins first.
        def _maybe_304(request, response):
            """Honour If-None-Match on the SPA fallback.

            `no-cache` on index.html is only affordable because the
            revalidation it forces can come back empty. Starlette's
            FileResponse SETS an ETag but does not CHECK the request's
            If-None-Match -- only StaticFiles does that -- so without this the
            browser would re-download the whole document on every navigation
            rather than getting a bodyless 304. Small document, but the
            comment above claimed a cheap revalidation and this is what makes
            that claim true.
            """
            # The stat_result is passed at construction below precisely so
            # this header exists here: Starlette's FileResponse otherwise sets
            # it lazily during __call__, by which point the response has been
            # returned and there is nothing left to intercept.
            etag = response.headers.get("etag")
            if etag and request.headers.get("if-none-match") == etag:
                return Response(
                    status_code=304,
                    headers={"etag": etag, "cache-control": _NO_CACHE["cache-control"]},
                )
            return response

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(request: Request, path: str):
            # Containment is structural, not a substring check. pathlib's `/`
            # operator DISCARDS the left side when the right is absolute, so
            # a request for `//etc/hostname` used to make `path` the string
            # "/etc/hostname" and `_WEB_DIST / path` silently become
            # `/etc/hostname` -- the `".." not in path` guard never saw a
            # `..` and .is_file() was true, so the file was served over the
            # network (any file readable by the service account, including
            # the deployment's env file). Rejecting a leading "/" up front
            # closes that specific hole; the real defence is .resolve() +
            # is_relative_to() below, which also collapses symlinks -- a
            # symlink placed inside dist that points outside it is caught
            # the same way an absolute path is, because both produce a
            # resolved path outside `root`.
            if path and not path.startswith("/"):
                candidate = (_WEB_DIST / path).resolve()
                root = _WEB_DIST.resolve()
                if candidate.is_relative_to(root) and candidate.is_file():
                    return _maybe_304(
                        request,
                        FileResponse(
                            candidate, headers=_NO_CACHE, stat_result=candidate.stat()
                        ),
                    )
            index = _WEB_DIST / "index.html"
            return _maybe_304(
                request, FileResponse(index, headers=_NO_CACHE, stat_result=index.stat())
            )

    return app


app = create_app()
