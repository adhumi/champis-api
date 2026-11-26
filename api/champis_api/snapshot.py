"""Relais du cache météo par le bucket, entre le service cron et l'API (spec › Points ouverts ›
Tuiles hors du processus Python, 4 nov. 2026).

La pré-chauffe sort de l'API dans un service cron Railway. Un volume Railway ne se monte, à notre
connaissance, que sur un seul service : le cron et l'API ne partagent donc pas de fichier. Le cron
travaille sur sa propre copie du cache et, à la fin de chaque passe, en dépose un instantané dans
le bucket (`VACUUM INTO` : un seul fichier compact, puis gzip : `weather/weather_cache.db.gz`) avec
une fiche (`weather/weather_cache.json` : `published`, `today`, `dept_index_at`, `cells`,
`cell_weather`, `cell_field`, `bytes`, et depuis le 26 nov. 2026 `key`, `compression`,
`sent_bytes`). L'instantané est surtout du JSON en texte : compressé, il pèse plusieurs fois moins
à envoyer (l'envoi d'un service vers le bucket est une sortie réseau) et à retélécharger par l'API
et les deux crons. Une fiche sans `key` décrit un instantané d'avant, non compressé, sous
`weather/weather_cache.db` : il se lit encore. L'API compare la fiche à sa clé `meta`
`cache_published`, au démarrage puis toutes les `CHAMPIS_CACHE_POLL_MINUTES`, et importe
l'instantané en une transaction (`import_cache_db` : remplacement table par table, les workers
lisent l'ancien état puis le nouveau).

Le cron fait le chemin inverse avant sa passe (`warm --pull`) : il repart de l'instantané publié
(cache, historique accumulé, climatologie) puis importe un amorçage plus récent s'il y en a un, ce
qui le rend indépendant d'un volume. Même chose pour `notify --pull` à 7 h.
"""

from __future__ import annotations

import gzip
import logging
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from champis_pipeline.storage import BucketLike

from .cache import WeatherCache

log = logging.getLogger(__name__)

CACHE_DB_KEY = "weather/weather_cache.db.gz"
LEGACY_CACHE_DB_KEY = "weather/weather_cache.db"  # instantané non compressé d'avant le 26 nov. 2026
GZIP = "gzip"
# Niveau 1 : sur du JSON de nombres, l'essentiel du gain pour une fraction du temps des niveaux
# hauts (la passe du cron reste bornée par Open-Meteo, pas par la compression).
GZIP_LEVEL = 1
COPY_CHUNK = 4 * 1024 * 1024
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
        "cell_field": cache.cell_field_count(),
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
    packed = work_dir / "weather_cache.publish.db.gz"
    cache.snapshot(tmp)
    try:
        info = cache_info(cache)
        info["bytes"] = tmp.stat().st_size
        _gzip(tmp, packed)
        tmp.unlink(missing_ok=True)
        info["key"] = CACHE_DB_KEY
        info["compression"] = GZIP
        info["sent_bytes"] = packed.stat().st_size
        info["published"] = (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
        bucket.upload(packed, CACHE_DB_KEY, progress)
        bucket.put_json(CACHE_META_KEY, info)
    finally:
        tmp.unlink(missing_ok=True)
        packed.unlink(missing_ok=True)
    cache.meta_set(META_CACHE_PUBLISHED, info["published"])
    try:
        # la fiche ne le désigne plus : un lecteur d'avant échoue bruyamment au lieu d'importer
        # un état périmé sous la date du nouveau
        bucket.delete([LEGACY_CACHE_DB_KEY])
    except Exception:
        log.exception("ancien instantané non compressé non retiré du bucket")
    log.info(
        "cache publié : %s (%d cellules, %.0f Mo, %.0f Mo envoyés)",
        info["published"],
        info["cells"],
        info["bytes"] / 1e6,
        info["sent_bytes"] / 1e6,
    )
    return info


def _gzip(src: Path, dst: Path) -> None:
    with src.open("rb") as fin, gzip.open(dst, "wb", compresslevel=GZIP_LEVEL) as fout:
        shutil.copyfileobj(fin, fout, COPY_CHUNK)


def _gunzip(src: Path, dst: Path) -> None:
    with gzip.open(src, "rb") as fin, dst.open("wb") as fout:
        shutil.copyfileobj(fin, fout, COPY_CHUNK)


def snapshot_location(meta: dict[str, Any]) -> tuple[str, str | None] | None:
    """(clé, compression) de l'instantané que décrit la fiche ; None si la fiche annonce un
    format que cette version ne sait pas lire. La clé ne vient jamais de la fiche telle quelle :
    seules les deux clés connues sont lues."""
    key, compression = meta.get("key"), meta.get("compression")
    if key is None and compression is None:
        return LEGACY_CACHE_DB_KEY, None
    if key == CACHE_DB_KEY and compression == GZIP:
        return CACHE_DB_KEY, GZIP
    return None


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
    where = snapshot_location(meta)
    if where is None:
        log.error(
            "cache publié %s dans un format inconnu (%s, %s) : ignoré",
            meta["published"],
            meta.get("key"),
            meta.get("compression"),
        )
        return None
    key, compression = where
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = work_dir / "weather_cache.download.db"
    packed = work_dir / "weather_cache.download.db.gz"
    log.info(
        "téléchargement du cache publié %s (%s, %.0f Mo)",
        key,
        meta["published"],
        meta.get("sent_bytes", meta.get("bytes", 0)) / 1e6,
    )
    try:
        if compression == GZIP:
            bucket.download(key, packed)
            _gunzip(packed, tmp)
            packed.unlink(missing_ok=True)
        else:
            bucket.download(key, tmp)
        counts = cache.import_cache_db(tmp)
    finally:
        tmp.unlink(missing_ok=True)
        packed.unlink(missing_ok=True)
    cache.meta_set(META_CACHE_PUBLISHED, meta["published"])
    log.info(
        "cache importé depuis le bucket : %s, %d cellules, %d entrées de météo précalculée, "
        "%d lignes de champ compact",
        meta["published"],
        counts.get("soil_clim", 0),
        counts.get("cell_weather", 0),
        counts.get("cell_field", 0),
    )
    return meta
