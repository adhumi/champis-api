"""Service champis-api : `/species`, `/weather`, `/weather/bbox`, `/weather/field`, `/departements`,
`/observations`, `/coins`, `/health`, le backoffice privé `/admin` (spec › Backoffice) et, en
développement, la carte web statique sous `/web`.

Sans état hors le cache météo SQLite ; aucun calcul géographique (tout est dans les tuiles).

Plusieurs workers uvicorn (décision du 4 nov. 2026, spec › Points ouverts › Tuiles hors du
processus Python) : chaque processus a son propre état en mémoire (limiteur de débit, cache du
manifeste `/tiles`, connexion SQLite en WAL) et un seul, le **meneur** (verrou `flock` sur le
volume), tient les tâches de fond : import du cache publié par le service cron (toutes les
`CHAMPIS_CACHE_POLL_MINUTES`) et, en repli sans cron, la pré-chauffe intégrée
(`CHAMPIS_WARM_HOURS`, qui bloque ce worker pendant la passe) et la notification « mes coins »
(`CHAMPIS_NOTIFY_HOURS`). L'import de l'amorçage au démarrage est sérialisé par un second verrou.
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from champis_pipeline.species import species_groups
from champis_pipeline.storage import Bucket, BucketLike
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import catalog, counts
from .admin import pages as admin_pages
from .admin.router import router as admin_router
from .apns import ApnsClient
from .cache import WeatherCache
from .coins import notify_all
from .departements import compute as compute_dept_index
from .grid import WeatherGrid
from .locks import file_lock, try_lock
from .ratelimit import RateLimitMiddleware
from .routers import coins, departements, observations, species, tiles, weather
from .seed import sync_seed
from .settings import Settings
from .snapshot import META_CACHE_PUBLISHED, sync_cache
from .store import Store
from .tile_cache import TileCache
from .warm import WARM_INTERVAL_S, warm
from .weather_client import OpenMeteoClient

log = logging.getLogger(__name__)
PARIS = ZoneInfo("Europe/Paris")
# Chemins hors limitation de débit : relais des PMTiles (`/tiles/<nom>`, pas le manifeste `/tiles`)
# et fichiers statiques du site. Lectures nombreuses, sans calcul ni appel externe.
RATE_LIMIT_EXEMPT = ("/tiles/", "/web/", "/shared/", "/species/")
# La page de carte du site appelle `GET /tiles` une fois à l'ouverture : c'est elle que la mesure
# minimale compte (étape C2), reconnue à l'en-tête Origin du site ; l'app iOS n'en envoie pas.
MAP_OPEN_PATH = "/tiles"
SEED_LOCK = ".champis-seed.lock"  # à côté du cache : import de l'amorçage, un worker à la fois
LEADER_LOCK = ".champis-leader.lock"  # tenu par le worker meneur tant qu'il vit


@dataclass
class State:
    grid: WeatherGrid
    client: OpenMeteoClient
    cache: WeatherCache
    bucket: BucketLike | None = None  # PMTiles (spec › hébergement) ; None = glisser-déposer seul
    tiles_mode: str = "presign"
    tiles_url_hours: int = 24
    tile_cache: TileCache | None = (
        None  # plages relayées gardées sur le volume ; None = relais seul
    )
    store: Store | None = None  # observations et coins (Postgres) ; None = endpoints en 503
    apns: ApnsClient | None = None  # notifications « mes coins » ; None = jamais envoyées


def _configure_logging() -> None:
    """Uvicorn ne règle que ses propres loggers : sans ceci, les INFO de champis_api sont muets."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("champis_api").setLevel(logging.INFO)


def create_app(settings: Settings | None = None, state: State | None = None) -> FastAPI:
    settings = settings or Settings()
    _configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state is not None:
            app.state.champis = state
            catalog.configure(state.bucket)
            yield
            return
        settings.cache_db.parent.mkdir(parents=True, exist_ok=True)
        cache = WeatherCache(settings.cache_db, shared=True)
        bucket = Bucket(settings.s3) if settings.s3 else None
        catalog.configure(bucket)  # les tuiles publiées décident des espèces servies
        if bucket is None:
            log.warning("pas de bucket (CHAMPIS_S3_*) : /tiles vide, glisser-déposer seul")
        tile_cache = TileCache.from_settings(settings) if bucket is not None else None
        if settings.tiles_mode == "proxy":
            if tile_cache is None:
                log.info("relais des tuiles sans cache disque (CHAMPIS_TILE_CACHE_MB=0)")
            else:
                log.info(
                    "relais des tuiles : cache disque %s, %d Mo",
                    tile_cache.root,
                    settings.tile_cache_mb,
                )
        try:
            # un worker importe, les autres attendent puis constatent que c'est fait ; dans un
            # fil : la migration de `cell_field` peut prendre une minute sur la France
            await asyncio.to_thread(
                _startup_imports, cache, bucket, settings.weather_seed, settings.cache_db.parent
            )
        except Exception:  # un bucket injoignable ne doit pas empêcher l'API de démarrer
            log.exception("amorçage : import impossible, on continue avec le cache existant")
        store = Store(settings.database_url) if settings.database_url else None
        if store is None:
            log.warning("pas de base (CHAMPIS_DATABASE_URL) : /observations et /coins en 503")
        else:
            log.info("observations et coins : %s", store.dialect)
        async with (
            httpx.AsyncClient(headers={"User-Agent": "champis-api"}) as http,
            httpx.AsyncClient(http2=True, timeout=10.0) as http2,
        ):
            grid = WeatherGrid.load(settings.weather_grid)
            key = settings.open_meteo_api_key
            apns = None
            if settings.apns_configured:
                apns = ApnsClient(
                    http2,
                    settings.apns_key_pem,
                    settings.apns_key_id,
                    settings.apns_team_id,
                    settings.apns_topic,
                )
            else:
                log.warning(
                    "APNs non configuré (CHAMPIS_APNS_*) : pas de notification « mes coins »"
                )
            client = OpenMeteoClient(http, cache, api_key=key)
            app.state.champis = State(
                grid=grid,
                client=client,
                cache=cache,
                bucket=bucket,
                tiles_mode=settings.tiles_mode,
                tiles_url_hours=settings.tiles_url_hours,
                tile_cache=tile_cache,
                store=store,
                apns=apns,
            )
            warmer = OpenMeteoClient(http, cache, min_interval_s=WARM_INTERVAL_S, api_key=key)

            async def background() -> None:
                """Ce que seul le meneur fait."""
                tasks = [
                    cache_refresher(
                        settings.cache_db,
                        bucket,
                        settings.cache_db.parent,
                        settings.cache_poll_minutes,
                    ),
                    warm_scheduler(
                        grid, warmer, settings.warm_hours, settings.warm_on_start, store
                    ),
                ]
                if store is not None and apns is not None and settings.notify_hours:
                    tasks.append(notify_scheduler(store, grid, client, apns, settings.notify_hours))
                await asyncio.gather(*tasks)

            leader = asyncio.create_task(
                lead(settings.cache_db.parent / LEADER_LOCK, background, settings.leader_retry_s)
            )
            try:
                yield
            finally:
                leader.cancel()
                with suppress(asyncio.CancelledError):
                    await leader
        if store is not None:
            store.close()
        cache.close()

    # Pas de documentation interactive ni de schéma OpenAPI servis (décision du 13 nov. 2026) :
    # `/docs`, `/redoc` et `/openapi.json` répondent 404, en production comme en développement ;
    # le contrat des routes est décrit dans `api/README.md`.
    app = FastAPI(
        title="Champis API", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    # Le backoffice (`/admin`) lit la clé et l'heure de démarrage ici ; `settings` reste la seule
    # source de configuration, les routeurs ne relisent pas l'environnement.
    app.state.settings = settings
    app.state.started_at = datetime.now(UTC)
    # `/weather/bbox` sur une emprise large pèse plusieurs Mo en JSON très répétitif, dix fois
    # moins une fois compressé (décision du 2 nov. 2026).
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    # Le site vit sur champis.app, l'API sur api.champis.app (découpage du 6 nov. 2026) : les
    # requêtes Range du lecteur PMTiles du navigateur doivent passer et lire leurs en-têtes.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["X-Champis-User", "Content-Type", "Range", "If-None-Match"],
        expose_headers=["Content-Range", "Content-Length", "Accept-Ranges", "ETag"],
    )
    # Service public : seau à jetons par IP, écritures bien plus rares que lectures. Le relais de
    # tuiles et les fichiers du site en sont exemptés (étude du 4 nov. 2026) : un écran de carte
    # vaut 75 à 150 requêtes Range, le seau de 120 lectures laissait des tuiles blanches dès la
    # première vue ; le limiteur protège le CPU et Open-Meteo, pas la sortie du bucket.
    app.add_middleware(
        RateLimitMiddleware,
        read_per_minute=settings.rate_read_per_minute,
        write_per_minute=settings.rate_write_per_minute,
        max_body=settings.max_body_bytes,
        exempt_prefixes=RATE_LIMIT_EXEMPT,
    )

    site_origin = settings.site_url.rstrip("/")

    def _store():
        return getattr(getattr(app.state, "champis", None), "store", None)

    # appareils actifs (lot BO2) : par worker, les empreintes déjà écrites aujourd'hui
    tracker = counts.DeviceDayTracker()
    # cible d'une requête météo : groupe ou espèce, parmi les espèces servies aux clients (une
    # couche « girolles » de l'app demande les girolles du catalogue, pas celles sans planche ni
    # tuiles) ; le catalogue suit le manifeste du bucket, donc relu à chaque mesure (en cache)

    def _measure(store: Store, request, status: int) -> None:
        """Dans un fil, après la réponse : ce que la mesure minimale retient de la requête."""
        day = counts.today()
        groups = species_groups(catalog.served_species())
        user = counts.valid_user_id(request.headers.get("x-champis-user"))
        if user and settings.measure_secret:
            hashed = counts.device_hash(settings.measure_secret, user)
            if tracker.first_today(day, hashed):
                try:
                    store.mark_device_day(day, hashed)
                except Exception:
                    log.exception("mesure : appareil actif non enregistré")
        if request.method == "GET" and status == 200 and request.url.path in counts.LAYER_PATHS:
            client = (
                "app" if user else "web" if request.headers.get("origin") == site_origin else None
            )
            if client:
                for target in counts.layer_targets(request.query_params.getlist("species"), groups):
                    counts.bump_quietly(store, counts.layer_counter(client, target), day=day)

    @app.middleware("http")
    async def count_requests(request, call_next):
        """Mesure minimale (étape C, lot C2) : une ouverture de la carte du site de plus, rien
        d'autre ; seul l'en-tête Origin est regardé (est-ce le site ?), ni adresse IP ni
        identifiant ne sont lus ou gardés. Depuis le backoffice (lot BO1) : les réponses 429 et
        5xx du jour, en nombre seulement, pour la page Santé. Lot BO2 : l'appareil de l'app réduit
        à une empreinte par jour, et la cible (espèce ou groupe) de chaque requête météo."""
        try:
            response = await call_next(request)
        except Exception:
            store = _store()
            if store is not None:
                await asyncio.to_thread(counts.bump_quietly, store, "http_5xx")
            raise
        store = _store()
        if store is None:
            return response
        if (
            request.method == "GET"
            and request.url.path == MAP_OPEN_PATH
            and request.headers.get("origin") == site_origin
            and response.status_code == 200
        ):
            await asyncio.to_thread(counts.bump_quietly, store, "web_map_opens")
        elif response.status_code == 429:
            await asyncio.to_thread(counts.bump_quietly, store, "http_429")
        elif response.status_code >= 500:
            await asyncio.to_thread(counts.bump_quietly, store, "http_5xx")
        if request.headers.get("x-champis-user") or request.url.path in counts.LAYER_PATHS:
            await asyncio.to_thread(_measure, store, request, response.status_code)
        return response

    app.include_router(species.router)
    app.include_router(weather.router)
    app.include_router(tiles.router)
    app.include_router(departements.router)
    app.include_router(observations.router)
    app.include_router(coins.router)
    app.include_router(admin_router)  # 404 tant que CHAMPIS_ADMIN_KEY est vide
    admin_pages.install(app)  # les pages du backoffice (lot BO6), mêmes conditions

    @app.get("/health")
    async def health() -> dict[str, str | int | None]:
        st = app.state.champis
        meta = st.cache.dept_index_meta()
        return {
            "status": "ok",
            "cells": len(st.grid),
            "seeded": st.cache.seeded_cells(),
            "dept_index_at": meta[0] if meta else None,
            # `published` du dernier instantané importé du bucket (cron) ; None = jamais
            "cache_published": st.cache.meta_get(META_CACHE_PUBLISHED),
        }

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        # en production, le site est ailleurs (champis-web) ; en développement, servi ici
        return RedirectResponse(
            "/web/" if settings.serve_web and settings.web_dir else settings.site_url
        )

    if settings.serve_web:
        # Développement : la page lit ../shared/palette.json et ../species/<id>.json, mêmes chemins
        # que le service statique de production.
        for name in ("shared", "species"):
            app.mount(f"/{name}", StaticFiles(directory=settings.referential / name, html=True))
        if settings.web_dir:
            app.mount("/web", StaticFiles(directory=settings.web_dir / "web", html=True))

    return app


def next_warm(now: datetime, hours: tuple[int, ...]) -> datetime:
    """Prochaine occurrence (heure locale Paris) parmi `hours`, strictement après `now`."""
    local = now.astimezone(PARIS)
    candidates = []
    for d in (0, 1):
        day = (local + timedelta(days=d)).date()
        for h in hours:
            t = datetime(day.year, day.month, day.day, h, tzinfo=PARIS)
            if t > local:
                candidates.append(t)
    return min(candidates)


async def lead(lock_path: Path, background, retry_s: float) -> None:
    """Un seul worker tient les tâches de fond : celui qui obtient le verrou. Les autres
    réessaient toutes les `retry_s` secondes, pour prendre le relais si le meneur meurt."""
    handle = None
    try:
        while (handle := try_lock(lock_path)) is None:
            await asyncio.sleep(retry_s)
        log.info("worker meneur (pid %d) : tâches de fond lancées", os.getpid())
        await background()
        # plus rien à faire (pas de planificateur, import au démarrage seulement) : on garde
        # le verrou jusqu'à l'arrêt, un seul meneur par volume reste la règle
        await asyncio.Event().wait()
    finally:
        if handle is not None:
            handle.close()


def _startup_imports(
    cache: WeatherCache, bucket: BucketLike | None, seed: Path, work_dir: Path
) -> None:
    """Sous le verrou d'amorçage : l'amorçage publié s'il est plus récent, puis la migration
    d'une base d'avant `cell_field` (dérivée de `cell_weather`, idempotente ; spec › Points
    ouverts › Stockage compact par jour pour /weather/field, 5 nov. 2026)."""
    with file_lock(work_dir / SEED_LOCK):
        sync_seed(cache, bucket, seed, work_dir)
        cache.ensure_cell_field()


def _refresh_cache(cache_db: Path, bucket: BucketLike, work_dir: Path):
    """Sur sa propre connexion : la transaction d'import ne se mêle pas aux lectures des
    requêtes en cours, qui voient l'ancien état puis le nouveau (WAL)."""
    cache = WeatherCache(cache_db, shared=True)
    try:
        return sync_cache(cache, bucket, work_dir)
    finally:
        cache.close()


async def cache_refresher(
    cache_db: Path, bucket: BucketLike | None, work_dir: Path, poll_minutes: int
) -> None:
    """Importe le cache publié par le service cron : au démarrage, puis toutes les
    `poll_minutes` minutes (0 = au démarrage seulement). L'import tourne hors de la boucle
    d'événements ; une erreur (bucket injoignable) est journalisée et réessayée au tour suivant."""
    if bucket is None:
        return
    while True:
        try:
            meta = await asyncio.to_thread(_refresh_cache, cache_db, bucket, work_dir)
            if meta is not None:
                log.info("cache publié importé (%s)", meta["published"])
        except Exception:
            log.exception("import du cache publié en échec")
        if poll_minutes <= 0:
            return
        await asyncio.sleep(poll_minutes * 60)


async def warm_scheduler(
    grid: WeatherGrid,
    client: OpenMeteoClient,
    hours: tuple[int, ...],
    on_start: bool,
    store: Store | None = None,
) -> None:
    if on_start:
        await _warm_once(grid, client, store)
    if not hours:
        return
    while True:
        at = next_warm(datetime.now(PARIS), hours)
        log.info("prochaine pré-chauffe à %s", at.isoformat(timespec="minutes"))
        await asyncio.sleep((at - datetime.now(PARIS)).total_seconds())
        await _warm_once(grid, client, store)


async def notify_scheduler(
    store: Store,
    grid: WeatherGrid,
    client: OpenMeteoClient,
    apns: ApnsClient,
    hours: tuple[int, ...],
) -> None:
    """Notification « mes coins » aux heures locales données (après la pré-chauffe du matin)."""
    while True:
        at = next_warm(datetime.now(PARIS), hours)
        log.info("prochaine notification des coins à %s", at.isoformat(timespec="minutes"))
        await asyncio.sleep((at - datetime.now(PARIS)).total_seconds())
        try:
            await notify_all(store, grid, client, apns)
        except Exception:
            log.exception("notification des coins en échec")


async def _warm_once(
    grid: WeatherGrid, client: OpenMeteoClient, store: Store | None = None
) -> None:
    try:
        report = await warm(grid, client)
        if store is not None:
            await asyncio.to_thread(
                counts.record_warm, store, report.cells, len(report.unavailable)
            )
        log.info(
            "pré-chauffe terminée : %d cellules, %d indisponibles, %d périmées",
            report.cells,
            len(report.unavailable),
            report.stale,
        )
    except Exception:
        log.exception("pré-chauffe en échec")
        return
    try:  # vue France : l'indice par département se recalcule sur le cache tout juste rempli
        await compute_dept_index(grid, client, client.cache, catalog.served_species())
    except Exception:
        log.exception("indice par département en échec")


app = create_app()
