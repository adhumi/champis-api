"""Amorçage : historique J−200 → J−1 et climatologie du sol, dans une base SQLite.

À lancer **hors Railway** (`champis-api seed`), une fois par grille. Décision du 25 oct. 2026
(option a) : la climatologie (10 ans par cellule, 26 des 27 appels pondérés) vient du pipeline
(`champis-pipeline soil-clim`, ERA5-Land via le Climate Data Store, gratuit) ; `seed` ne demande
plus à l'archive Open-Meteo gratuite que l'historique (≈ 1 appel pondéré par cellule, lots de 50 :
la France en 2 à 3 jours à 10 000 appels par jour, reprise automatique), et la climatologie
seulement pour les cellules qui n'en ont pas encore (`--climatology`). Le résultat est une base
`weather_seed.db` au schéma du cache (`history` source `seed`, `soil_clim`, `meta`), écrite par
lots : à l'échelle France (~22 000 cellules, ~400 Mo) un fichier JSON réécrit à chaque cellule ne
tenait plus.

Publication : `champis-api publish-seed` dépose la base et sa fiche
(`weather/weather_seed.json` : generated, cells, bytes) dans le bucket. Au démarrage, l'API compare
la date `generated` à celle enregistrée dans sa table `meta` ; si elle diffère, elle télécharge la
base sur le volume et l'importe par ATTACH (`INSERT OR IGNORE` pour l'historique : jamais par-dessus
la prévision ; `INSERT OR REPLACE` pour la climatologie), sans rien charger en mémoire. Un fichier
local (`CHAMPIS_WEATHER_SEED`, `.db` ou l'ancien `.json`) reste le repli de développement.

Reprise : une cellule qui a déjà un historique (resp. une climatologie) dans la base n'est pas
refaite.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from champis_pipeline.storage import BucketLike

from .cache import WeatherCache
from .grid import Cell, WeatherGrid
from .weather_client import (
    BATCH_ARCHIVE,
    BATCH_CLIMATOLOGY,
    OpenMeteoClient,
    _redact,
    _retry_after,
)

log = logging.getLogger(__name__)

SEED_VERSION = 2  # 1 = JSON (une entrée par cellule), 2 = SQLite au schéma du cache
BACKOFF_MIN_S = 60  # premier délai sur un 429 sans Retry-After exploitable
BACKOFF_MAX_S = 900  # les quotas se comptent à l'heure : attendre, pas abandonner
SEED_DB_KEY = "weather/weather_seed.db"
SEED_META_KEY = "weather/weather_seed.json"
META_GENERATED = "seed_generated"  # clé `meta` : date de l'amorçage importé
# clé `meta` : horodatage de la publication importée. `generated` n'est qu'un jour : deux
# publications le même jour (climatologie puis historique complet) se confondaient, et l'API
# ignorait la seconde.
META_PUBLISHED = "seed_published"


# ---------------------------------------------------------------------- amorçage (poste)


async def seed(
    grid: WeatherGrid,
    client: OpenMeteoClient,
    out: Path,
    today: date | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    history: bool = True,
    climatology: bool = False,
    progress: Callable[[str, int, int, float], None] | None = None,
) -> dict[str, Any]:
    """Complète la base `out` : historique des cellules qui n'en ont pas, et, si `climatology`,
    climatologie de celles qui n'en ont pas ; écrit par lot.

    `progress(quoi, faites, total, appels_pondérés)` après chaque lot ; défaut : une ligne de journal.

    Un 429 (quota) ou une coupure réseau ne fait pas échouer l'amorçage : on attend
    (Retry-After, sinon un délai qui double jusqu'à 15 min) et on reprend le même lot.
    Renvoie la fiche {version, generated, cells, history_cells, bytes, grid_cells}.
    """
    today = today or client.today()
    progress = progress or _progress
    out.parent.mkdir(parents=True, exist_ok=True)
    db = WeatherCache(out)
    try:
        if history:
            done = db.history_ids()
            todo = [c for c in grid.cells.values() if c.id not in done]
            log.info("historique : %d cellules à faire, %d déjà présentes", len(todo), len(done))
            for i in range(0, len(todo), BATCH_ARCHIVE):
                batch = todo[i : i + BATCH_ARCHIVE]
                rows = await _patiently(client.fetch_archive_batch, batch, today, sleep)
                for cell, r in zip(batch, rows, strict=True):
                    db.put_history(cell.id, r, "seed")
                db.meta_set(META_GENERATED, today.isoformat())
                progress("historique", i + len(batch), len(todo), client.weight)
        if climatology:
            done = db.seeded_ids()
            todo = [c for c in grid.cells.values() if c.id not in done]
            log.info("climatologie : %d cellules à faire, %d déjà présentes", len(todo), len(done))
            for i in range(0, len(todo), BATCH_CLIMATOLOGY):
                batch = todo[i : i + BATCH_CLIMATOLOGY]
                clims = await _patiently(client.fetch_climatology_batch, batch, today, sleep)
                for cell, clim in zip(batch, clims, strict=True):
                    db.put_soil_clim(cell.id, clim["months"])
                db.meta_set(META_GENERATED, today.isoformat())
                progress("climatologie", i + len(batch), len(todo), client.weight)
        return seed_info(db, out, len(grid.cells))
    finally:
        db.close()


def _progress(what: str, done: int, total: int, weight: float) -> None:
    log.info("%s %d/%d (≈ %.0f appels pondérés depuis le lancement)", what, done, total, weight)


def seed_info(db: WeatherCache, path: Path, grid_cells: int | None = None) -> dict[str, Any]:
    info = {
        "version": SEED_VERSION,
        "generated": db.meta_get(META_GENERATED),
        "cells": db.seeded_cells(),  # climatologie : ce qui rend une cellule servable
        "history_cells": len(db.history_ids()),
        "bytes": path.stat().st_size if path.exists() else 0,
    }
    if grid_cells is not None:
        info["grid_cells"] = grid_cells
    return info


async def _patiently(
    fetch, batch: list[Cell], today: date, sleep: Callable[[float], Awaitable[None]]
):
    backoff = BACKOFF_MIN_S
    while True:
        try:
            return await fetch(batch, today)
        except (httpx.HTTPStatusError, httpx.TransportError) as e:
            ra = _retry_after(e)
            if ra is None and not isinstance(e, httpx.TransportError):
                raise  # 4xx/5xx autre qu'un quota : ce n'est pas en attendant que ça passera
            wait = max(ra or 0, backoff)
            log.warning("%s… : %s — reprise dans %d s", batch[0].id, _redact(e), wait)
            await sleep(wait)
            backoff = min(backoff * 2, BACKOFF_MAX_S)


# ---------------------------------------------------------------------- publication (bucket)


def publish_seed(
    bucket: BucketLike, path: Path, progress=None, now: datetime | None = None
) -> dict[str, Any]:
    """Dépose la base d'amorçage et sa fiche dans le bucket ; renvoie la fiche. La fiche porte
    `published`, horodatage propre à cette publication, que l'API compare au démarrage."""
    db = WeatherCache(path)
    try:
        info = seed_info(db, path)
    finally:
        db.close()
    if not info["generated"] or not info["cells"]:
        raise ValueError(
            f"{path} : aucune climatologie dans la base (champis-pipeline soil-clim, ou "
            "champis-api seed --climatology)"
        )
    info["published"] = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
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
        # une fiche récente se reconnaît à `published` ; une ancienne, à sa seule date
        published = meta.get("published")
        seen = cache.meta_get(META_PUBLISHED) if published else current
        if (published or meta.get("generated")) == seen:
            log.info("amorçage du bucket déjà importé (%s)", seen)
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
        if published:
            cache.meta_set(META_PUBLISHED, published)
        log.info("amorçage importé depuis le bucket : %d cellules (%s)", n, meta.get("generated"))
        return n
    if local.exists() and current is None:
        n = import_seed(cache, local)
        log.info("amorçage importé : %d cellules (%s)", n, local)
        return n
    if current is None:
        log.warning("pas d'amorçage (%s, bucket) : les cellules seront indisponibles", local)
    return 0
