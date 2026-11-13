"""Pages du backoffice rendues par le serveur (lot BO6, `docs/backoffice-bo6.md`) : chaque écran a
son adresse et marche sans JavaScript (liens, formulaires, POST puis redirection) ; tri, filtres et
page vivent dans la query string ; htmx ne reçoit que le fragment qu'il vise ; un formulaire sans
jeton `csrf` est refusé ; l'API JSON `/admin/api/*` ne bouge pas (test_admin.py, test_species.py)."""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from champis_api import counts, tools
from champis_api.admin import auth, pages, views
from champis_api.app import State, create_app
from champis_api.settings import Settings
from champis_api.store import Store
from champis_pipeline.species import group_hash, species_groups, species_hash
from conftest import MemoryBucket

KEY = "une-cle-de-test-suffisamment-longue-0123456789"
CEPE = "boletus_edulis"
GIROLLE = "cantharellus_cibarius"
NOW = datetime.now(UTC)


def manifest() -> dict:
    cepes = species_groups()["cepes"]
    return {
        "generated": "2026-11-06T10:00:00",
        "depts_by_hash": {"dfr": [f"{d:02d}" for d in range(1, 97)], "d25": ["25"]},
        "tiles": [
            {
                "kind": "T",
                "species": CEPE,
                "hash": species_hash(CEPE),
                "depts_hash": "dfr",
                "bytes": 2_900_000_000,
                "name": "T-x",
                "key": "tiles/T-x",
            },
            {
                "kind": "T",
                "species": CEPE,
                "hash": species_hash(CEPE),
                "depts_hash": "d25",
                "dept": "25",
                "bytes": 1,
                "name": "T-y",
                "key": "tiles/T-y",
            },
            {
                "kind": "T",
                "species": GIROLLE,
                "hash": "00000000",
                "depts_hash": "dfr",
                "bytes": 10,
                "name": "T-z",
                "key": "tiles/T-z",
            },
            {
                "kind": "G",
                "group": "cepes",
                "hash": group_hash(cepes),
                "depts_hash": "dfr",
                "species": cepes,
                "name": "G-c",
                "key": "tiles/G-c",
            },
        ],
    }


def inventory() -> dict:
    h = species_hash(CEPE)
    return {
        "at": NOW.isoformat(),
        "depts": 96,
        "t": {
            **{sid: {"current": 96, "stale": 0} for sid in species_groups()["cepes"]},
            GIROLLE: {"current": 10, "stale": 86},
        },
        "tiles": [f"T-{CEPE}-{h}-p1.0.0-d0a1b2c.pmtiles", "A-300m-d0a1b2c.pmtiles"],
        "batch": {"ok": 96, "failed": 0, "last": "2026-11-07T20:00:00+00:00"},
    }


def render_key(sid: str = CEPE, zoom: int = 9, current: bool = True) -> str:
    return f"renders/T-{sid}-{species_hash(sid) if current else '0000abcd'}-z{zoom}.png"


def seed_bucket() -> MemoryBucket:
    bucket = MemoryBucket()
    bucket.json["tiles/manifest.json"] = manifest()
    bucket.objects[render_key()] = b"png" * 1000
    bucket.objects[render_key(zoom=10, current=False)] = b"png"
    bucket.objects[render_key(GIROLLE)] = b"png"
    bucket.objects[f"reports/{CEPE}-25.html"] = b"<html>"
    bucket.json[f"reports/{CEPE}-25.json"] = {"species": CEPE, "dept": "25", "auc": 0.712, "n_in_forest": 340}  # fmt: skip
    bucket.objects[f"reports/{CEPE}-25-edge.html"] = b"<html>"
    bucket.json[f"reports/{CEPE}-25-edge.json"] = {
        "species": CEPE,
        "dept": "25",
        "variant": "edge",
        "auc": 0.731,
        "n_in_forest": 340,
        "baseline": {"auc": 0.712},
        "entry": {"auc_gain": 0.019, "met": True, "evaluated": True},
    }
    bucket.objects[f"reports/{GIROLLE}-39.html"] = b"<html>"  # sans fiche JSON : lu dans le nom
    bucket.objects["artifacts/weather_grid.json"] = b"{}"
    bucket.json["artifacts/weather_grid.json.meta.json"] = {"generated": "2026-11-07T21:00:00+00:00", "command": "weather-grid", "sha256": "ab" * 32}  # fmt: skip
    return bucket


def seed_store(store: Store) -> dict[str, str]:
    """Un journal varié : crons finis, une tâche en échec, une en cours, une en file."""
    ids = {}
    ids["warm"] = store.create_job("warm", {}, "cron", NOW - timedelta(hours=3), status="ok")
    store.finish_job(ids["warm"], "ok", 0, NOW - timedelta(hours=2), "2 cellules")
    ids["failed"] = store.create_job(
        "notify", {}, "cron", NOW - timedelta(hours=5), status="failed"
    )
    ids["running"] = store.create_job("tiles", {"depts": "all", "species": GIROLLE, "jobs": "2"}, "admin", NOW - timedelta(minutes=4))  # fmt: skip
    store.start_job(ids["running"], "champis-jobs", NOW - timedelta(minutes=3))
    store.update_job(ids["running"], step_index=2, step_total=4, step_name="niveau 15", progress_done=30, progress_total=120, progress_unit="tuiles")  # fmt: skip
    store.add_event(ids["running"], "info", "[1/4] lecture des T", NOW - timedelta(minutes=3))
    store.add_event(ids["running"], "error", "une tuile illisible", NOW - timedelta(minutes=2))
    ids["queued"] = store.create_job("publish-cache", {}, "admin", NOW)
    store.worker_heartbeat("champis-jobs", NOW, ids["running"], 80_000, 100_000, "abc123", inventory=inventory())  # fmt: skip
    store.bump(counts.today(), "layer_app:cepes", 30)
    store.bump(counts.today(), "layer_web:boletus_edulis", 5)
    store.bump(counts.today(), "web_map_opens", 12)
    return ids


def make_app(grid, client, store, bucket=None):
    settings = Settings(admin_key=KEY, s3=None, apns_key="", rate_write_per_minute=1000)
    state = State(grid=grid, client=client, cache=client.cache, store=store, bucket=bucket)
    return create_app(settings=settings, state=state)


class Shown:
    """Une réponse telle qu'on la lit à l'écran : `text` sans les entités HTML (`d&#39;` → `d'`)."""

    def __init__(self, r: httpx.Response) -> None:
        self.status_code, self.headers, self.content = r.status_code, r.headers, r.content
        self.text = html.unescape(r.text)


class Site:
    """Un navigateur sans JavaScript : cookies gardés, formulaires envoyés avec leur jeton."""

    def __init__(self, http: httpx.AsyncClient, store: Store | None, bucket, ids: dict[str, str]):
        self.http, self.store, self.bucket, self.ids = http, store, bucket, ids

    async def login(self) -> None:
        r = await self.http.post("/admin/login", data={"key": KEY})
        assert r.status_code == 303, r.text

    @property
    def csrf(self) -> str:
        return auth.csrf_token(KEY, self.http.cookies.get("champis_admin"))

    async def get(self, url: str, **kw) -> Shown:
        return Shown(await self.http.get(url, **kw))

    async def fragment(self, url: str, target: str) -> Shown:
        headers = {"HX-Request": "true", "HX-Target": target}
        return Shown(await self.http.get(url, headers=headers))

    async def post(self, url: str, data: dict | None = None, **kw) -> Shown:
        return Shown(await self.http.post(url, data={"csrf": self.csrf, **(data or {})}, **kw))


async def _open(app, store, bucket, ids, login=True):
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        s = Site(c, store, bucket, ids)
        if login:
            await s.login()
        yield s


@pytest_asyncio.fixture
async def site(grid, client, store):
    bucket = seed_bucket()
    ids = seed_store(store)
    async for s in _open(make_app(grid, client, store, bucket), store, bucket, ids):
        yield s


@pytest_asyncio.fixture
async def bare(grid, client):
    """Ni base ni bucket : chaque page le dit au lieu de casser."""
    async for s in _open(make_app(grid, client, None), None, None, {}):
        yield s


@pytest_asyncio.fixture
async def anonymous(grid, client, store):
    async for s in _open(make_app(grid, client, store), store, None, {}, login=False):
        yield s


def is_page(r) -> bool:
    return r.text.lstrip().startswith("<!doctype html>")


# ------------------------------------------------------------------ connexion


@pytest.mark.asyncio
async def test_pages_redirect_to_login_and_come_back(anonymous: Site):
    s = anonymous
    r = await s.get("/admin/jobs?kind=warm&page=2")
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login?next=%2Fadmin%2Fjobs%3Fkind%3Dwarm%26page%3D2"
    # htmx ne suit pas une redirection vers la page de connexion dans un fragment
    r = await s.fragment("/admin/jobs", "jobs-table")
    assert r.status_code == 401 and r.headers["hx-redirect"].startswith("/admin/login")
    r = await s.get(r.headers["hx-redirect"])
    assert r.status_code == 200 and is_page(r) and 'name="key"' in r.text
    assert 'name="next" value="/admin/jobs"' in r.text and "Se déconnecter" not in r.text

    r = await s.http.post("/admin/login", data={"key": "nope", "next": "/admin/usage"})
    assert r.status_code == 401 and "Clé refusée" in r.text and 'value="/admin/usage"' in r.text
    r = await s.http.post("/admin/login", data={"key": KEY, "next": "/admin/usage?days=30"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/usage?days=30"
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    assert (await s.get("/admin/usage?days=30")).status_code == 200
    r = await s.get("/admin")
    assert r.status_code == 303 and r.headers["location"] == "/admin/health"
    r = await s.get("/admin/login?next=/admin/species")  # déjà connecté : tout droit
    assert r.status_code == 303 and r.headers["location"] == "/admin/species"

    # déconnexion : formulaire avec jeton, puis plus de session
    assert (await s.http.post("/admin/logout", data={})).status_code == 403
    r = await s.post("/admin/logout")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    s.http.cookies.clear()
    assert (await s.get("/admin/health")).status_code == 303
    # un POST sans session revient à la connexion, sans adresse de retour
    r = await s.http.post("/admin/tools/warm", data={})
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


@pytest.mark.asyncio
async def test_login_never_redirects_outside_the_backoffice(anonymous: Site):
    for evil in ("https://evil.test/", "//evil.test", "/admin//evil.test", "/admin/../x", "/adminx", "/admin\\x"):  # fmt: skip
        assert pages.safe_next(evil) in (pages.HOME, "/admin/../x"), evil
    assert pages.safe_next("/admin/../x") == "/admin/../x"  # reste sur l'hôte : 404 au pire
    assert pages.safe_next("/admin/jobs?kind=warm") == "/admin/jobs?kind=warm"
    assert pages.safe_next(None) == pages.HOME
    r = await anonymous.http.post("/admin/login", data={"key": KEY, "next": "https://evil.test/"})
    assert r.status_code == 303 and r.headers["location"] == pages.HOME
    r = await anonymous.http.post(
        "/admin/login", data={"key": KEY}, headers={"Origin": "https://evil.test"}
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_login_form_is_rate_limited_and_json_login_is_strict(anonymous: Site):
    s = anonymous
    r = await s.http.post("/admin/login", content=b"[1]", headers={"Content-Type": "application/json", "X-Champis-Admin": "1"})  # fmt: skip
    assert r.status_code == 422
    r = await s.http.post("/admin/login", json={"key": 3}, headers={"X-Champis-Admin": "1"})
    assert r.status_code == 422
    for _ in range(5):
        await s.http.post("/admin/login", data={"key": "nope"})
    r = await s.http.post("/admin/login", data={"key": KEY})
    assert (
        r.status_code == 429
        and "Trop d'essais" in html.unescape(r.text)
        and "retry-after" in r.headers
    )
    assert (await s.http.post("/admin/logout", headers={"X-Champis-Admin": "1"})).status_code == 401


@pytest.mark.asyncio
async def test_without_a_key_no_page_exists(grid, client, store):
    app = create_app(
        settings=Settings(admin_key="", s3=None, apns_key=""),
        state=State(grid=grid, client=client, cache=client.cache, store=store),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        for path in ("/admin/login", "/admin/health", "/admin/species", "/admin/jobs", "/admin/search?q=x", f"/admin/static/{pages.VENDOR['htmx_js']}"):  # fmt: skip
            assert (await c.get(path)).status_code == 404, path
        assert (await c.post("/admin/login", data={"key": "x"})).status_code == 404
        assert (await c.post("/admin/tools/warm", data={})).status_code == 404


@pytest.mark.asyncio
async def test_static_files_are_vendored_and_listed(site: Site):
    for name in pages.STATIC_FILES:
        r = await site.get(f"/admin/static/{name}")
        assert r.status_code == 200 and len(r.content) > 200, name
        assert ("immutable" in r.headers["cache-control"]) == name.startswith("vendor/")
    assert (await site.get("/admin/static/vendor/nope.js")).status_code == 404
    assert (await site.get("/admin/static/../pages.py")).status_code in (400, 404)
    # aucune requête vers un tiers : la page ne charge que /admin/static
    html = (await site.get("/admin/health")).text
    assert "https://" not in html.split("</head>")[0] and "cdn" not in html.lower()
    assert html.count('<script src="/admin/static/') == 3 and "hx-boost" not in html


@pytest.mark.asyncio
async def test_bearer_opens_pages_too(anonymous: Site):
    r = await anonymous.get("/admin/tools", headers={"Authorization": f"Bearer {KEY}"})
    assert r.status_code == 200 and "File d'attente" in r.text


# ------------------------------------------------------------------ santé, usage


@pytest.mark.asyncio
async def test_health_page_and_its_fragment(site: Site):
    r = await site.get("/admin/health")
    assert r.status_code == 200 and is_page(r)
    assert (
        r.headers["x-robots-tag"].startswith("noindex") and r.headers["cache-control"] == "no-store"
    )
    assert "notification « mes coins » : dernière exécution en échec" in r.text
    assert "champis-jobs" in r.text and f"/admin/jobs/{site.ids['running']}" in r.text
    assert 'aria-current="page"' in r.text and "Contrôles" in r.text
    # la pastille du menu compte les tâches en file ou en cours
    assert 'title="tâches en file ou en cours">2<' in r.text
    r = await site.fragment("/admin/health", "health-live")
    assert (
        r.status_code == 200
        and not is_page(r)
        and r.text.lstrip().startswith('<div id="health-live"')
    )
    assert 'hx-trigger="every 60s"' in r.text
    # un fragment inconnu reçoit la page entière : jamais une moitié de page par erreur
    assert is_page(await site.fragment("/admin/health", "autre-chose"))


@pytest.mark.asyncio
async def test_health_says_what_is_missing(bare: Site):
    r = await bare.get("/admin/health")
    assert r.status_code == 200
    assert "Pas de base" in r.text and "Non configuré" in r.text and "Aucun battement" in r.text


@pytest.mark.asyncio
async def test_usage_page(site: Site, bare: Site):
    r = await site.get("/admin/usage")
    assert r.status_code == 200 and "Quatorze derniers jours" in r.text and "<svg" in r.text
    assert "90 jours : 12" in r.text and "30 requêtes météo" in r.text
    assert 'href="/admin/usage?days=30"' in r.text
    r = await site.get("/admin/usage?days=30")
    assert "30 jours : 12" in r.text
    assert (await site.get("/admin/usage?days=abc")).status_code == 200  # valeur illisible : défaut
    r = await bare.get("/admin/usage")
    assert r.status_code == 503 and is_page(r) and "Pas de base de données" in r.text


# ------------------------------------------------------------------ espèces


@pytest.mark.asyncio
async def test_species_list_filters_sorts_in_the_query_string(site: Site):
    r = await site.get("/admin/species")
    assert r.status_code == 200 and "Cèpe de Bordeaux" in r.text and "Girolle" in r.text
    assert f'href="/admin/species/{CEPE}"' in r.text and "Jeu publié du 06/11/2026" in r.text
    total = r.text.count('<a href="/admin/species/')
    assert f"1–{total} sur {total} espèces" in r.text

    r = await site.get("/admin/species?q=bordeaux")
    assert r.text.count('<a href="/admin/species/') == 1 and 'value="bordeaux"' in r.text
    r = await site.get("/admin/species?group=cepes&sort=usage&dir=desc")
    rows = r.text.count('<a href="/admin/species/')
    assert rows == len(species_groups()["cepes"]) and "tri décroissant" in r.text
    # le tri courant suit le formulaire de filtres, et l'en-tête propose le sens inverse
    assert 'name="sort" value="usage" form="species-filters"' in r.text
    assert "/admin/species?group=cepes&sort=usage&dir=asc" in r.text
    r = await site.get("/admin/species?state=en_cours")
    assert "Girolle" in r.text and "Cèpe de Bordeaux" not in r.text
    r = await site.get("/admin/species?state=a_jour&complete=non&q=zzz")
    assert "Aucun résultat" in r.text and "Effacer les filtres" in r.text
    r = await site.get("/admin/species?sort=nimporte&page=99")  # valeurs inconnues : défauts
    assert r.status_code == 200 and "Cèpe de Bordeaux" in r.text

    r = await site.fragment("/admin/species?q=girolle", "species-table")
    assert not is_page(r) and r.text.lstrip().startswith('<div id="species-table"')
    assert "Girolle" in r.text and "Cèpe de Bordeaux" not in r.text


@pytest.mark.asyncio
async def test_species_page_shows_steps_as_forms(site: Site):
    r = await site.get(f"/admin/species/{CEPE}")
    assert r.status_code == 200 and "Cèpe de Bordeaux" in r.text and "Étapes" in r.text
    for kind in ("batch", "tiles", "publish", "weather-grid", "render", "check"):
        assert f'action="/admin/tools/{kind}"' in r.text, kind
    assert r.text.count('name="csrf"') >= 8  # un par formulaire, déconnexion comprise
    # un groupe = une case par espèce : l'outil reçoit la liste, pas une chaîne à découper
    for sid in species_groups()["cepes"]:
        assert f'name="species" value="{sid}"' in r.text
    assert "T à jour 96 / 96 départements" in r.text and "Publier" in r.text
    assert "/admin/renders/open?key=renders/T-boletus_edulis" in r.text and "ancien jeu" in r.text
    assert "AUC 0,731 (+0,019, critère atteint)" in r.text
    assert "/admin/reports/open?key=reports/boletus_edulis-25-edge.html" in r.text
    assert "hx-trigger" not in r.text.split('id="species-live"')[1].split(">")[0]  # rien ne tourne
    r = await site.get(f"/admin/species/{CEPE}?zoom=10")
    assert "Générer (niveau 10)" in r.text and 'name="zoom" value="10"' in r.text

    # une tâche du groupe tourne : boutons éteints, bloc relu par htmx, puis rechargement
    r = await site.get(f"/admin/species/{GIROLLE}")
    live = r.text.split('id="species-live"')[1]
    assert (
        'hx-trigger="every 5s"' in live.split(">")[0]
        and f"/admin/jobs/{site.ids['running']}" in live
    )
    assert "disabled" in live and "Aucune carte HD" not in r.text
    r = await site.fragment(f"/admin/species/{GIROLLE}?live=1", "species-live")
    assert r.status_code == 200 and not is_page(r) and "niveau 15" in r.text
    r = await site.fragment(f"/admin/species/{CEPE}?live=1", "species-live")
    assert r.status_code == 204 and r.headers["hx-refresh"] == "true"

    r = await site.get("/admin/species/amanita_inconnue")
    assert r.status_code == 404 and is_page(r) and "Espèce inconnue" in r.text


@pytest.mark.asyncio
async def test_species_page_without_bucket_or_store(bare: Site):
    r = await bare.get(f"/admin/species/{CEPE}")
    assert r.status_code == 200 and "volume inconnu" in r.text and "Aucun rapport" in r.text
    assert "Bouton éteint" in r.text and "Aucune carte HD" in r.text
    assert (await bare.get("/admin/species")).status_code == 200


@pytest.mark.asyncio
async def test_species_file_page(site: Site):
    r = await site.get(f"/admin/species/{CEPE}/file")
    assert r.status_code == 200 and is_page(r)
    for title in ("Terrain → potentiel T", "Phénologie → fenêtre Φ", "Météo → indice M", "Fiche en mots", "Illustration", "Références"):  # fmt: skip
        assert title in r.text, title
    assert '<path class="line line0"' in r.text and '<svg class="trap"' in r.text
    assert f"/admin/api/species/{CEPE}/illustration" in r.text or "Aucune planche" in r.text
    assert (await site.get("/admin/species/nope/file")).status_code == 404


# ------------------------------------------------------------------ tâches


@pytest.mark.asyncio
async def test_jobs_list_filters_sorts_and_paginates(site: Site):
    for i in range(60):
        site.store.create_job("warm", {"n": str(i)}, "cron", NOW - timedelta(days=1, minutes=i), status="ok")  # fmt: skip
    r = await site.get("/admin/jobs")
    assert r.status_code == 200 and "1–50 sur 64 tâches" in r.text
    assert 'href="/admin/jobs?page=2"' in r.text and 'rel="next"' in r.text
    table = r.text.split("<tbody>")[1]
    assert table.index("publication du cache") < table.index("tuiles PMTiles")  # récente d'abord
    assert 'hx-trigger="every 5s"' in r.text  # une tâche de la page tourne
    r = await site.get("/admin/jobs?page=2")
    assert "51–64 sur 64 tâches" in r.text and 'hx-trigger="every 5s"' not in r.text
    assert (await site.get("/admin/jobs?page=999")).text.count("51–64 sur 64") == 1

    r = await site.get("/admin/jobs?kind=warm&origin=cron&status=ok&page=2")
    assert "51–61 sur 61 tâches" in r.text and 'value="warm" selected' in r.text
    assert "/admin/jobs?kind=warm&status=ok&origin=cron&sort=kind&dir=asc" in r.text
    r = await site.get("/admin/jobs?status=failed")
    assert "1–1 sur 1 tâches" in r.text and "notification" in r.text
    r = await site.get(f"/admin/jobs?q={GIROLLE}")
    assert "1–1 sur 1 tâches" in r.text and "tuiles PMTiles" in r.text
    r = await site.get(f"/admin/jobs?q={site.ids['queued'][:8]}")
    assert "1–1 sur 1 tâches" in r.text
    r = await site.get("/admin/jobs?sort=kind&dir=desc&origin=admin")
    table = r.text.split("<tbody>")[1]
    assert table.index("tuiles PMTiles") < table.index("publication du cache")
    assert 'name="sort" value="kind" form="jobs-filters"' in r.text
    r = await site.get("/admin/jobs?q=introuvable")
    assert "Aucun résultat" in r.text

    # champs vides du formulaire : l'adresse gardée est propre, avec ou sans JavaScript
    r = await site.get("/admin/jobs?q=&kind=&status=failed&origin=")
    assert r.status_code == 303 and r.headers["location"] == "/admin/jobs?status=failed"
    r = await site.fragment("/admin/jobs?q=&kind=warm&status=&origin=", "jobs-table")
    assert r.status_code == 200 and r.headers["hx-push-url"] == "/admin/jobs?kind=warm"
    # le bloc relu toutes les 5 s ne touche pas à l'historique : seuls les liens poussent l'URL
    assert "hx-push-url" not in r.text.split(">")[0] and "hx-push-url" not in r.headers.get("x", "")

    r = await site.fragment("/admin/jobs?status=running", "jobs-table")
    assert "hx-push-url" not in r.headers
    assert not is_page(r) and r.text.lstrip().startswith('<div id="jobs-table"')
    assert 'hx-get="/admin/jobs?status=running"' in r.text and "tuiles PMTiles" in r.text


@pytest.mark.asyncio
async def test_empty_journal_and_missing_store(grid, client, bare: Site):
    store = Store("sqlite://")
    async for s in _open(make_app(grid, client, store), store, None, {}):
        r = await s.get("/admin/jobs")
        assert "Aucune tâche inscrite" in r.text and "Effacer les filtres" not in r.text
        r = await s.get("/admin/tools")
        assert "Rien en file ni en cours" in r.text and "Aucun service d'exécution" in r.text
    for path in ("/admin/jobs", "/admin/jobs/x", "/admin/tools", "/admin/tools/warm"):
        r = await bare.get(path)
        assert r.status_code == 503 and "Pas de base de données" in r.text, path


@pytest.mark.asyncio
async def test_job_page_follows_a_running_job(site: Site):
    r = await site.get(f"/admin/jobs/{site.ids['running']}")
    assert r.status_code == 200 and "tuiles PMTiles" in r.text
    assert "étape 2/4 : niveau 15" in r.text and "30 / 120 tuiles" in r.text
    assert 'style="width:25.0%"' in r.text and "une tuile illisible" in r.text
    assert 'hx-trigger="every 3s"' in r.text and "Annuler cette tâche" in r.text
    assert f"/admin/tools/tiles?depts=all&jobs=2&species={GIROLLE}" in r.text  # relancer
    r = await site.fragment(f"/admin/jobs/{site.ids['running']}", "job-live")
    assert not is_page(r) and r.text.lstrip().startswith('<div id="job-live"')

    r = await site.get(f"/admin/jobs/{site.ids['warm']}")
    assert "2 cellules" in r.text and "hx-trigger" not in r.text and "Annuler cette" not in r.text
    assert "Aucun événement" in r.text and 'style="width:100.0%"' in r.text
    r = await site.get("/admin/jobs/inconnue")
    assert r.status_code == 404 and "Tâche inconnue" in r.text


@pytest.mark.asyncio
async def test_job_log_link_when_the_bucket_has_it(site: Site):
    site.store.update_job(site.ids["warm"], log_key="jobs/x.log")
    r = await site.get(f"/admin/jobs/{site.ids['warm']}")
    assert 'href="https://bucket.test/jobs/x.log"' in r.text


@pytest.mark.asyncio
async def test_cancel_is_post_redirect_get_with_a_message_shown_once(site: Site):
    queued, running = site.ids["queued"], site.ids["running"]
    # sans jeton : refusé, rien n'est annulé
    r = await site.http.post(f"/admin/jobs/{queued}/cancel", data={})
    assert r.status_code == 403 and "Formulaire expiré" in r.text and is_page(r)
    assert site.store.job(queued)["status"] == "queued"
    r = await site.http.post(f"/admin/jobs/{queued}/cancel", data={"csrf": "faux"})
    assert r.status_code == 403

    r = await site.post(f"/admin/jobs/{queued}/cancel", {"next": "/admin/jobs?status=queued"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/jobs?status=queued"
    assert site.store.job(queued)["status"] == "cancelled"
    r = await site.get(r.headers["location"])
    assert "Tâche annulée." in r.text
    assert "Tâche annulée." not in (await site.get("/admin/jobs")).text  # une seule fois

    r = await site.post(f"/admin/jobs/{queued}/cancel")  # déjà finie
    assert r.status_code == 303 and r.headers["location"] == f"/admin/jobs/{queued}"
    assert "rien à annuler" in (await site.get(r.headers["location"])).text
    r = await site.post(f"/admin/jobs/{running}/cancel", {"next": "https://evil.test"})
    assert r.headers["location"] == pages.HOME
    assert "Annulation demandée" in (await site.get("/admin/health")).text


@pytest.mark.asyncio
async def test_cancel_through_htmx_swaps_only_the_row_or_the_live_block(site: Site):
    queued, running = site.ids["queued"], site.ids["running"]
    hx = {"HX-Request": "true", "HX-Target": f"job-row-{queued}"}
    r = await site.post(f"/admin/jobs/{queued}/cancel", headers=hx)
    assert r.status_code == 200 and r.text.lstrip().startswith(f'<tr id="job-row-{queued}"')
    assert "cancelled" in r.text and "<form" not in r.text
    r = await site.post(f"/admin/jobs/{running}/cancel", headers={"HX-Request": "true", "HX-Target": "job-live"})  # fmt: skip
    assert r.status_code == 200 and r.text.lstrip().startswith('<div id="job-live"')
    assert "annulation demandée" in r.text and "Annuler cette tâche" not in r.text
    assert site.store.job(running)["cancel_requested"] is True
    hx = {"HX-Request": "true", "HX-Target": "job-row-inconnue"}
    assert (await site.post("/admin/jobs/inconnue/cancel", headers=hx)).status_code == 303


# ------------------------------------------------------------------ outils


@pytest.mark.asyncio
async def test_tools_index_and_prefilled_form(site: Site):
    r = await site.get("/admin/tools")
    assert r.status_code == 200 and "File d'attente" in r.text and "battement" in r.text
    for kind in tools.TOOLS:
        assert f'href="/admin/tools/{kind}"' in r.text, kind
    assert f'href="/admin/jobs/{site.ids["running"]}"' in r.text
    r = await site.fragment("/admin/tools", "tools-queue")
    assert not is_page(r) and 'hx-trigger="every 5s"' in r.text

    r = await site.get("/admin/tools/batch")
    assert (
        'name="depts" type="text" value="all"' in r.text and 'name="jobs" type="number"' in r.text
    )
    assert 'name="clean_raw" value="true" checked' in r.text
    assert 'name="force" value="true">' in r.text and "Aucune case cochée" in r.text
    r = await site.get(f"/admin/tools/batch?depts=25+39&species={CEPE}+{GIROLLE}&force=true&clean_raw=false&inconnu=1")  # fmt: skip
    assert 'value="25 39"' in r.text and 'name="force" value="true" checked' in r.text
    assert 'name="clean_raw" value="true">' in r.text
    assert f'value="{CEPE}" checked' in r.text and f'value="{GIROLLE}" checked' in r.text
    r = await site.get("/admin/tools/tiles")  # une tâche de cette sorte tourne déjà
    assert "le lancement sera refusé" in r.text
    assert "Cet outil n'a aucune option" in (await site.get("/admin/tools/publish-cache")).text
    assert 'name="edge_min" type="number" step="any" inputmode="decimal" value=""' in (await site.get("/admin/tools/check")).text  # fmt: skip
    r = await site.get("/admin/tools/nope")
    assert r.status_code == 404 and "Outil inconnu" in r.text


@pytest.mark.asyncio
async def test_launch_shows_errors_under_each_field(site: Site):
    before = site.store.count_jobs()
    r = await site.post("/admin/tools/batch", {"depts": "", "jobs": "9", "species": "amanita_x"})
    assert r.status_code == 422 and is_page(r)
    assert "au moins un département, ou all" in r.text and "au plus 4" in r.text
    assert "espèce inconnue amanita_x" in r.text and r.text.count("invalid-feedback") == 3
    assert (
        'id="f-jobs" name="jobs" type="number" step="any" inputmode="decimal" value="9"' in r.text
    )
    assert 'aria-describedby="f-jobs-err"' in r.text  # la saisie refusée reste dans le champ
    r = await site.post("/admin/tools/check", {"depts": "25", "species": CEPE, "edge_min": "10"})
    assert (
        r.status_code == 422
        and "variante lisière : les quatre bornes et le poids, ou rien" in r.text
    )
    assert site.store.count_jobs() == before
    # outil exclusif déjà en cours : refusé, avec le lien vers la tâche
    r = await site.post("/admin/tools/tiles", {"depts": "all", "jobs": "2"})
    assert r.status_code == 409 and "une tâche est déjà running" in r.text
    assert f'href="/admin/jobs/{site.ids["running"]}"' in r.text
    assert site.store.count_jobs() == before
    assert (await site.http.post("/admin/tools/warm", data={})).status_code == 403
    assert (await site.post("/admin/tools/nope")).status_code == 404


@pytest.mark.asyncio
async def test_launch_redirects_to_the_new_job(site: Site):
    r = await site.http.post(
        "/admin/tools/batch",
        content=f"csrf={site.csrf}&depts=25+39&species={CEPE}&species={GIROLLE}&jobs=2&clean_raw=true",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/jobs/")
    job = site.store.job(r.headers["location"].rsplit("/", 1)[1])
    assert job["kind"] == "batch" and job["origin"] == "admin" and job["status"] == "queued"
    # les mêmes options normalisées que l'API JSON : case absente = non
    assert job["args"] == tools.validate("batch", {"depts": "25 39", "species": f"{CEPE} {GIROLLE}", "jobs": 2, "clean_raw": True, "force": False})  # fmt: skip
    r = await site.get(r.headers["location"])
    assert "tâche mise en file" in r.text and "en attente du worker" in r.text


@pytest.mark.asyncio
async def test_destructive_launches_ask_first(site: Site):
    before = site.store.count_jobs()
    real = {"depts": "all", "species": CEPE, "per_dept": "true", "groups": "true", "prune": "false", "dry_run": "false", "back": f"/admin/species/{CEPE}"}  # fmt: skip
    r = await site.post("/admin/tools/publish", real)
    assert r.status_code == 200 and is_page(r) and "Publier dans le bucket" in r.text
    assert 'name="confirmed" value="1"' in r.text and 'name="dry_run" value="False"' in r.text
    assert f'href="/admin/species/{CEPE}"' in r.text and site.store.count_jobs() == before
    r = await site.post("/admin/tools/publish", {**real, "prune": "true"})
    assert "efface du bucket" in r.text and site.store.count_jobs() == before
    # à blanc : pas de question
    r = await site.post("/admin/tools/publish", {**real, "dry_run": "true"})
    assert r.status_code == 303
    site.store.finish_job(r.headers["location"].rsplit("/", 1)[1], "ok", 0, NOW)
    r = await site.post("/admin/tools/publish", {**real, "confirmed": "1"})
    assert r.status_code == 303
    assert site.store.job(r.headers["location"].rsplit("/", 1)[1])["args"]["dry_run"] == "False"
    r = await site.post("/admin/tools/render", {"species": CEPE, "zoom": "9", "force": "true"})
    assert r.status_code == 200 and "remplacer celle du bucket" in r.text


# ------------------------------------------------------------------ cartes HD, rapports, sorties


@pytest.mark.asyncio
async def test_renders_list_open_and_delete(site: Site):
    r = await site.get("/admin/renders")
    assert r.status_code == 200 and "1–3 sur 3 cartes" in r.text
    assert "jeu courant" in r.text and "ancien jeu" in r.text and "3,0 ko" in r.text
    assert "1–1 sur 1 cartes" in (await site.get("/admin/renders?zoom=10")).text
    assert "1–1 sur 1 cartes" in (await site.get("/admin/renders?set=courant")).text
    assert "1–1 sur 1 cartes" in (await site.get("/admin/renders?q=girolle")).text
    r = await site.get("/admin/renders?sort=bytes&dir=desc")
    assert r.text.index("3,0 ko") < r.text.index("3 o")
    assert "Aucun résultat" in (await site.get("/admin/renders?q=zzz")).text
    r = await site.fragment("/admin/renders?zoom=9", "renders-table")
    assert not is_page(r) and "1–2 sur 2 cartes" in r.text

    key = render_key()
    r = await site.get(f"/admin/renders/open?key={key}")
    assert r.status_code == 302 and r.headers["location"] == f"https://bucket.test/{key}"
    for bad in ("", "tiles/manifest.json", "renders/../tiles/x.png"):
        assert (await site.get(f"/admin/renders/open?key={bad}")).status_code == 422
        assert (await site.get(f"/admin/renders/delete?key={bad}")).status_code == 422
        assert (await site.post("/admin/renders/delete", {"key": bad})).status_code == 422

    # sans JavaScript : page de confirmation, POST, redirection, message
    r = await site.get(f"/admin/renders/delete?key={key}&next=/admin/species/{CEPE}")
    assert r.status_code == 200 and "Supprimer" in r.text and f'value="{key}"' in r.text
    assert f'name="next" value="/admin/species/{CEPE}"' in r.text and key in site.bucket.objects
    assert (await site.http.post("/admin/renders/delete", data={"key": key})).status_code == 403
    r = await site.post("/admin/renders/delete", {"key": key, "next": f"/admin/species/{CEPE}"})
    assert r.status_code == 303 and r.headers["location"] == f"/admin/species/{CEPE}"
    assert key not in site.bucket.objects
    assert "Carte supprimée" in (await site.get(r.headers["location"])).text
    r = await site.post("/admin/renders/delete", {"key": key})
    assert r.headers["location"] == "/admin/renders"
    assert "Carte absente du bucket" in (await site.get("/admin/renders")).text
    # avec htmx : la ligne disparaît
    other = render_key(GIROLLE)
    r = await site.post("/admin/renders/delete", {"key": other}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.text == "" and other not in site.bucket.objects


@pytest.mark.asyncio
async def test_reports_list_and_open(site: Site):
    r = await site.get("/admin/reports")
    assert r.status_code == 200 and "1–3 sur 3 rapports" in r.text
    assert (
        "AUC 0,712" in r.text
        and "AUC 0,731 (+0,019, critère atteint)" in r.text
        and "AUC ?" in r.text
    )
    assert "1–1 sur 1 rapports" in (await site.get("/admin/reports?entry=atteint")).text
    assert "1–1 sur 1 rapports" in (await site.get(f"/admin/reports?species={GIROLLE}")).text
    assert "1–2 sur 2 rapports" in (await site.get("/admin/reports?dept=25")).text
    assert "1–1 sur 1 rapports" in (await site.get("/admin/reports?q=edge")).text
    r = await site.get("/admin/reports?sort=auc&dir=desc")  # sans AUC : toujours en dernier
    assert r.text.index("AUC 0,731") < r.text.index("AUC 0,712") < r.text.index("AUC ?")
    r = await site.get("/admin/reports?sort=auc")
    assert r.text.index("AUC 0,712") < r.text.index("AUC 0,731") < r.text.index("AUC ?")
    assert not is_page(await site.fragment("/admin/reports", "reports-table"))
    key = f"reports/{CEPE}-25.html"
    r = await site.get(f"/admin/reports/open?key={key}")
    assert r.status_code == 302 and r.headers["location"] == f"https://bucket.test/{key}"
    for bad in (
        "",
        "reports/x.json",
        "reports/../tiles/x.html",
        "tiles/x.html",
        "reports/a/b.html",
    ):
        assert (await site.get(f"/admin/reports/open?key={bad}")).status_code == 422, bad


@pytest.mark.asyncio
async def test_artifacts_list_and_open(site: Site):
    r = await site.get("/admin/artifacts")
    assert r.status_code == 200 and "weather_grid.json" in r.text and "abababababab" in r.text
    r = await site.get("/admin/artifacts/open?key=artifacts/weather_grid.json")
    assert r.status_code == 302 and r.headers["location"].endswith("artifacts/weather_grid.json")
    for bad in ("", "artifacts/", "artifacts/../x", "tiles/manifest.json"):
        assert (await site.get(f"/admin/artifacts/open?key={bad}")).status_code == 422, bad


@pytest.mark.asyncio
async def test_bucket_pages_say_why_they_are_empty(bare: Site, grid, client, store):
    for path, text in (("/admin/renders", "Bucket non configuré"), ("/admin/reports", "Bucket non configuré"), ("/admin/artifacts", "Bucket non configuré")):  # fmt: skip
        r = await bare.get(path)
        assert r.status_code == 200 and text in r.text, path
    r = await bare.get(f"/admin/renders/open?key={render_key()}")
    assert r.status_code == 503 and "Bucket non configuré" in r.text
    assert (await bare.get("/admin/reports/open?key=reports/a.html")).status_code == 503
    assert (await bare.get("/admin/artifacts/open?key=artifacts/a")).status_code == 503

    class Broken(MemoryBucket):
        def list(self, prefix):
            raise ConnectionError("réseau")

        def presign(self, key, expires_s):
            raise ConnectionError("réseau")

    empty = MemoryBucket()
    async for s in _open(make_app(grid, client, store, empty), store, empty, {}):
        assert "Aucune carte HD" in (await s.get("/admin/renders")).text
        assert "Aucun rapport GBIF" in (await s.get("/admin/reports")).text
        assert "Aucune sortie" in (await s.get("/admin/artifacts")).text
    broken = Broken()
    job = store.create_job("warm", {}, "cron", NOW, status="ok")
    store.update_job(job, log_key="jobs/x.log")
    async for s in _open(make_app(grid, client, store, broken), store, broken, {}):
        for path in ("/admin/renders", "/admin/reports", "/admin/artifacts"):
            r = await s.get(path)
            assert r.status_code == 200 and "Bucket injoignable (ConnectionError)" in r.text, path
        r = await s.get(f"/admin/renders/open?key={render_key()}")
        assert r.status_code == 502 and "Bucket injoignable" in r.text
        r = await s.get(f"/admin/jobs/{job}")  # le journal signé manque, la page s'affiche
        assert r.status_code == 200 and "Journal complet" not in r.text


# ------------------------------------------------------------------ recherche


@pytest.mark.asyncio
async def test_search_goes_straight_to_a_single_hit(site: Site):
    r = await site.get("/admin/search?q=bordeaux")
    assert r.status_code == 303 and r.headers["location"] == f"/admin/species/{CEPE}"
    r = await site.get(f"/admin/search?q={site.ids['running'][:10]}")
    assert r.status_code == 303 and r.headers["location"] == f"/admin/jobs/{site.ids['running']}"
    r = await site.get("/admin/search?q=cèpe")
    assert r.status_code == 200 and "Cèpe de Bordeaux" in r.text and 'value="cèpe"' in r.text
    r = await site.get("/admin/search?q=carte")
    assert "Outil" in r.text and "Page" in r.text and "/admin/tools/render" in r.text
    r = await site.get("/admin/search?q=zzzzzz")
    assert "Aucun résultat" in r.text and "/admin/jobs?q=zzzzzz" in r.text
    assert "Tape un nom" in (await site.get("/admin/search")).text


# ------------------------------------------------------------------ fonctions pures


def test_formatting():
    assert views.fmt_n(1234567) == "1 234 567" and views.fmt_n(None) == "—"
    assert views.fmt_n("abc") == "abc"
    assert views.fmt_num(0.25) == "0,25" and views.fmt_num(600.0, 0) == "600"
    assert (
        views.fmt_num(None) == "—" and views.fmt_num("x") == "x" and views.fmt_num(True) == "True"
    )
    assert views.fmt_size(2_900_000_000) == "2,9 Go" and views.fmt_size(150_000_000) == "150 Mo"
    assert views.fmt_size(12) == "12 o" and views.fmt_size(None) == "—"
    assert views.fmt_when("2026-11-06T10:00:00+00:00") == "06/11/2026 11:00"  # heure de Paris
    assert views.fmt_when("2026-07-01T10:00:00Z") == "01/07/2026 12:00"
    assert views.fmt_when(None) == "jamais" and views.fmt_when("2026-10-17") == "17/10/2026 02:00"
    assert views.fmt_when("pas une date") == "pas une date" and views.parse_iso(3) is None
    assert views.fmt_time("2026-11-06T10:00:05+00:00") == "11:00:05" and views.fmt_time(None) == ""
    now = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
    assert views.fmt_ago("2026-11-13T11:30:00+00:00", now) == "il y a 30 min"
    assert views.fmt_ago("2026-11-13T02:00:00+00:00", now) == "il y a 10 h"
    assert (
        views.fmt_ago(datetime(2026, 11, 3, 12, 0), now) == "il y a 10 j"
        and views.fmt_ago(None) == ""
    )
    assert views.fmt_duration("2026-11-13T11:59:30+00:00", None, now) == "30 s"
    assert (
        views.fmt_duration("2026-11-13T11:30:00+00:00", "2026-11-13T11:42:05+00:00")
        == "12 min 05 s"
    )
    assert views.fmt_duration("2026-11-13T09:00:00+00:00", None, now) == "3 h 00 min"
    assert views.fmt_duration(None) == "—"
    assert (
        views.last_word({"step_name": "tuiles", "step_index": 1, "step_total": 3}) == "[1/3] tuiles"
    )
    assert views.last_word({}) == "" and views.job_percent({}) == 0.0
    assert views.job_percent({"step_index": 3, "step_total": 4}) == 50.0
    assert views.completeness_text(None) == ""
    assert views.completeness_text({"complete": False, "missing": ["photo", "tiles"]}) == "manque : photo, tuiles publiées"  # fmt: skip


def test_listing_keeps_its_state_in_the_url():
    lst = views.listing("/admin/jobs", {"q": " cèpe ", "kind": "warm", "sort": "kind", "dir": "desc", "page": "3", "x": "1"}, ("q", "kind", "status"), ("created_at", "kind"), "created_at", True)  # fmt: skip
    assert lst.params == {"q": "cèpe", "kind": "warm", "status": ""} and lst.filtered
    assert lst.url() == "/admin/jobs?q=c%C3%A8pe&kind=warm&sort=kind&dir=desc&page=3"
    assert lst.url(page=1) == "/admin/jobs?q=c%C3%A8pe&kind=warm&sort=kind&dir=desc"
    assert lst.sort_url("kind").endswith("sort=kind&dir=asc")  # même colonne : sens inverse
    # autre colonne : croissant d'abord ; retomber sur le tri par défaut ne s'écrit pas dans l'URL
    assert lst.sort_url("created_at").endswith("kind=warm&sort=created_at&dir=asc")
    by_date = views.listing("/admin/jobs", {"sort": "created_at", "dir": "asc"}, (), ("created_at",), "created_at", True)  # fmt: skip
    assert by_date.url() == "/admin/jobs?sort=created_at&dir=asc"
    assert by_date.sort_url("created_at") == "/admin/jobs"
    assert lst.arrow("kind") == "↓" and lst.arrow("created_at") == ""
    plain = views.listing("/admin/jobs", {"sort": "drop table", "dir": "desc", "page": "-4"}, ("q",), ("created_at",), "created_at", True)  # fmt: skip
    assert (plain.sort, plain.desc, plain.page) == ("created_at", True, 1)
    assert plain.url() == "/admin/jobs" and not plain.filtered
    assert views.to_int("abc", 7, 1, 9) == 7 and views.to_int("99", 7, 1, 9) == 9

    page = views.paginate(list(range(120)), 3)
    assert (page.first, page.last, page.pages, page.window) == (101, 120, 3, [1, 2, 3])
    assert views.paginate([], 5).first == 0 and views.paginate(list(range(120)), 99).page == 3
    assert views.matches("cèpe BORD", "Cèpe de Bordeaux", None) and not views.matches(
        "cèpe x", "Cèpe"
    )
    rows = [{"v": 2}, {"v": None}, {"v": 5}]
    lst = views.Listing("/x", sort="v", desc=True)
    assert [r["v"] for r in views.sort_items(rows, {"v": lambda r: r["v"]}, lst)] == [5, 2, None]
    assert views.sort_items(rows, {}, lst) == rows


def test_charts_are_computed_on_the_server():
    s = views.sparkline(["a", "b", "c"], {"a": 4, "c": 2})
    assert [b["h"] for b in s["bars"]] == [48.0, 1.0, 24.0] and s["bars"][1]["empty"]
    assert s["bars"][1]["title"] == "b : ·" and views.sparkline([], {})["bars"] == []
    t = views.trapezoid({"min": 0, "opt_low": 5, "opt_high": 15, "max": 20}, "°C")
    assert t["label"] == "0 · 5–15 · 20 °C" and t["points"].startswith("2.0,23 41.0,3")
    assert views.trapezoid({"min": 3, "opt_low": 3, "opt_high": 3, "max": 3})[
        "points"
    ]  # plage nulle
    assert not views.is_trapezoid({"min": 1}) and not views.is_trapezoid(3)
    chart = views.phi_chart([{"altitude_m": 600, "days": [0.0, 1.0] + [0.25] * 363}])
    assert chart["curves"][0]["d"].startswith("M6.0,6") or chart["curves"][0]["d"].startswith(
        "M6.0,78"
    )
    assert len(chart["months"]) == 12 and chart["months"][0]["label"] == "janv."
    bars = views.weight_bars({"default": 0.1, "chene": 1.0, "hetre": 0.6, "note": "x"})
    assert [b["key"] for b in bars] == ["chene", "hetre", "default"] and bars[0]["pct"] == 100
    assert [b["key"] for b in views.weight_bars({"S": 1, "N": 0.2}, ("N", "S"))] == ["N", "S"]
    assert views.share_bars({})["total"] == 0
    assert views.share_bars({"a": 3, "b": 1})["rows"][0] == {"key": "a", "n": 3, "pct": 75}
    p = views.params({"a": 1.5, "t": {"min": 0, "opt_low": 1, "opt_high": 2, "max": 3}, "l": [1], "skip": 1}, {"a": "mm"}, ("skip",))  # fmt: skip
    assert (
        p[0] == {"key": "a", "text": "1,5 mm"}
        and "trapezoid" in p[1]
        and p[2] == {"key": "l", "raw": [1]}
    )
    sections = views.file_sections({"file": {"id": "x", "futur": 1, "weather": {"temp_mean": {"min": 0, "opt_low": 1, "opt_high": 2, "max": 3}, "rain": {"mm": 20}, "autre": 0.5, "weights": {"rain": 1}}}, "phi": []})  # fmt: skip
    assert sections["extra"] == {"futur": 1} and sections["phenology"]["chart"] is None
    assert [f["key"] for f in sections["weather"]["factors"]] == ["temp_mean", "rain", "autre"]
    assert sections["weather"]["factors"][2]["text"] == "0,5"


def test_steps_and_confirmations():
    sp = {"id": CEPE, "hash": "h", "group": {"name": "Cèpes", "species": [CEPE, "boletus_aereus"]}, "published": None, "worker": None, "jobs": [], "maps": []}  # fmt: skip
    steps = {s.key: s for s in views.species_steps(sp, {"current": False, "hash": None, "current_hash": "abc"}, 9)}  # fmt: skip
    assert list(steps) == ["batch", "tiles", "publish-dry", "publish", "weather-grid", "render"]
    assert steps["batch"].disabled.startswith("volume inconnu") and steps["batch"].args["species"] == f"{CEPE} boletus_aereus"  # fmt: skip
    assert steps["publish"].args["dry_run"] == "false" and steps["publish"].args["prune"] == "false"
    assert (
        steps["render"].disabled == "aucun jeu France publié"
        and "absente ≠ abc" in steps["weather-grid"].detail
    )
    sp["worker"] = {"t_current": 1, "t_stale": 0, "depts": 2, "batch_last": None, "tiles_current": 0, "tiles_stale": 0, "group_missing_t": ["boletus_aereus"]}  # fmt: skip
    sp["jobs"] = [{"id": "j"}]
    steps = {s.key: s for s in views.species_steps(sp, {"current": True, "hash": "abc"}, 10)}
    assert steps["batch"].dot == "warn" and "dernier batch jamais" in steps["batch"].detail
    assert "T à calculer d'abord : boletus_aereus" in steps["tiles"].detail
    assert all(s.disabled == "une tâche de l'espèce est en cours" for s in steps.values())
    sp["jobs"] = []
    steps = {s.key: s for s in views.species_steps(sp, {"current": True, "hash": "abc"}, 10)}
    assert steps["tiles"].disabled.startswith("le terrain du groupe") and steps["publish"].disabled

    assert views.confirmation("warm", {}) is None
    assert views.confirmation("publish", {"dry_run": "True", "prune": "True"}) is None
    assert "toutes les espèces" in views.confirmation("publish", {"dry_run": "False", "prune": "False", "species": ""})  # fmt: skip
    assert views.confirmation("publish", {"dry_run": "False", "prune": "True"}) == tools.TOOLS["publish"].confirm  # fmt: skip
    assert views.confirmation("render", {"force": "False"}) is None
    assert views.check_summary({"auc": 0.6, "entry": {"auc_gain": -0.01, "evaluated": True}}) == "AUC 0,6 (−0,01, critère non atteint)"  # fmt: skip
    assert (
        views.check_summary({"auc": 0.6, "entry": {"auc_gain": 0.0}}) == "AUC 0,6 (+0, non évalué)"
    )


def test_tokens_for_forms_and_messages():
    token = auth.csrf_token(KEY, "session-a")
    assert auth.csrf_valid(KEY, "session-a", token) and not auth.csrf_valid(KEY, "session-b", token)
    assert not auth.csrf_valid(KEY, "session-a", None) and not auth.csrf_valid(KEY, "session-a", "")
    assert not auth.csrf_valid("autre-cle-tout-aussi-longue-0123456789", "session-a", token)
    signed = auth.sign(KEY, "success|ok")
    assert auth.unsign(KEY, signed) == "success|ok"
    assert auth.unsign(KEY, "0" * 24 + ".success|ok") is None and auth.unsign(KEY, "rien") is None
    assert auth.unsign(KEY, None) is None


def test_validate_fields_reports_every_error():
    args, errors = tools.validate_fields("batch", {"depts": "", "jobs": "x", "inconnu": 1})
    assert set(errors) == {tools.FORM, "depts", "jobs"} and args["clean_raw"] == "True"
    with pytest.raises(tools.ToolError, match="options refusées : inconnu"):
        tools.validate("batch", {"inconnu": 1})
    with pytest.raises(tools.ToolError, match="jobs : nombre attendu"):
        tools.validate("batch", {"jobs": ""})
    with pytest.raises(tools.ToolError, match="outil inconnu"):
        tools.validate_fields("nope", {})
    _, errors = tools.validate_fields("render", {"species": "nope", "zoom": "9"})
    assert "valeur inconnue" in errors["species"]


def test_store_lists_jobs_by_filter_sort_and_page():
    store = Store("sqlite://")
    ids = seed_store(store)
    assert store.count_jobs() == 4 and store.count_jobs(kind="warm", status="ok") == 1
    assert store.count_jobs(origin="admin") == 2 and store.count_jobs(search=GIROLLE) == 1
    assert [j["id"] for j in store.list_jobs(1, offset=1)] == [ids["running"]]
    kinds = [j["kind"] for j in store.list_jobs(10, sort="kind", descending=False)]
    assert kinds == sorted(kinds)
    assert next(j["id"] for j in store.list_jobs(10, sort="nimporte")) == ids["queued"]
    assert store.list_jobs(10, search=ids["warm"][:8])[0]["id"] == ids["warm"]
