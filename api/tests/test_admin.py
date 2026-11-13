"""Backoffice `/admin` (spec › Backoffice, lot BO1) : fermé sans clé, ouvert par cookie ou Bearer,
origine et en-tête d'action vérifiés, essais de connexion limités ; les pages Santé, Usage et
Tâches rendent ce que la spec décrit ; les compteurs 429 et 5xx se remplissent sans adresse."""

from __future__ import annotations

import time
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from champis_api import counts
from champis_api.admin import auth
from champis_api.app import State, create_app
from champis_api.ratelimit import RateLimitMiddleware
from champis_api.settings import Settings
from conftest import MemoryBucket

KEY = "une-cle-de-test-suffisamment-longue-0123456789"
ACTION = {"X-Champis-Admin": "1"}


def _app(grid, client, store, bucket=None, **settings):
    s = Settings(admin_key=KEY, s3=None, apns_key="", **settings)
    state = State(grid=grid, client=client, cache=client.cache, store=store, bucket=bucket)
    return create_app(settings=s, state=state)


@pytest_asyncio.fixture
async def admin(grid, client, store):
    app = _app(grid, client, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        yield c


# ------------------------------------------------------------------ accès


def test_tokens_and_keys():
    t = auth.make_token(KEY, now=1000.0, ttl_s=60)
    assert auth.token_valid(KEY, t, now=1059.0)
    assert not auth.token_valid(KEY, t, now=1061.0)  # expiré
    assert not auth.token_valid("autre-cle-tout-aussi-longue-0123456789", t, now=1000.0)
    assert not auth.token_valid(KEY, "n-importe-quoi", now=1000.0)
    assert not auth.token_valid(KEY, None)
    assert auth.key_matches(KEY, KEY) and not auth.key_matches(KEY, KEY + "x")
    assert not auth.key_matches("courte", "courte")  # une clé trop courte n'ouvre rien
    assert not auth.key_matches("", "")


@pytest.mark.asyncio
async def test_without_a_key_the_backoffice_does_not_exist(grid, client, store):
    app = create_app(
        settings=Settings(admin_key="", s3=None, apns_key=""),
        state=State(grid=grid, client=client, cache=client.cache, store=store),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        for path in ("/admin", "/admin/", "/admin/static/admin.js", "/admin/api/health"):
            assert (await c.get(path)).status_code == 404, path
        r = await c.post("/admin/login", json={"key": ""}, headers=ACTION)
        assert r.status_code == 404
        assert (await c.get("/health")).status_code == 200  # le reste de l'API est intact


@pytest.mark.asyncio
async def test_login_cookie_bearer_origin_and_logout(admin: httpx.AsyncClient):
    # sans session, la page d'entrée mène à la connexion (pages : test_admin_pages.py)
    r = await admin.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    r = await admin.get("/admin/login")
    assert r.status_code == 200 and "backoffice" in r.text
    assert r.headers["x-robots-tag"].startswith("noindex")
    for name in ("admin.js", "admin.css"):
        assert (await admin.get(f"/admin/static/{name}")).status_code == 200
    assert (await admin.get("/admin/static/../settings.py")).status_code in (404, 400)
    assert (await admin.get("/admin/api/me")).status_code == 401

    # la connexion : en-tête d'action obligatoire, origine étrangère refusée, mauvaise clé 401
    assert (await admin.post("/admin/login", json={"key": KEY})).status_code == 403
    r = await admin.post(
        "/admin/login", json={"key": KEY}, headers={**ACTION, "Origin": "https://evil.test"}
    )
    assert r.status_code == 403
    assert (
        await admin.post("/admin/login", json={"key": "nope"}, headers=ACTION)
    ).status_code == 401
    r = await admin.post(
        "/admin/login", json={"key": KEY}, headers={**ACTION, "Origin": "http://test"}
    )
    assert r.status_code == 204
    cookie = r.headers["set-cookie"]
    assert "champis_admin=" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Path=/admin" in cookie and "Secure" not in cookie  # http en test ; https en prod
    assert (await admin.get("/admin/api/me")).json() == {"ok": True}
    # une page tierce ne peut pas lire avec le cookie
    r = await admin.get("/admin/api/me", headers={"Origin": "https://evil.test"})
    assert r.status_code == 403
    # déconnexion : action, donc en-tête requis
    assert (await admin.post("/admin/logout")).status_code == 403
    assert (await admin.post("/admin/logout", headers=ACTION)).status_code == 204
    admin.cookies.clear()
    assert (await admin.get("/admin/api/me")).status_code == 401
    # Bearer pour curl et les scripts
    r = await admin.get("/admin/api/me", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200
    r = await admin.get("/admin/api/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_attempts_are_limited_per_ip(admin: httpx.AsyncClient):
    for _ in range(5):
        r = await admin.post("/admin/login", json={"key": "nope"}, headers=ACTION)
        assert r.status_code == 401
    r = await admin.post("/admin/login", json={"key": KEY}, headers=ACTION)
    assert r.status_code == 429 and "retry-after" in r.headers
    # une autre adresse n'est pas touchée
    r = await admin.post(
        "/admin/login", json={"key": KEY}, headers={**ACTION, "X-Real-IP": "5.6.7.8"}
    )
    assert r.status_code == 204
    # les seaux de connexion se purgent seuls : une minute plus tard, il n'en reste aucun d'ancien
    buckets = admin._transport.app.state.admin_login_buckets
    assert len(buckets.buckets) == 2
    buckets.clock = lambda: time.monotonic() + 120.0
    buckets.take("9.9.9.9")
    assert list(buckets.buckets) == ["9.9.9.9"]


# ------------------------------------------------------------------ pages


@pytest.fixture
def bearer():
    return {"Authorization": f"Bearer {KEY}"}


@pytest.mark.asyncio
async def test_health_page_gathers_state_and_alerts(grid, client, store, bearer):
    store.create_job("warm", {}, "cron", datetime.now(UTC) - timedelta(hours=20), status="failed")
    store.bump(counts.today() - timedelta(days=1), "http_5xx", 3)
    bucket = MemoryBucket()
    bucket.json["tiles/manifest.json"] = {
        "generated": "2026-11-06T10:00:00",
        "depts_by_hash": {"dabc": ["25", "39"], "d25": ["25"]},
        "tiles": [
            {"kind": "T", "species": "boletus_edulis", "depts_hash": "dabc", "bytes": 10},
            {"kind": "A", "depts_hash": "dabc", "bytes": 5},
            {
                "kind": "T",
                "species": "boletus_edulis",
                "depts_hash": "d25",
                "bytes": 2,
                "dept": "25",
            },
        ],
    }
    app = _app(grid, client, store, bucket=bucket)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        r = await c.get("/admin/api/health", headers=bearer)
        assert r.status_code == 200
        h = r.json()
    assert h["api"]["build"] == "développement" and h["api"]["started_at"]
    assert h["cache"]["cells"] == 2 and h["cache"]["seeded"] == 2
    assert h["cache"]["cache_published"] is None
    assert h["disk"]["free_bytes"] > 0
    assert h["database"]["dialect"] == "sqlite" and h["database"]["tables"]["jobs"] == 1
    assert h["jobs"]["warm"]["last"]["status"] == "failed" and h["jobs"]["notify"]["last"] is None
    m = h["bucket"]["manifest"]
    assert m["tiles"] == 3 and m["bytes"] == 17 and m["per_dept_sets"] == 1
    assert m["sets"][0]["depts"] == ["25", "39"] and m["sets"][0]["kinds"] == {"T": 1, "A": 1}
    yesterday = (counts.today() - timedelta(days=1)).isoformat()
    assert h["traffic"][yesterday] == {"http_5xx": 3}
    assert {k["name"] for k in h["checks"]} >= {"grille météo", "espèces"}
    texts = [a["text"] for a in h["alerts"]]
    assert any("aucun cache publié" in t for t in texts)
    assert any("pré-chauffe : dernière exécution en échec" in t for t in texts)
    assert any("notification « mes coins » : aucune exécution" in t for t in texts)
    assert any("3 réponses 5xx hier" in t for t in texts)
    assert h["alerts"][0]["level"] == "error"  # les plus graves d'abord


@pytest.mark.asyncio
async def test_health_without_bucket_or_store_still_answers(grid, client, bearer):
    app = _app(grid, client, store=None)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        h = (await c.get("/admin/api/health", headers=bearer)).json()
        assert h["database"] is None and h["jobs"] == {} and h["traffic"] == {}
        assert h["bucket"] == {"configured": False}
        texts = [a["text"] for a in h["alerts"]]
        assert any("pas de base" in t for t in texts) and any(
            "bucket non configuré" in t for t in texts
        )
        assert (await c.get("/admin/api/usage", headers=bearer)).status_code == 503
        assert (await c.get("/admin/api/jobs", headers=bearer)).status_code == 503


@pytest.mark.asyncio
async def test_usage_page_returns_counters_and_the_fourteen_day_table(admin, store, bearer):
    today = counts.today()
    store.bump(today, "web_map_opens", 4)
    store.bump(today - timedelta(days=100), "web_map_opens", 9)  # hors fenêtre de 90 jours
    store.set_count(today - timedelta(days=1), "coins", 7)
    r = await admin.get("/admin/api/usage?days=90", headers=bearer)
    assert r.status_code == 200
    u = r.json()
    assert len(u["days"]) == 90 and u["days"][-1] == today.isoformat()
    assert u["counters"]["web_map_opens"] == {today.isoformat(): 4}
    assert u["counters"]["coins"] == {(today - timedelta(days=1)).isoformat(): 7}
    assert u["labels"]["http_5xx"] == "5xx"
    assert u["table"]["header"][:2] == ["jour", "carte"] and len(u["table"]["rows"]) == 14
    assert u["table"]["rows"][0][:2] == [today.isoformat(), "4"]
    assert u["table"]["rows"][1][5] == "7"  # coins, la veille ; « · » ailleurs
    assert u["table"]["rows"][2][1] == "·"
    assert (
        len((await admin.get("/admin/api/usage?days=9999", headers=bearer)).json()["days"]) == 400
    )


@pytest.mark.asyncio
async def test_jobs_pages_list_and_detail(admin, store, bearer):
    now = datetime.now(UTC)
    jid = store.create_job("warm", {"pull": "True"}, "cron", now, status="running")
    store.add_event(jid, "step", "[1/3] pré-chauffe", now)
    store.update_job(jid, log_key=f"jobs/{jid}.log")
    store.create_job("notify", {}, "cli", now - timedelta(hours=1))
    r = await admin.get("/admin/api/jobs", headers=bearer)
    assert r.status_code == 200
    body = r.json()
    assert [j["kind"] for j in body["jobs"]] == ["warm", "notify"]
    assert body["kinds"]["warm"] == {"label": "pré-chauffe", "expected_every_h": 14.0}
    assert [
        j["kind"]
        for j in (await admin.get("/admin/api/jobs?kind=notify", headers=bearer)).json()["jobs"]
    ] == ["notify"]
    r = await admin.get(f"/admin/api/jobs/{jid}", headers=bearer)
    assert r.status_code == 200
    d = r.json()
    assert d["job"]["status"] == "running" and d["events"][0]["text"] == "[1/3] pré-chauffe"
    assert d["log_url"] is None  # pas de bucket dans ce test
    assert (await admin.get("/admin/api/jobs/nope", headers=bearer)).status_code == 404


@pytest.mark.asyncio
async def test_job_log_is_presigned_when_the_bucket_has_it(grid, client, store, bearer):
    jid = store.create_job("warm", {}, "cron", datetime.now(UTC), status="ok")
    store.update_job(jid, log_key=f"jobs/{jid}.log")
    app = _app(grid, client, store, bucket=MemoryBucket())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        d = (await c.get(f"/admin/api/jobs/{jid}", headers=bearer)).json()
    assert d["log_url"] == f"https://bucket.test/jobs/{jid}.log"


# ------------------------------------------------------------------ compteurs de trafic


@pytest.mark.asyncio
async def test_429_and_5xx_are_counted_without_any_address(grid, client, store):
    settings = Settings(rate_read_per_minute=1, rate_write_per_minute=1, s3=None, apns_key="")
    app = create_app(
        settings=settings, state=State(grid=grid, client=client, cache=client.cache, store=store)
    )
    for m in app.user_middleware:
        if m.cls is RateLimitMiddleware:
            m.kwargs["clock"] = lambda: 0.0

    @app.get("/_boom")
    async def boom():
        raise RuntimeError("cassé")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        assert (await c.get("/health")).status_code == 200
        assert (await c.get("/health")).status_code == 429
        assert (await c.get("/_boom", headers={"X-Forwarded-For": "9.9.9.9"})).status_code == 500
    today: date = counts.today()
    assert store.counts_since(today)[today] == {"http_429": 1, "http_5xx": 1}
