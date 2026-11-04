"""Étape « status » : où en est chaque département sur ce poste, et ce que le bucket contient.

Lecture seule. Pour un département : BD Forêt extraite (`raw`), les six rasters `grid`, les T par
espèce, le dernier résultat du `batch`, les PMTiles de son propre jeu (hors ligne de l'app). Pour
la liste demandée : les PMTiles du jeu complet, en local et dans le bucket s'il est configuré.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config
from .species import list_species, species_hash

GRID_RASTERS = ("host", "cover", "ph", "alt", "slope", "aspect")


@dataclass
class DeptStatus:
    dept: str
    raw: bool
    grid: int  # rasters présents sur six
    terrain: int  # espèces dont le T est calculé
    species: int
    batch: str  # "ok", "ECHEC", "-" (jamais passé)
    tiles: int  # PMTiles du jeu de ce seul département, présents
    tiles_expected: int

    @property
    def ready(self) -> bool:
        """Prêt pour `tiles` : rasters et T complets."""
        return self.grid == len(GRID_RASTERS) and self.terrain == self.species

    def cells(self) -> list[str]:
        """Colonnes du tableau, dans l'ordre de `COLUMNS`."""
        return [
            self.dept,
            "oui" if self.raw else "-",
            f"{self.grid}/{len(GRID_RASTERS)}",
            f"{self.terrain}/{self.species}",
            self.batch,
            f"{self.tiles}/{self.tiles_expected}" if self.tiles_expected else "-",
            "prêt" if self.ready else "incomplet",
        ]


COLUMNS = ("dept", "raw", "grid", "T", "batch", "tuiles dept", "état")


@dataclass
class SetStatus:
    """Le jeu complet des départements demandés : fichiers attendus, présents en local, dans le bucket."""

    depts: list[str]
    expected: list[str]
    local: list[str] = field(default_factory=list)
    bucket: list[str] | None = None  # None : bucket non configuré ou non interrogé
    bucket_error: str | None = None
    manifest_generated: str | None = None


def dept_status(
    dept: str,
    species: list[str] | None = None,
    batch_status: dict[str, dict] | None = None,
    groups: bool = False,
) -> DeptStatus:
    """`groups` : compter aussi les PMTiles G (un par groupe) parmi les tuiles attendues."""
    from .publish import expected_names

    species = species or list_species()
    if batch_status is None:
        from .batch import load_status

        batch_status = load_status()
    raw_dir = config.RAW_DIR / dept / "bdforet" / "extracted"
    raw = raw_dir.exists() and next(raw_dir.rglob("FORMATION_VEGETALE.shp"), None) is not None
    grid_dir = config.GRID_DIR / dept
    grid = sum((grid_dir / f"{k}.tif").exists() for k in GRID_RASTERS)
    terrain = sum(
        (config.T_DIR / sid / f"{dept}-{species_hash(sid)}.tif").exists() for sid in species
    )
    entry = batch_status.get(dept)
    batch = "-" if entry is None else ("ok" if entry.get("ok") else "ECHEC")
    names = expected_names([dept], species, groups)
    tiles = sum((config.TILES_DIR / n).exists() for n in names)
    return DeptStatus(dept, raw, grid, terrain, len(species), batch, tiles, len(names))


def set_status(
    depts: list[str],
    species: list[str] | None = None,
    bucket: Any | None = None,
    groups: bool = False,
) -> SetStatus:
    """`bucket` : objet au contrat `BucketLike`, ou None pour ne pas interroger ; `groups` : les G
    font partie du jeu attendu."""
    from .publish import expected_names
    from .storage import MANIFEST_KEY, TILES_PREFIX

    species = species or list_species()
    expected = expected_names(sorted(depts), species, groups)
    status = SetStatus(
        sorted(depts), expected, [n for n in expected if (config.TILES_DIR / n).exists()]
    )
    if bucket is not None:
        try:
            present = bucket.list(TILES_PREFIX)
            status.bucket = [n for n in expected if TILES_PREFIX + n in present]
            manifest = bucket.get_json(MANIFEST_KEY) or {}
            status.manifest_generated = manifest.get("generated")
        except Exception as e:  # réseau, identifiants : on le dit, on ne plante pas
            status.bucket_error = f"{type(e).__name__}: {e}"
    return status


def table(rows: list[list[str]], header: tuple[str, ...]) -> str:
    """Tableau texte aligné (données : stdout)."""
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(header)
    ]
    line = lambda cells: "  ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)).rstrip()  # noqa: E731
    return "\n".join([line(header), line(tuple("-" * w for w in widths)), *(line(r) for r in rows)])
