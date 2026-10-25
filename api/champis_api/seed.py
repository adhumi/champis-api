"""Amorçage : historique J−200 → J−1 et climatologie du sol depuis l'archive ERA5-Land.

À lancer **hors Railway** (`champis-api seed`), une fois par grille, avec la clé API (plan
Professional le temps de l'amorçage ; décision du 25 oct. 2026) ou sur l'archive gratuite pour un
petit secteur. Le résultat est une base SQLite `weather_seed.db` au schéma du cache (`history`
source `seed`, `soil_clim`, `meta`), écrite par lots : à l'échelle France (~22 000 cellules,
~400 Mo) un fichier JSON réécrit à chaque cellule ne tenait plus.

Publication : `champis-api publish-seed` dépose la base et sa fiche
(`weather/weather_seed.json` : generated, cells, bytes) dans le bucket. Au démarrage, l'API compare
la date `generated` à celle enregistrée dans sa table `meta` ; si elle diffère, elle télécharge la
base sur le volume et l'importe par ATTACH (`INSERT OR IGNORE` pour l'historique : jamais par-dessus
la prévision ; `INSERT OR REPLACE` pour la climatologie), sans rien charger en mémoire. Un fichier
local (`CHAMPIS_WEATHER_SEED`, `.db` ou l'ancien `.json`) reste le repli de développement.

Reprise : les cellules déjà pourvues d'une climatologie dans la base ne sont pas refaites.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

import httpx
from champis_pipeline.storage import BucketLike

from .cache import WeatherCache
from .grid import Cell, WeatherGrid
from .weather_client import OpenMeteoClient, _redact, _retry_after

log = logging.getLogger(__name__)

SEED_VERSION = 2  # 1 = JSON (une entrée par cellule), 2 = SQLite au schéma du cache
SEED_BATCH = 10  # cellules par requête : ≤ lot climatologie (la plus lourde : 10 ans par point)
BACKOFF_MIN_S = 60  # premier délai sur un 429 sans Retry-After exploitable
BACKOFF_MAX_S = 900  # les quotas se comptent à l'heure : attendre, pas abandonner
SEED_DB_KEY = "weather/weather_seed.db"
SEED_META_KEY = "weather/weather_seed.json"
META_GENERATED = "seed_generated"  # clé `meta` : date de l'amorçage importé


# ---------------------------------------------------------------------- amorçage (poste)


async def seed(
    grid: WeatherGrid,
    client: OpenMeteoClient,
    out: Path,
    today: date | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    """Complète la base `out` avec les cellules de la grille qui y manquent ; écrit par lot.

    Un 429 (quota) ou une coupure réseau ne fait pas échouer l'amorçage : on attend
    (Retry-After, sinon un délai qui double jusqu'à 15 min) et on reprend le même lot.
    Renvoie la fiche {version, generated, cells, grid_cells}.
    """
    today = today or client.today()
    out.parent.mkdir(parents=True, exist_ok=True)
    db = WeatherCache(out)
    try:
        done = db.seeded_ids()
        todo = [c for c in grid.cells.values() if c.id not in done]
        log.info("amorçage : %d cellules à faire, %d déjà présentes", len(todo), len(done))
        for i in range(0, len(todo), SEED_BATCH):
            batch = todo[i : i + SEED_BATCH]
            results = await _fetch_patiently(client, batch, today, sleep)
            for cell, (rows, clim) in zip(batch, results, strict=True):
                db.put_history(cell.id, rows, "seed")
                db.put_soil_clim(cell.id, clim["months"])
            db.meta_set(META_GENERATED, today.isoformat())
            log.info(
                "amorçage %d/%d (≈ %.0f appels pondérés)",
                min(i + SEED_BATCH, len(todo)),
                len(todo),
                client.weight,
            )
        return seed_info(db, out, len(grid.cells))
    finally:
        db.close()


def seed_info(db: WeatherCache, path: Path, grid_cells: int | None = None) -> dict[str, Any]:
    info = {
        "version": SEED_VERSION,
        "generated": db.meta_get(META_GENERATED),
        "cells": db.seeded_cells(),
        "bytes": path.stat().st_size if path.exists() else 0,
    }
    if grid_cells is not None:
        info["grid_cells"] = grid_cells
    return info


async def _fetch_patiently(
    client: OpenMeteoClient,
    batch: list[Cell],
    today: date,
    sleep: Callable[[float], Awaitable[None]],
) -> list[tuple[dict[str, dict], dict[str, Any]]]:
    backoff = BACKOFF_MIN_S
    while True:
        try:
            return await client.fetch_seed_batch(batch, today)
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            ra = _retry_after(e)
            if ra is None and not isinstance(e, httpx.TransportError):
                raise  # 4xx/5xx autre qu'un quota : ce n'est pas en attendant que ça passera
            wait = max(ra or 0, backoff)
            log.warning("%s… : %s — reprise dans %d s", batch[0].id, _redact(e), wait)
            await sleep(wait)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


# ---------------------------------------------------------------------- publication (bucket)


def publish_seed(bucket: BucketLike, path: Path, progress=None) -> dict[str, Any]:
    """Dépose la base d'amorçage et sa fiche dans le bucket ; renvoie la fiche."""
    db = WeatherCache(path)
    try:
        info = seed_info(db, path)
    finally:
        db.close()
    if not info["generated"] or not info["cells"]:
        raise ValueError(f"{path} : base d'amorçage vide (lancer `champis-api seed`)")
    bucket.upload(path, SEED_DB_KEY, progress)
    bucket.put_json(SEED_META_KEY, info)
    return info


# ---------------------------------------------------------------------- import (API)


def import_seed(cache: WeatherCache, path: Path) -> int:
    """Importe un amorçage local (`.db` par ATTACH, ou l'ancien `.json`). Renvoie les cellules."""
    if path.suffix == ".json":
        return _import_json(cache, path)
    return cache.import_seed_db(path)


def _import_json(cache: WeatherCache, path: Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise ValueError(f"{path} : version {data.get('version')!r} inconnue")
    for cid, c in data["cells"].items():
        cache.put_history(cid, c["history"], "seed")
        cache.put_soil_clim(cid, c["soil_clim"])
    if data.get("generated"):
        cache.meta_set(META_GENERATED, data["generated"])
    return len(data["cells"])


def sync_seed(cache: WeatherCache, bucket: BucketLike | None, local: Path, work_dir: Path) -> int:
    """Au démarrage : importe l'amorçage du bucket s'il est plus récent que celui en base, sinon
    le fichier local s'il n'a jamais été importé. Renvoie le nombre de cellules importées (0 si
    rien à faire)."""
    current = cache.meta_get(META_GENERATED)
    meta = bucket.get_json(SEED_META_KEY) if bucket is not None else None
    if meta is not None:
        if meta.get("generated") == current:
            log.info("amorçage du bucket déjà importé (%s)", current)
            return 0
        work_dir.mkdir(parents=True, exist_ok=True)
        tmp = work_dir / "weather_seed.db"
        log.info(
            "téléchargement de l'amorçage %s (%.0f Mo)", SEED_DB_KEY, meta.get("bytes", 0) / 1e6
        )
        bucket.download(SEED_DB_KEY, tmp)
        try:
            n = cache.import_seed_db(tmp)
        finally:
            tmp.unlink(missing_ok=True)
        log.info("amorçage importé depuis le bucket : %d cellules (%s)", n, meta.get("generated"))
        return n
    if local.exists() and current is None:
        n = import_seed(cache, local)
        log.info("amorçage importé : %d cellules (%s)", n, local)
        return n
    if current is None:
        log.warning("pas d'amorçage (%s, bucket) : les cellules seront indisponibles", local)
    return 0
