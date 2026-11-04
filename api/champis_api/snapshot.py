"""Relais du cache météo par le bucket, entre le service cron et l'API (spec › Points ouverts ›
Tuiles hors du processus Python, 4 nov. 2026).

La pré-chauffe sort de l'API dans un service cron Railway. Un volume Railway ne se monte, à notre
connaissance, que sur un seul service : le cron et l'API ne partagent donc pas de fichier. Le cron
travaille sur sa propre copie du cache et, à la fin de chaque passe, en dépose un instantané dans
le bucket (`weather/weather_cache.db`, `VACUUM INTO` : un seul fichier compact) avec une fiche
(`weather/weather_cache.json` : `published`, `today`, `dept_index_at`, `cells`, `cell_weather`,
`bytes`). L'API compare la fiche à sa clé `meta` `cache_published`, au démarrage puis toutes les
`CHAMPIS_CACHE_POLL_MINUTES`, et importe l'instantané en une transaction (`import_cache_db` :
remplacement table par table, les workers lisent l'ancien état puis le nouveau).

Le cron fait le chemin inverse avant sa passe (`warm --pull`) : il repart de l'instantané publié
(cache, historique accumulé, climatologie) puis importe un amorçage plus récent s'il y en a un, ce
qui le rend indépendant d'un volume. Même chose pour `notify --pull` à 7 h.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from champis_pipeline.storage import BucketLike

from .cache import WeatherCache

log = logging.getLogger(__name__)

CACHE_DB_KEY = "weather/weather_cache.db"
CACHE_META_KEY = "weather/weather_cache.json"
META_CACHE_PUBLISHED = "cache_published"  # clé `meta` : `published` de l'instantané importé
SNAPSHOT_VERSION = 1


def cache_info(cache: WeatherCache) -> dict[str, Any]:
    meta = cache.dept_index_meta()
    return {
        "version": SNAPSHOT_VERSION,
        "today": meta[1] if meta else None,
        "dept_index_at": meta[0] if meta else None,
        "cells": cache.seeded_cells(),
        "cell_weather": cache.cell_weather_count(),
    }


def publish_cache(
    bucket: BucketLike,
    cache: WeatherCache,
    work_dir: Path,
    progress: Callable[[int], None] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Dépose un instantané du cache et sa fiche dans le bucket ; renvoie la fiche. Le cache
    garde `cache_published` pour ne pas réimporter sa propre publication."""
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = work_dir / "weather_cache.publish.db"
    cache.snapshot(tmp)
    try:
        info = cache_info(cache)
        info["bytes"] = tmp.stat().st_size
        info["published"] = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        bucket.upload(tmp, CACHE_DB_KEY, progress)
        bucket.put_json(CACHE_META_KEY, info)
    finally:
        tmp.unlink(missing_ok=True)
    cache.meta_set(META_CACHE_PUBLISHED, info["published"])
    log.info(
        "cache publié : %s (%d cellules, %.0f Mo)",
        info["published"],
        info["cells"],
        info["bytes"] / 1e6,
    )
    return info


def published_cache(bucket: BucketLike | None) -> dict[str, Any] | None:
    """La fiche de l'instantané publié, None sans bucket ou sans publication."""
    return bucket.get_json(CACHE_META_KEY) if bucket is not None else None


def sync_cache(
    cache: WeatherCache, bucket: BucketLike | None, work_dir: Path
) -> dict[str, Any] | None:
    """Importe l'instantané du bucket s'il est plus récent que celui en base. Renvoie sa fiche,
    None s'il n'y avait rien à faire (pas de bucket, rien de publié, déjà importé)."""
    meta = published_cache(bucket)
    if meta is None or not meta.get("published"):
        return None
    if meta["published"] == cache.meta_get(META_CACHE_PUBLISHED):
        return None
    assert bucket is not None
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = work_dir / "weather_cache.download.db"
    log.info(
        "téléchargement du cache publié %s (%s, %.0f Mo)",
        CACHE_DB_KEY,
        meta["published"],
        meta.get("bytes", 0) / 1e6,
    )
    bucket.download(CACHE_DB_KEY, tmp)
    try:
        counts = cache.import_cache_db(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    cache.meta_set(META_CACHE_PUBLISHED, meta["published"])
    log.info(
        "cache importé depuis le bucket : %s, %d cellules, %d entrées de météo précalculée",
        meta["published"],
        counts.get("soil_clim", 0),
        counts.get("cell_weather", 0),
    )
    return meta
