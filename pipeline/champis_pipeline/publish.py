"""Étape « publish » : PMTiles du dossier data/tiles → bucket, puis manifeste `tiles/manifest.json`.

Idempotent : un fichier déjà présent dans le bucket avec la même taille n'est pas renvoyé. Le
manifeste décrit tout ce qui est dans le bucket (plusieurs publications s'additionnent) et garde
la liste des départements derrière chaque hash `d<…>`. Avec `per_dept`, un jeu par département
accompagne le jeu complet (hors ligne de l'app, décision du 31 oct. 2026) ; le manifeste marque
ces entrées d'un champ `dept`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config
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


@dataclass
class Plan:
    """Ce que `publish` ferait : à téléverser, déjà là, à supprimer (`prune`). `--dry-run` l'affiche."""

    sets: list[list[str]]
    names: list[str]  # tous les fichiers attendus, dans l'ordre
    todo: list[str]  # absents du bucket ou de taille différente
    already: list[str]
    stale: list[str]  # clés du bucket d'autres jeux, supprimées si `prune`
    sizes: dict[str, int]  # taille locale par nom

    @property
    def todo_bytes(self) -> int:
        return sum(self.sizes[n] for n in self.todo)


def plan(
    bucket: BucketLike,
    depts: list[str],
    species: list[str],
    tiles_dir: Path | None = None,
    prune: bool = False,
    per_dept: bool = False,
) -> Plan:
    """Compare les PMTiles attendus au contenu du bucket, sans rien écrire."""
    tiles_dir = tiles_dir or config.TILES_DIR
    sets = dept_sets(depts, per_dept)
    names = list(dict.fromkeys(n for s in sets for n in expected_names(s, species)))
    missing = [n for n in names if not (tiles_dir / n).exists()]
    if missing:
        raise FileNotFoundError(f"PMTiles absents de {tiles_dir} : {missing} (lancer `tiles`)")
    sizes = {n: (tiles_dir / n).stat().st_size for n in names}
    present = bucket.list(TILES_PREFIX)
    todo = [n for n in names if present.get(TILES_PREFIX + n) != sizes[n]]
    keep = {_depts_hash(s) for s in sets}
    stale = [k for k in present if _depts_hash_of(k) not in (None, *keep)] if prune else []
    return Plan(sets, names, todo, [n for n in names if n not in todo], stale, sizes)


def publish(
    bucket: BucketLike,
    depts: list[str],
    species: list[str],
    tiles_dir: Path | None = None,
    prune: bool = False,
    per_dept: bool = False,
    plan_: Plan | None = None,
) -> dict[str, Any]:
    """Téléverse les PMTiles attendus pour (depts, species), met à jour le manifeste, le renvoie.

    `prune` supprime du bucket les PMTiles des autres jeux de départements (hash `d…` différent)
    et les retire de `depts_by_hash` : il ne reste que les jeux publiés par cet appel.
    `per_dept` publie aussi un jeu par département (`tiles --per-dept` les a produits).
    `plan_` : un plan déjà calculé (la CLI l'affiche d'abord) ; sinon il est recalculé.
    """
    tiles_dir = tiles_dir or config.TILES_DIR
    p = plan_ or plan(bucket, depts, species, tiles_dir, prune, per_dept)
    for name in p.already:
        log.info("déjà dans le bucket : %s", name)
    log.info("à téléverser : %d fichier(s), %.1f Mo", len(p.todo), p.todo_bytes / 1e6)
    with tqdm(
        total=p.todo_bytes, desc="publish", unit="B", unit_scale=True, unit_divisor=1024
    ) as bar:
        for i, name in enumerate(p.todo, 1):
            path = tiles_dir / name
            bar.set_postfix_str(f"{i}/{len(p.todo)} {name}", refresh=True)
            bucket.upload(path, TILES_PREFIX + name, progress=bar.update)
    old = bucket.get_json(MANIFEST_KEY) or {}
    depts_by_hash = dict(old.get("depts_by_hash", {}))
    keep = {_depts_hash(s): s for s in p.sets}
    depts_by_hash.update(keep)
    if prune:
        if p.stale:
            log.info("suppression de %d fichier(s) d'autres jeux : %s", len(p.stale), p.stale)
            bucket.delete(p.stale)
        depts_by_hash = keep
    manifest = build_manifest(
        bucket.list(TILES_PREFIX), depts_by_hash, datetime.now(UTC).isoformat()
    )
    bucket.put_json(MANIFEST_KEY, manifest)
    log.info("manifeste : %d fichiers", len(manifest["tiles"]))
    return manifest
