"""Service champis-api : `/species`, `/weather`, `/weather/bbox`, `/health`, et la carte web
statique sous `/web`.

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
from champis_pipeline.storage import Bucket, BucketLike
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .cache import WeatherCache
from .grid import WeatherGrid
from .routers import species, tiles, weather
from .seed import sync_seed
from .settings import Settings
from .warm import WARM_INTERVAL_S, warm
from .weather_client import OpenMeteoClient

log = logging.getLogger(__name__)
PARIS = ZoneInfo("Europe/Paris")


@dataclass
class State:
    grid: WeatherGrid
    client: OpenMeteoClient
    cache: WeatherCache
    bucket: BucketLike | None = None  # PMTiles (spec › hébergement) ; None = glisser-déposer seul
    tiles_mode: str = "presign"
    tiles_url_hours: int = 24


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
        async with httpx.AsyncClient(headers={"User-Agent": "champis-api"}) as http:
            grid = WeatherGrid.load(settings.weather_grid)
            key = settings.open_meteo_api_key
            app.state.champis = State(
                grid=grid,
                client=OpenMeteoClient(http, cache, api_key=key),
                cache=cache,
                bucket=bucket,
                tiles_mode=settings.tiles_mode,
                tiles_url_hours=settings.tiles_url_hours,
            )
            warmer = OpenMeteoClient(http, cache, min_interval_s=WARM_INTERVAL_S, api_key=key)
            task = asyncio.create_task(
                warm_scheduler(grid, warmer, settings.warm_hours, settings.warm_on_start)
            )
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        cache.close()

    app = FastAPI(title="Champis API", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["GET"]
    )
    app.include_router(species.router)
    app.include_router(weather.router)
    app.include_router(tiles.router)

    @app.get("/health")
    async def health() -> dict[str, str | int]:
        st = app.state.champis
        return {"status": "ok", "cells": len(st.grid), "seeded": st.cache.seeded_cells()}

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


app = create_app()
