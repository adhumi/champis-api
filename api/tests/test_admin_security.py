"""Backoffice, correctifs du 14 nov. 2026 : `Bearer` refusés comptés avec les essais de connexion,
session révocable connue du serveur (déconnexion, toutes les sessions, redéploiement, plusieurs
workers), jeton `csrf` propre à la session, en-têtes de sécurité sur toutes les réponses `/admin`
et gabarits sans script ni style en ligne (la CSP les refuserait)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from champis_api import tools
from champis_api.admin import auth, pages, security
from champis_api.app import State, create_app
from champis_api.settings import Settings
from champis_api.store import Store, admin_sessions
from sqlalchemy import select, update
from test_admin_pages import CEPE, KEY, Site, _open, make_app, seed_bucket, seed_store

ACTION = {"X-Champis-Admin": "1"}
BEARER = {"Authorization": f"Bearer {KEY}"}


@pytest_asyncio.fixture
async def site(grid, client, store):
    bucket = seed_bucket()
    ids = seed_store(store)
    async for s in _open(make_app(grid, client, store, bucket), store, bucket, ids):
        yield s


@pytest_asyncio.fixture
async def anonymous(grid, client, store):
    async for s in _open(make_app(grid, client, store), store, None, {}, login=False):
        yield s


def _client(app, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", **kw)


def _digests(store: Store) -> list[str]:
    with store.engine.connect() as conn:
        return [r[0] for r in conn.execute(select(admin_sessions.c.id))]


# ------------------------------------------------------------------ essais (Bearer)


@pytest.mark.asyncio
async def test_refused_bearers_share_the_login_bucket(anonymous: Site):
    c = anonymous.http
    # la bonne clé ne prend rien au seau : un script peut enchaîner les appels
    for _ in range(12):
        assert (await c.get("/admin/api/me", headers=BEARER)).status_code == 200
    bad = {"Authorization": "Bearer pas-la-bonne-cle"}
    for path in ("/admin/api/me", "/admin/api/health", "/admin/api/jobs", "/admin/jobs"):
        assert (await c.get(path, headers=bad)).status_code in (401, 303), path
    assert (await c.get("/admin/api/me", headers=bad)).status_code == 401  # le cinquième
    # seau vide : 429, sans comparaison ; la bonne clé ne passe pas non plus de cette adresse
    r = await c.get("/admin/api/me", headers=bad)
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert (await c.get("/admin/api/me", headers=BEARER)).status_code == 429
    assert (await c.get("/admin/usage", headers=BEARER)).status_code == 429
    # c'est le même seau que le formulaire de connexion
    assert (await c.post("/admin/login", data={"key": KEY})).status_code == 429
    r = await c.post("/admin/login", json={"key": KEY}, headers=ACTION)
    assert r.status_code == 429
    # une autre adresse n'est pas touchée
    other = {**BEARER, "X-Real-IP": "5.6.7.8"}
    assert (await c.get("/admin/api/me", headers=other)).status_code == 200


@pytest.mark.asyncio
async def test_failed_logins_also_block_bearers(anonymous: Site):
    c = anonymous.http
    for _ in range(5):
        assert (await c.post("/admin/login", data={"key": "nope"})).status_code == 401
    assert (await c.get("/admin/api/me", headers=BEARER)).status_code == 429


# ------------------------------------------------------------------ sessions


@pytest.mark.asyncio
async def test_sessions_are_known_to_the_server_and_revocable(grid, client, store):
    app = make_app(grid, client, store)
    async with app.router.lifespan_context(app), _client(app) as a, _client(app) as b:
        for c in (a, b):
            assert (await c.post("/admin/login", data={"key": KEY})).status_code == 303
        cookie_a, cookie_b = a.cookies.get("champis_admin"), b.cookies.get("champis_admin")
        assert cookie_a != cookie_b  # une session par connexion, plus un cookie commun
        # la base ne garde que le condensé de l'identifiant : pas de quoi refaire un cookie
        digests = _digests(store)
        assert len(digests) == 2 and store.table_counts()["admin_sessions"] == 2
        for cookie in (cookie_a, cookie_b):
            sid = auth.session_id_of(KEY, cookie)
            assert auth.session_digest(sid) in digests and sid not in digests

        # le jeton csrf est propre à la session
        csrf_a, csrf_b = auth.csrf_token(KEY, cookie_a), auth.csrf_token(KEY, cookie_b)
        assert csrf_a != csrf_b
        assert csrf_a in (await a.get("/admin/health")).text
        r = await b.post("/admin/logout", data={"csrf": csrf_a})
        assert r.status_code == 403 and len(_digests(store)) == 2

        # déconnexion de A : sa session n'existe plus, son cookie rejoué ne vaut plus rien
        r = await a.post("/admin/logout", data={"csrf": csrf_a})
        assert r.status_code == 303 and len(_digests(store)) == 1
        replay = {"Cookie": f"champis_admin={cookie_a}"}
        async with _client(app) as thief:
            assert (await thief.get("/admin/api/me", headers=replay)).status_code == 401
            r = await thief.get("/admin/health", headers=replay)
            assert r.status_code == 303 and r.headers["location"] == "/admin/login"
            r = await thief.post("/admin/jobs/x/cancel", data={"csrf": csrf_a}, headers=replay)
            assert r.status_code == 303  # le jeton csrf est mort avec la session
        assert (await b.get("/admin/api/me")).status_code == 200  # B n'est pas touché

        # « déconnecter toutes les sessions » : B, et celle qu'on vient de rouvrir pour A
        assert (await a.post("/admin/login", data={"key": KEY})).status_code == 303
        cookie_a2 = a.cookies.get("champis_admin")
        page = (await b.get("/admin/health")).text
        assert 'formaction="/admin/logout/all"' in page
        assert (await b.post("/admin/logout/all", data={"csrf": "faux"})).status_code == 403
        r = await b.post("/admin/logout/all", data={"csrf": csrf_b})
        assert r.status_code == 303 and r.headers["location"] == "/admin/login"
        assert _digests(store) == []
        async with _client(app) as thief:
            for cookie in (cookie_a2, cookie_b):
                r = await thief.get("/admin/api/me", headers={"Cookie": f"champis_admin={cookie}"})
                assert r.status_code == 401
        # les scripts : en-tête d'action et Bearer
        assert (await a.post("/admin/login", data={"key": KEY})).status_code == 303
        async with _client(app) as script:
            assert (await script.post("/admin/logout/all", headers=ACTION)).status_code == 401
            r = await script.post("/admin/logout/all", headers={**ACTION, **BEARER})
            assert r.status_code == 204
        assert _digests(store) == []


@pytest.mark.asyncio
async def test_sessions_survive_a_redeploy_expire_and_die_with_the_key(grid, client, tmp_path):
    url = f"sqlite:///{tmp_path / 'champis.db'}"
    store = Store(url)
    app = make_app(grid, client, store)
    async with app.router.lifespan_context(app), _client(app) as c:
        assert (await c.post("/admin/login", data={"key": KEY})).status_code == 303
        cookie = {"Cookie": f"champis_admin={c.cookies.get('champis_admin')}"}
    store.close()

    # un autre processus (autre worker, déploiement suivant) : la session est dans la base
    again = Store(url)
    app = make_app(grid, client, again)
    async with app.router.lifespan_context(app), _client(app) as c:
        assert (await c.get("/admin/api/me", headers=cookie)).status_code == 200
        # l'ancien cookie `<expiration>.<signature>`, encore valable 30 jours avant ce lot
        old = {"Cookie": "champis_admin=1999999999.0123456789abcdef0123456789abcdef01234567"}
        assert (await c.get("/admin/api/me", headers=old)).status_code == 401
        # une session expirée ne vaut plus rien, et part à la connexion suivante
        past = datetime.now(UTC) - timedelta(seconds=1)
        with again.engine.begin() as conn:
            conn.execute(update(admin_sessions).values(expires_at=past))
        assert (await c.get("/admin/api/me", headers=cookie)).status_code == 401
        assert (await c.post("/admin/login", data={"key": KEY})).status_code == 303
        assert len(_digests(again)) == 1
    # changer la clé invalide toutes les sessions (signature du cookie)
    other = "une-autre-cle-de-test-tout-aussi-longue-0123456789"
    settings = Settings(admin_key=other, s3=None, apns_key="", database_url="sqlite://")
    app = create_app(
        settings=settings, state=State(grid=grid, client=client, cache=client.cache, store=again)
    )
    async with app.router.lifespan_context(app), _client(app) as c:
        assert (await c.get("/admin/api/me", headers=cookie)).status_code == 401
    again.close()


@pytest.mark.asyncio
async def test_login_needs_the_base_to_record_the_session(anonymous: Site, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("base injoignable")

    monkeypatch.setattr(anonymous.store, "create_admin_session", down)
    r = await anonymous.http.post("/admin/login", data={"key": KEY})
    assert r.status_code == 503 and "set-cookie" not in r.headers
    assert "la base ne répond pas" in r.text and "Bearer" in r.text
    r = await anonymous.http.post("/admin/login", json={"key": KEY}, headers=ACTION)
    assert r.status_code == 503
    monkeypatch.setattr(anonymous.store, "admin_session_valid", down)
    r = await anonymous.http.get("/admin/api/me", headers={"Cookie": "champis_admin=x.y"})
    assert r.status_code == 401
    sid = auth.new_session_id()
    signed = {"Cookie": f"champis_admin={auth.session_cookie(KEY, sid)}"}
    assert (await anonymous.http.get("/admin/api/me", headers=signed)).status_code == 401
    assert (await anonymous.http.get("/admin/api/me", headers=BEARER)).status_code == 200


# ------------------------------------------------------------------ en-têtes


def _secured(r: httpx.Response) -> bool:
    h = r.headers
    return (
        h.get("content-security-policy") == security.CSP
        and h.get("x-frame-options") == "DENY"
        and h.get("x-content-type-options") == "nosniff"
        # pas `no-referrer` : les formulaires partiraient avec `Origin: null`, refusé (403)
        and h.get("referrer-policy") == "same-origin"
    )


def test_csp_allows_no_inline_script_or_style():
    csp = dict(d.split(" ", 1) for d in security.CSP.split("; "))
    assert csp["default-src"] == "'none'" and csp["frame-ancestors"] == "'none'"
    assert csp["script-src"] == "'self'" and csp["style-src"] == "'self'"
    assert "unsafe" not in security.CSP and "*" not in security.CSP
    assert csp["form-action"] == "'self'" and csp["base-uri"] == "'none'"


@pytest.mark.asyncio
async def test_every_admin_response_carries_the_security_headers(site: Site):
    c = site.http
    responses = {
        "page": await c.get("/admin/health"),
        "fragment": await c.get(
            "/admin/jobs", headers={"HX-Request": "true", "HX-Target": "jobs-table"}
        ),
        "redirection": await c.get("/admin"),
        "fichier": await c.get(f"/admin/static/{pages.VENDOR['htmx_js']}"),
        "noscript": await c.get("/admin/static/noscript.css"),
        "404": await c.get("/admin/nulle-part"),
        "erreur de page": await c.get("/admin/species/inconnue"),
        "JSON": await c.get("/admin/api/me"),
        "422": await c.post("/admin/api/jobs", json={"kind": "x"}, headers=ACTION),
        "planche": await c.get(f"/admin/api/species/{CEPE}/illustration"),
    }
    async with _client(c._transport.app) as other:
        responses["401"] = await other.get("/admin/api/me")
        responses["connexion"] = await other.get("/admin/login")
        responses["refus"] = await other.post("/admin/login", data={"key": "nope"})
    for name, r in responses.items():
        assert _secured(r), name
        assert "strict-transport-security" not in r.headers, name  # http en test
    assert responses["noscript"].status_code == 200 and responses["planche"].status_code == 200
    # derrière HTTPS (TLS terminé par Railway) : HSTS, et les cookies sont `Secure`
    https = {"X-Forwarded-Proto": "https"}
    r = await c.get("/admin/health", headers=https)
    assert r.headers["strict-transport-security"].startswith("max-age=31536000")
    r = await c.post(f"/admin/jobs/{site.ids['queued']}/cancel", data={"csrf": site.csrf}, headers=https)  # fmt: skip
    flash = r.headers["set-cookie"]
    assert "champis_flash=" in flash and "Secure" in flash and "HttpOnly" in flash
    async with _client(c._transport.app) as other:
        r = await other.post("/admin/login", data={"key": KEY}, headers=https)
        cookie = r.headers["set-cookie"]
        assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "Path=/admin" in cookie and "Max-Age=2592000" in cookie
    # le reste de l'API n'est pas concerné
    r = await c.get("/health")
    assert "content-security-policy" not in r.headers and "x-frame-options" not in r.headers
    assert "content-security-policy" not in (await c.get("/administration")).headers


@pytest.mark.asyncio
async def test_security_headers_also_cover_the_limiter_and_a_closed_backoffice(grid, client, store):
    settings = Settings(admin_key=KEY, s3=None, apns_key="", rate_read_per_minute=1)
    app = create_app(
        settings=settings, state=State(grid=grid, client=client, cache=client.cache, store=store)
    )
    async with app.router.lifespan_context(app), _client(app) as c:
        assert _secured(await c.get("/admin/login"))
        r = await c.get("/admin/login")
        assert r.status_code == 429 and _secured(r)
    closed = create_app(
        settings=Settings(admin_key="", s3=None, apns_key=""),
        state=State(grid=grid, client=client, cache=client.cache, store=store),
    )
    async with closed.router.lifespan_context(closed), _client(closed) as c:
        r = await c.get("/admin")
        assert r.status_code == 404 and _secured(r)


INLINE = (
    (re.compile(r"<style\b", re.I), "balise <style>"),
    (re.compile(r"<script\b(?![^>]*\bsrc=)", re.I), "script en ligne"),
    (re.compile(r"<[a-z][^<>]*\sstyle\s*=", re.I), "attribut style"),
    (re.compile(r"<[a-z][^<>]*\son[a-z]+\s*=", re.I), "attribut on…="),
    (re.compile(r"<[a-z][^<>]*\shx-on[:-]", re.I), "attribut hx-on"),
    (re.compile(r"javascript:", re.I), "adresse javascript:"),
    (re.compile(r"<(?:script|link|img)\b[^>]*(?:src|href)=[\"'](?:https?:)?//", re.I), "tiers"),
)


@pytest.mark.asyncio
async def test_no_screen_carries_inline_script_or_style(site: Site, anonymous: Site):
    """La CSP refuse scripts et styles en ligne : chaque écran, plein et en fragment, n'en porte
    aucun, et ne charge rien d'un autre hôte."""
    ids = site.ids
    screens = [
        "/admin/health",
        "/admin/usage",
        "/admin/usage?days=365",
        "/admin/species",
        f"/admin/species/{CEPE}",
        f"/admin/species/{CEPE}/file",
        "/admin/jobs",
        *(f"/admin/jobs/{ids[k]}" for k in ("warm", "failed", "running", "queued")),
        "/admin/tools",
        *(f"/admin/tools/{t['kind']}" for t in tools.describe()),
        "/admin/renders",
        "/admin/renders/delete?key=renders/T-x-00000000-z9.png",
        "/admin/reports",
        "/admin/artifacts",
        "/admin/search?q=c",
        "/admin/species/inconnue",
    ]
    fragments = [
        ("/admin/health", "health-live"),
        ("/admin/species", "species-table"),
        (f"/admin/species/{CEPE}", "species-live"),
        ("/admin/jobs", "jobs-table"),
        (f"/admin/jobs/{ids['running']}", "job-live"),
        ("/admin/tools", "tools-queue"),
        ("/admin/renders", "renders-table"),
        ("/admin/reports", "reports-table"),
    ]
    texts = {}
    for url in screens:
        r = await site.http.get(url)
        assert r.status_code in (200, 404, 422), url
        assert r.headers["content-type"].startswith("text/html"), url
        texts[url] = r.text
    for url, target in fragments:
        r = await site.http.get(url, headers={"HX-Request": "true", "HX-Target": target})
        assert r.status_code == 200 and not r.text.lstrip().startswith("<!doctype"), url
        texts[f"{url} #{target}"] = r.text
    texts["connexion"] = (await anonymous.http.get("/admin/login")).text
    texts["refus"] = (await anonymous.http.post("/admin/login", data={"key": "x"})).text
    assert len(texts) > 30
    for name, text in texts.items():
        for pattern, what in INLINE:
            found = pattern.search(text)
            assert found is None, f"{name} : {what} ({text[found.start() : found.start() + 120]!r})"
    # htmx n'injecte pas sa feuille d'indicateurs et n'évalue rien
    assert '"includeIndicatorStyles":false' in texts["/admin/health"]
    assert '"allowEval":false' in texts["/admin/health"]
    # les barres restent là, sans style : SVG et <progress>
    assert '<svg class="bar"' in texts["/admin/usage"]
    assert "<progress" in texts[f"/admin/jobs/{ids['running']}"]
