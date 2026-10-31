"""Étape « publish » : PMTiles du dossier data/tiles → bucket, puis manifeste `tiles/manifest.json`.

Idempotent : un fichier déjà présent dans le bucket avec la même taille n'est pas renvoyé. Le
manifeste décrit tout ce qui est dans le bucket (plusieurs publications s'additionnent) et garde
la liste des départements derrière chaque hash `d<…>`. Avec `per_dept`, un jeu par département
accompagne le jeu complet (hors ligne de l'app, décision du 31 oct. 2026) ; le manifeste marque
ces entrées d'un champ `dept`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import TILES_DIR
from .progress import tqdm
from .species import species_hash
from .storage import MANIFEST_KEY, TILES_PREFIX, BucketLike, build_manifest, parse_tile_name
from .tiles import ALT_CLASS_M, _depts_hash

log = logging.getLogger(__name__)


def expected_names(depts: list[str], species: list[str]) -> list[str]:
    from . import SCORING_VERSION

    dh = _depts_hash(depts)
    names = [f"T-{sid}-{species_hash(sid)}-p{SCORING_VERSION}-d{dh}.pmtiles" for sid in species]
    return [*names, f"A-{ALT_CLASS_M}m-d{dh}.pmtiles"]


def _depts_hash_of(key: str) -> str | None:
    info = parse_tile_name(key.removeprefix(TILES_PREFIX))
    return info["depts_hash"] if info else None


def dept_sets(depts: list[str], per_dept: bool) -> list[list[str]]:
    """Jeux à publier : le jeu complet, puis un jeu par département si `per_dept` (et s'il y en a
    plusieurs : un jeu d'un seul département est déjà son propre jeu)."""
    sets = [sorted(depts)]
    if per_dept and len(depts) > 1:
        sets += [[d] for d in sorted(depts)]
    return sets


def publish(
    bucket: BucketLike,
    depts: list[str],
    species: list[str],
    tiles_dir: Path = TILES_DIR,
    prune: bool = False,
    per_dept: bool = False,
) -> dict[str, Any]:
    """Téléverse les PMTiles attendus pour (depts, species), met à jour le manifeste, le renvoie.

    `prune` supprime du bucket les PMTiles des autres jeux de départements (hash `d…` différent)
    et les retire de `depts_by_hash` : il ne reste que les jeux publiés par cet appel.
    `per_dept` publie aussi un jeu par département (`tiles --per-dept` les a produits).
    """
    sets = dept_sets(depts, per_dept)
    names = list(dict.fromkeys(n for s in sets for n in expected_names(s, species)))
    missing = [n for n in names if not (tiles_dir / n).exists()]
    if missing:
        raise FileNotFoundError(f"PMTiles absents de {tiles_dir} : {missing} (lancer `tiles`)")
    present = bucket.list(TILES_PREFIX)
    todo = [n for n in names if present.get(TILES_PREFIX + n) != (tiles_dir / n).stat().st_size]
    for name in names:
        if name not in todo:
            log.info("déjà dans le bucket : %s", name)
    total = sum((tiles_dir / n).stat().st_size for n in todo)
    log.info("à téléverser : %d fichier(s), %.1f Mo", len(todo), total / 1e6)
    with tqdm(total=total, desc="publish", unit="B", unit_scale=True, unit_divisor=1024) as bar:
        for i, name in enumerate(todo, 1):
            path = tiles_dir / name
            bar.set_postfix_str(f"{i}/{len(todo)} {name}", refresh=True)
            bucket.upload(path, TILES_PREFIX + name, progress=bar.update)
    old = bucket.get_json(MANIFEST_KEY) or {}
    depts_by_hash = dict(old.get("depts_by_hash", {}))
    keep = {_depts_hash(s): s for s in sets}
    depts_by_hash.update(keep)
    if prune:
        stale = [k for k in bucket.list(TILES_PREFIX) if _depts_hash_of(k) not in (None, *keep)]
        if stale:
            log.info("suppression de %d fichier(s) d'autres jeux : %s", len(stale), stale)
            bucket.delete(stale)
        depts_by_hash = keep
    manifest = build_manifest(
        bucket.list(TILES_PREFIX), depts_by_hash, datetime.now(UTC).isoformat()
    )
    bucket.put_json(MANIFEST_KEY, manifest)
    log.info("manifeste : %d fichiers", len(manifest["tiles"]))
    return manifest
