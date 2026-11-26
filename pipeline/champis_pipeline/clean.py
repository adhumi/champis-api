"""`clean` : ce qui occupe le dossier des données, et ce qui n'y sert plus (26 nov. 2026).

Le volume du service d'exécution grossit à chaque passage du pipeline et rien n'en sortait :
`status` nommait les PMTiles d'autres jeux « à supprimer à la main », les T d'une ancienne
empreinte d'espèce restaient à côté des nouveaux. `plan()` les recense sans rien écrire,
`apply()` les efface. N'est périmé que ce qui ne peut plus être lu par aucune étape :

- un T dont le nom ne porte pas l'empreinte courante de son espèce (`species_hash`), ou dont
  l'espèce n'est plus dans le référentiel : `terrain` et `tiles` ne lisent que l'empreinte courante ;
- un PMTiles qui n'est attendu ni dans le jeu de tous les départements ni dans un jeu d'un seul
  département (espèces et groupes du référentiel courant) : `publish` ne l'enverra jamais ;
- les T de variantes (`T-variants`, tests d'entrée de `check --edge`), refaits à la demande.

Les entrées brutes (`raw`), les rasters de grille et tout ce qui est courant ne sont jamais
touchés : `batch --clean-raw` s'occupe des premières, les autres se recalculent en heures.
"""

from __future__ import annotations

import contextlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .species import list_species, species_hash


def tree_bytes(path: Path) -> int:
    """Octets d'un fichier ou de tout un dossier ; les liens ne sont pas suivis, une entrée qui
    disparaît pendant le parcours (une étape tourne) compte pour zéro."""
    try:
        if not path.is_dir() or path.is_symlink():
            return path.lstat().st_size
    except OSError:
        return 0
    total = 0
    stack = [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for e in entries:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def folder_sizes(root: Path | None = None) -> dict[str, int]:
    """Octets par entrée de premier niveau du dossier des données (`raw`, `grid`, `T`, `tiles`,
    journaux, bases…), les plus grosses d'abord."""
    root = root or config.DATA_DIR
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return {}
    sizes = {p.name: tree_bytes(p) for p in entries}
    return dict(sorted(sizes.items(), key=lambda kv: -kv[1]))


@dataclass
class Plan:
    """Ce que `clean` effacerait, par sorte : (chemin, octets)."""

    stale_t: list[tuple[Path, int]] = field(default_factory=list)
    other_tiles: list[tuple[Path, int]] = field(default_factory=list)
    variants: list[tuple[Path, int]] = field(default_factory=list)

    @property
    def items(self) -> list[tuple[Path, int]]:
        return [*self.stale_t, *self.other_tiles, *self.variants]

    @property
    def total_bytes(self) -> int:
        return sum(b for _, b in self.items)


def expected_tiles(depts: list[str], species: list[str]) -> set[str]:
    """Tous les PMTiles qu'une publication courante peut envoyer : le jeu de `depts` et un jeu
    par département, T, A, E et G."""
    from .publish import dept_sets, expected_names

    return {
        name
        for s in dept_sets(depts, per_dept=True)
        for name in expected_names(s, species, groups=True)
    }


def plan(depts: list[str] | None = None) -> Plan:
    """Recense ce qui est périmé, sans rien écrire."""
    from .batch import DEPTS_ALL

    depts = sorted(depts or DEPTS_ALL)
    species = list_species()
    out = Plan()
    if config.T_DIR.is_dir():
        for folder in sorted(p for p in config.T_DIR.iterdir() if p.is_dir()):
            suffix = f"-{species_hash(folder.name)}" if folder.name in species else None
            for f in sorted(folder.glob("*.tif")):
                if suffix is None or not f.stem.endswith(suffix):
                    out.stale_t.append((f, tree_bytes(f)))
    if config.TILES_DIR.is_dir():
        wanted = expected_tiles(depts, species)
        for f in sorted(config.TILES_DIR.glob("*.pmtiles")):
            if f.name not in wanted:
                out.other_tiles.append((f, tree_bytes(f)))
    if config.VARIANTS_DIR.is_dir():
        out.variants = [(p, tree_bytes(p)) for p in sorted(config.VARIANTS_DIR.iterdir())]
    return out


def apply(p: Plan) -> int:
    """Efface ce que le plan nomme ; renvoie les octets rendus. Un dossier d'espèce vidé de ses
    T disparaît avec eux."""
    freed = 0
    for path, size in p.items:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            continue
        freed += size
    for path, _ in p.stale_t:
        with contextlib.suppress(OSError):
            path.parent.rmdir()  # seulement s'il est vide
    return freed
