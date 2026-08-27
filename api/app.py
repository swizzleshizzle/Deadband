"""The Deadband read-only API (spec 2026-08-19-read-only-api-design).

Binds 127.0.0.1 only and contains no auth code (spec D1) -- run it with:

    uv run uvicorn api.app:app --host 127.0.0.1 --port 8000

Serves web/dist/ as static files when present, so one localhost port is the
whole app; in dev the Vite server proxies /api here instead."""

from __future__ import annotations

import os
import pathlib

from fastapi import FastAPI

from api.accounts import router as accounts_router
from api.dashboard import router as dashboard_router
from api.health import router as health_router
from api.serialization import DeadbandJSONResponse
from api.trades import router as trades_router

_WEB_DIST = pathlib.Path(__file__).resolve().parents[1] / "web" / "dist"


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
    # Write routes exist ONLY when explicitly enabled. The published unit does
    # not set the flag, so these endpoints are absent there and return 404 to
    # every proxied request -- or 405 when web/dist is mounted below, because
    # the SPA catch-all is GET-only and still path-matches a write verb.
    # Either way nothing is trusted, not a header and not a source address
    # (spec section 6). Registered before the SPA catch-all.
    if enable_writes:
        from api.fills import router as fills_router
        from api.imports import router as imports_router

        app.include_router(fills_router)
        # POST /api/imports/preview writes nothing (db/import_flow.py's
        # `preview` never opens a transaction), but it belongs to the same
        # import feature as the write routes above and is gated with them
        # rather than being reachable on the published read-only instance --
        # the published unit has no legitimate use for an import wizard at all.
        app.include_router(imports_router)

    if _WEB_DIST.exists():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=_WEB_DIST / "assets"), name="assets")

        # SPA fallback, not StaticFiles(html=True): the client router owns
        # /trades and friends, so a refresh there must serve index.html
        # rather than 404. Registered last -- every /api route wins first.
        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> FileResponse:
            candidate = _WEB_DIST / path
            if path and ".." not in path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(_WEB_DIST / "index.html")

    return app


app = create_app()
