"""The Deadband read-only API (spec 2026-08-19-read-only-api-design).

Binds 127.0.0.1 only and contains no auth code (spec D1) -- run it with:

    uv run uvicorn api.app:app --host 127.0.0.1 --port 8000

Serves web/dist/ as static files when present, so one localhost port is the
whole app; in dev the Vite server proxies /api here instead."""

from __future__ import annotations

import pathlib

from fastapi import FastAPI

from api.health import router as health_router
from api.trades import router as trades_router
from api.serialization import DeadbandJSONResponse

_WEB_DIST = pathlib.Path(__file__).resolve().parents[1] / "web" / "dist"


def create_app() -> FastAPI:
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
    app.include_router(health_router)
    app.include_router(trades_router)

    if _WEB_DIST.exists():
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="web")

    return app


app = create_app()
