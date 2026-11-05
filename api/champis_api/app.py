"""Service champis-api : `/species`, `/weather`, `/weather/bbox`, `/weather/field`, `/departements`,
`/observations`, `/coins`, `/health`, et la carte web statique sous `/web`.

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
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from champis_pipeline.species import list_species
from champis_pipeline.storage import Bucket, BucketLike
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

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
            yield
            return
        settings.cache_db.parent.mkdir(parents=True, exist_ok=True)
        cache = WeatherCache(settings.cache_db, shared=True)
        bucket = Bucket(settings.s3) if settings.s3 else None
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
                    warm_scheduler(grid, warmer, settings.warm_hours, settings.warm_on_start),
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

    app = FastAPI(title="Champis API", lifespan=lifespan)
    # `/weather/bbox` sur une emprise large pèse plusieurs Mo en JSON très répétitif, dix fois
    # moins une fois compressé (décision du 2 nov. 2026).
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_origins),
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["X-Champis-User", "Content-Type"],
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
    app.include_router(species.router)
    app.include_router(weather.router)
    app.include_router(tiles.router)
    app.include_router(departements.router)
    app.include_router(observations.router)
    app.include_router(coins.router)

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

    if settings.serve_web:

        @app.get("/", include_in_schema=False)
        async def root() -> RedirectResponse:
            return RedirectResponse("/web/")

        # La page lit ../shared/palette.json et ../species/<id>.json : mêmes chemins qu'en local.
        for name in ("web", "shared", "species"):
            app.mount(f"/{name}", StaticFiles(directory=settings.repo_root / name, html=True))

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
    grid: WeatherGrid, client: OpenMeteoClient, hours: tuple[int, ...], on_start: bool
) -> None:
    if on_start:
        await _warm_once(grid, client)
    if not hours:
        return
    while True:
        at = next_warm(datetime.now(PARIS), hours)
        log.info("prochaine pré-chauffe à %s", at.isoformat(timespec="minutes"))
        await asyncio.sleep((at - datetime.now(PARIS)).total_seconds())
        await _warm_once(grid, client)


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


async def _warm_once(grid: WeatherGrid, client: OpenMeteoClient) -> None:
    try:
        report = await warm(grid, client)
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
        await compute_dept_index(grid, client, client.cache, list_species())
    except Exception:
        log.exception("indice par département en échec")


app = create_app()
