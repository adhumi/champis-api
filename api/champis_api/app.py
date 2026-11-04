"""Service champis-api : `/species`, `/weather`, `/weather/bbox`, `/departements`, `/observations`,
`/coins`, `/health`, et la carte web statique sous `/web`.

Sans état hors le cache météo SQLite ; aucun calcul géographique (tout est dans les tuiles).
Pré-chauffe intégrée (`CHAMPIS_WARM_HOURS`) : une tâche de fond lance `warm` aux heures voulues,
ce qui évite un second service Railway et partage le volume du cache.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
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
from .ratelimit import RateLimitMiddleware
from .routers import coins, departements, observations, species, tiles, weather
from .seed import sync_seed
from .settings import Settings
from .store import Store
from .warm import WARM_INTERVAL_S, warm
from .weather_client import OpenMeteoClient

log = logging.getLogger(__name__)
PARIS = ZoneInfo("Europe/Paris")
# Chemins hors limitation de débit : relais des PMTiles (`/tiles/<nom>`, pas le manifeste `/tiles`)
# et fichiers statiques du site. Lectures nombreuses, sans calcul ni appel externe.
RATE_LIMIT_EXEMPT = ("/tiles/", "/web/", "/shared/", "/species/")


@dataclass
class State:
    grid: WeatherGrid
    client: OpenMeteoClient
    cache: WeatherCache
    bucket: BucketLike | None = None  # PMTiles (spec › hébergement) ; None = glisser-déposer seul
    tiles_mode: str = "presign"
    tiles_url_hours: int = 24
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
        cache = WeatherCache(settings.cache_db)
        bucket = Bucket(settings.s3) if settings.s3 else None
        if bucket is None:
            log.warning("pas de bucket (CHAMPIS_S3_*) : /tiles vide, glisser-déposer seul")
        try:
            sync_seed(cache, bucket, settings.weather_seed, settings.cache_db.parent)
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
                store=store,
                apns=apns,
            )
            warmer = OpenMeteoClient(http, cache, min_interval_s=WARM_INTERVAL_S, api_key=key)
            tasks = [
                asyncio.create_task(
                    warm_scheduler(grid, warmer, settings.warm_hours, settings.warm_on_start)
                )
            ]
            if store is not None and apns is not None and settings.notify_hours:
                tasks.append(
                    asyncio.create_task(
                        notify_scheduler(store, grid, client, apns, settings.notify_hours)
                    )
                )
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
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
