"""Protection du service public : seau à jetons par IP, écritures rares, corps bornés."""

from __future__ import annotations

import httpx
import pytest
from champis_api.app import State, create_app
from champis_api.ratelimit import RateLimitMiddleware, TokenBuckets, client_ip
from champis_api.settings import Settings


def test_token_bucket_refills_over_time():
    clock = [0.0]
    b = TokenBuckets(per_minute=60, clock=lambda: clock[0])  # un jeton par seconde
    assert all(b.take("ip") == 0 for _ in range(60))
    wait = b.take("ip")
    assert 0 < wait <= 1.0
    clock[0] += 2.0
    assert b.take("ip") == 0 and b.take("ip") == 0 and b.take("ip") > 0
    assert b.take("autre-ip") == 0  # indépendant


def test_client_ip_prefers_forwarded_for():
    assert client_ip({"x-forwarded-for": "1.2.3.4, 10.0.0.1"}, ("10.0.0.1", 1)) == "1.2.3.4"
    assert client_ip({}, ("9.9.9.9", 1)) == "9.9.9.9"


@pytest.mark.asyncio
async def test_middleware_limits_reads_writes_and_body(grid, client, store):
    settings = Settings(rate_read_per_minute=3, rate_write_per_minute=1, max_body_bytes=100)
    app = create_app(
        settings=settings, state=State(grid=grid, client=client, cache=client.cache, store=store)
    )
    # horloge figée : aucun jeton ne revient pendant le test
    for m in app.user_middleware:
        if m.cls is RateLimitMiddleware:
            m.kwargs["clock"] = lambda: 0.0
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        for _ in range(3):
            assert (await c.get("/health")).status_code == 200
        r = await c.get("/health")
        assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
        # une autre IP n'est pas touchée
        assert (await c.get("/health", headers={"X-Forwarded-For": "5.6.7.8"})).status_code == 200
        h = {"X-Forwarded-For": "5.6.7.8", "X-Champis-User": "11111111-1111-4111-8111-111111111111"}
        assert (await c.delete("/observations", headers=h)).status_code == 200
        assert (await c.delete("/observations", headers=h)).status_code == 429  # 1 écriture/min
        big = {"X-Forwarded-For": "7.7.7.7", "content-length": "1000"}
        r = await c.post("/observations", content=b"x" * 10, headers=big)
        assert r.status_code == 413
