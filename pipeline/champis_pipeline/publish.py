"""Étape « publish » : PMTiles du dossier data/tiles → bucket, puis manifeste `tiles/manifest.json`.

Idempotent : un fichier déjà présent dans le bucket avec la même taille n'est pas renvoyé. Le
manifeste décrit tout ce qui est dans le bucket (plusieurs publications s'additionnent) et garde
la liste des départements derrière chaque hash `d<…>`. Avec `per_dept`, un jeu par département
accompagne le jeu complet (hors ligne de l'app, décision du 31 oct. 2026) ; le manifeste marque
ces entrées d'un champ `dept`. Avec `groups`, les PMTiles G (un par groupe d'affichage, spec ›
Points ouverts › PMTiles par groupe, 4 nov. 2026) sont attendus et publiés comme les T ; leurs
entrées portent `group` et `species` (ordre des canaux).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config
from .progress import tqdm
from .species import group_hash, group_sets, species_groups
from .storage import (
    MANIFEST_KEY,
    TILES_PREFIX,
    BucketLike,
    build_manifest,
    group_key,
    parse_tile_name,
)
from .tiles import ALT_CLASS_M, _depts_hash, group_tag, t_tag

log = logging.getLogger(__name__)


def complete_group_sets(species: list[str]) -> list[tuple[str, int, list[str]]]:
    """Fichiers G des groupes dont toutes les espèces sont dans `species` : un G porte toujours
    son groupe entier (ordre des canaux = ordre alphabétique de tout le groupe). Un groupe dont
    seule une partie est sélectionnée n'a pas de G (`partial_groups` les nomme)."""
    selected = set(species)
    return [s for s in group_sets() if set(s[2]) <= selected]


def partial_groups(species: list[str]) -> list[str]:
    """Groupes dont `species` ne couvre qu'une partie des espèces : pas de G pour eux."""
    selected = set(species)
    return [g for g, ids in species_groups().items() if 0 < len(selected & set(ids)) < len(ids)]


def expected_names(depts: list[str], species: list[str], groups: bool = False) -> list[str]:
    """PMTiles du jeu `depts` : T par espèce, A, puis (avec `groups`) un G par groupe complet."""
    dh = _depts_hash(depts)
    names = [f"T-{t_tag(sid, depts)}.pmtiles" for sid in species]
    names.append(f"A-{ALT_CLASS_M}m-d{dh}.pmtiles")
    if groups:
        names += [
            f"G-{group_tag(gid, ids, depts, part)}.pmtiles"
            for gid, part, ids in complete_group_sets(species)
        ]
    return names


def known_group_species(old_manifest: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """`group_key` → espèces dans l'ordre des canaux : ce que les fichiers espèces donnent aujourd'hui,
    complété par les G déjà décrits dans l'ancien manifeste (un modèle d'espèce qui a changé)."""
    known: dict[str, list[str]] = {}
    for t in (old_manifest or {}).get("tiles", []):
        if t.get("kind") == "G" and t.get("species"):
            known[group_key(t["group"], t["hash"])] = list(t["species"])
    for gid, _part, ids in group_sets():
        known[group_key(gid, group_hash(ids))] = ids
    return known


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
    groups: bool = False,
) -> Plan:
    """Compare les PMTiles attendus au contenu du bucket, sans rien écrire."""
    tiles_dir = tiles_dir or config.TILES_DIR
    sets = dept_sets(depts, per_dept)
    names = list(dict.fromkeys(n for s in sets for n in expected_names(s, species, groups)))
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
    groups: bool = False,
) -> dict[str, Any]:
    """Téléverse les PMTiles attendus pour (depts, species), met à jour le manifeste, le renvoie.

    `prune` supprime du bucket les PMTiles des autres jeux de départements (hash `d…` différent)
    et les retire de `depts_by_hash` : il ne reste que les jeux publiés par cet appel ; les G du
    jeu courant, publiés ou non par cet appel, portent son hash et restent.
    `per_dept` publie aussi un jeu par département (`tiles --per-dept` les a produits).
    `groups` publie aussi les G des groupes complets (`tiles --groups` les a produits).
    `plan_` : un plan déjà calculé (la CLI l'affiche d'abord) ; sinon il est recalculé.
    """
    tiles_dir = tiles_dir or config.TILES_DIR
    p = plan_ or plan(bucket, depts, species, tiles_dir, prune, per_dept, groups)
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
        bucket.list(TILES_PREFIX),
        depts_by_hash,
        datetime.now(UTC).isoformat(),
        known_group_species(old),
    )
    bucket.put_json(MANIFEST_KEY, manifest)
    log.info("manifeste : %d fichiers", len(manifest["tiles"]))
    return manifest
