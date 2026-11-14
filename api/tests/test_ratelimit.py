"""Protection du service public : seau à jetons par IP, écritures rares, corps bornés."""

from __future__ import annotations

import httpx
import pytest
from champis_api.app import State, create_app
from champis_api.ratelimit import RateLimitMiddleware, TokenBuckets, client_ip, scope_headers
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


def test_client_ip_is_the_one_set_by_the_trusted_proxy():
    """L'edge Railway pose `X-Real-IP` ; à défaut, le dernier élément de `X-Forwarded-For` (ajouté
    par le proxy). Le premier élément, que le client fournit, n'est jamais lu."""
    forged = {"x-forwarded-for": "6.6.6.6, 1.2.3.4", "x-real-ip": "1.2.3.4"}
    assert client_ip(forged, ("10.0.0.1", 1)) == "1.2.3.4"
    assert client_ip({"x-forwarded-for": "6.6.6.6, 7.7.7.7, 1.2.3.4"}, ("10.0.0.1", 1)) == "1.2.3.4"
    assert client_ip({"x-forwarded-for": "1.2.3.4"}, ("10.0.0.1", 1)) == "1.2.3.4"
    assert client_ip({"x-real-ip": " ", "x-forwarded-for": ""}, ("9.9.9.9", 1)) == "9.9.9.9"
    assert client_ip({}, ("9.9.9.9", 1)) == "9.9.9.9"
    assert client_ip({}, None) == "?"
    assert len(client_ip({"x-real-ip": "a" * 500}, None)) == 64  # clé bornée
    # d'un en-tête répété, la ligne du proxy (la dernière) l'emporte sur celle du client
    scope = {"headers": [(b"X-Real-IP", b"6.6.6.6"), (b"x-real-ip", b"1.2.3.4")]}
    assert client_ip(scope_headers(scope), None) == "1.2.3.4"


def test_token_buckets_forget_full_buckets_and_stay_bounded():
    clock = [0.0]
    b = TokenBuckets(per_minute=60, clock=lambda: clock[0], max_entries=3)
    for ip in ("a", "b", "c"):
        assert b.take(ip) == 0
        clock[0] += 1.0
    assert b.take("a") == 0  # "a" resservi : "b" devient le moins récemment servi
    assert b.take("d") == 0  # plafond atteint : "b" part
    assert list(b.buckets) == ["c", "a", "d"]
    clock[0] += 61.0  # une minute sans requête : tous de nouveau pleins, donc oubliés
    assert b.take("e") == 0
    assert list(b.buckets) == ["e"]
    # un seau vidé n'est pas oublié avant d'être redevenu plein : la limite tient
    for _ in range(59):
        b.take("e")
    assert b.take("e") > 0
    clock[0] += 30.0
    b.take("f")
    assert "e" in b.buckets and b.buckets["e"].tokens < 1.0


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
        # relais de tuiles et fichiers du site : jamais limités (un écran = 75 à 150 requêtes Range)
        for path in ("/tiles/T-x.pmtiles", "/web/carte.html", "/shared/palette.json"):
            assert (await c.get(path)).status_code != 429, path
        assert (await c.get("/tiles")).status_code == 429  # le manifeste, lui, compte
        # un `X-Forwarded-For` forgé par le client ne lui donne pas un seau neuf : l'adresse posée
        # par le proxy (dernier élément, ou `X-Real-IP`) reste la clé
        forged = {"X-Forwarded-For": "6.6.6.6, 127.0.0.1"}
        assert (await c.get("/health", headers=forged)).status_code == 429
        forged = {"X-Forwarded-For": "6.6.6.6", "X-Real-IP": "127.0.0.1"}
        assert (await c.get("/health", headers=forged)).status_code == 429
        # une autre IP n'est pas touchée
        assert (await c.get("/health", headers={"X-Real-IP": "5.6.7.8"})).status_code == 200
        h = {"X-Real-IP": "5.6.7.8", "X-Champis-User": "11111111-1111-4111-8111-111111111111"}
        assert (await c.delete("/observations", headers=h)).status_code == 200
        assert (await c.delete("/observations", headers=h)).status_code == 429  # 1 écriture/min
        big = {"X-Real-IP": "7.7.7.7", "content-length": "1000"}
        r = await c.post("/observations", content=b"x" * 10, headers=big)
        assert r.status_code == 413


@pytest.mark.asyncio
async def test_body_is_counted_as_received_not_as_announced(grid, client, store):
    """Un corps en `Transfer-Encoding: chunked` n'annonce pas sa taille : les octets reçus sont
    comptés, 413 au-delà de `max_body`, et l'application n'en lit pas davantage."""
    settings = Settings(max_body_bytes=1000, admin_key="k" * 40)
    app = create_app(
        settings=settings, state=State(grid=grid, client=client, cache=client.cache, store=store)
    )

    async def chunks(n: int):
        for _ in range(n):
            yield b"x" * 400

    user = {"X-Champis-User": "11111111-1111-4111-8111-111111111111"}
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        r = await c.post("/observations", content=chunks(50), headers=user)
        assert "content-length" not in r.request.headers
        assert r.status_code == 413 and "1000 octets" in r.json()["detail"]
        # sous la borne, la requête arrive à la route (422 : ce n'est pas un lot d'observations)
        r = await c.post("/observations", content=chunks(2), headers=user)
        assert r.status_code == 422
        r = await c.put("/coins", content=chunks(50), headers=user)
        assert r.status_code == 413
        # le formulaire du backoffice lit son corps lui-même (`request.body()`) : même réponse
        form = {"Content-Type": "application/x-www-form-urlencoded"}
        r = await c.post("/admin/login", content=chunks(50), headers=form)
        assert r.status_code == 413


@pytest.mark.asyncio
async def test_body_limit_stops_reading_and_keeps_a_started_response():
    """ASGI brut : la lecture s'arrête au premier message qui dépasse ; une application qui a
    déjà commencé à répondre garde sa réponse."""
    messages = [{"type": "http.request", "body": b"x" * 60, "more_body": True} for _ in range(5)]
    read = []

    async def receive():
        read.append(1)
        return messages.pop(0)

    out = []

    async def send(message):
        out.append(message)

    async def reader(scope, receive, send):
        while (await receive()).get("more_body"):
            pass
        raise AssertionError("jamais atteint")

    async def early(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive(), await receive()

    scope = {"type": "http", "method": "POST", "path": "/x", "headers": [], "client": ("1", 1)}
    await RateLimitMiddleware(reader, max_body=100)(scope, receive, send)
    assert len(read) == 2 and out[0]["status"] == 413
    out.clear()
    await RateLimitMiddleware(early, max_body=100)(scope, receive, send)
    assert [m.get("status") for m in out] == [200]
