"""GET /api/health: liveness plus schema currency (spec §3)."""

from tests.conftest import requires_db

pytestmark = requires_db


async def test_health_reports_current_migrations(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] is True
    assert body["migrations_current"] is True
    assert body["pending_migrations"] == []


async def test_health_reports_db_down_as_200(api_app):
    import httpx

    class _Broken:
        def acquire(self):
            raise RuntimeError("no database")

        async def close(self):
            return None

    api_app.state.pool = _Broken()
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["db"] is False
