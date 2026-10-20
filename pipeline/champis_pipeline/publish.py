"""Étape « publish » : PMTiles du dossier data/tiles → bucket, puis manifeste `tiles/manifest.json`.

Idempotent : un fichier déjà présent dans le bucket avec la même taille n'est pas renvoyé. Le
manifeste décrit tout ce qui est dans le bucket (plusieurs publications s'additionnent) et garde
la liste des départements derrière chaque hash `d<…>`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import TILES_DIR
from .species import species_hash
from .storage import MANIFEST_KEY, TILES_PREFIX, BucketLike, build_manifest
from .tiles import ALT_CLASS_M, _depts_hash

log = logging.getLogger(__name__)


def expected_names(depts: list[str], species: list[str]) -> list[str]:
    from . import SCORING_VERSION

    dh = _depts_hash(depts)
    names = [f"T-{sid}-{species_hash(sid)}-p{SCORING_VERSION}-d{dh}.pmtiles" for sid in species]
    return [*names, f"A-{ALT_CLASS_M}m-d{dh}.pmtiles"]


def publish(
    bucket: BucketLike, depts: list[str], species: list[str], tiles_dir: Path = TILES_DIR
) -> dict[str, Any]:
    """Téléverse les PMTiles attendus pour (depts, species), met à jour le manifeste, le renvoie."""
    names = expected_names(depts, species)
    missing = [n for n in names if not (tiles_dir / n).exists()]
    if missing:
        raise FileNotFoundError(f"PMTiles absents de {tiles_dir} : {missing} (lancer `tiles`)")
    present = bucket.list(TILES_PREFIX)
    for name in names:
        path, key = tiles_dir / name, TILES_PREFIX + name
        if present.get(key) == path.stat().st_size:
            log.info("déjà dans le bucket : %s", name)
            continue
        log.info("téléversement %s (%.1f Mo)", name, path.stat().st_size / 1e6)
        bucket.upload(path, key)
    old = bucket.get_json(MANIFEST_KEY) or {}
    depts_by_hash = dict(old.get("depts_by_hash", {}))
    depts_by_hash[_depts_hash(depts)] = sorted(depts)
    manifest = build_manifest(
        bucket.list(TILES_PREFIX), depts_by_hash, datetime.now(UTC).isoformat()
    )
    bucket.put_json(MANIFEST_KEY, manifest)
    log.info("manifeste : %d fichiers", len(manifest["tiles"]))
    return manifest
