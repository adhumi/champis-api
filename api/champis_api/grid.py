"""Grille météo : les cellules ~5 km de `data/weather_grid.json` (produit par le pipeline).

L'API ne connaît pas les pixels, seulement ces cellules, leurs classes d'altitude et, en version 2,
les poids Σ T par département, classe d'altitude et espèce qui servent à l'indice par département.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_VERSIONS = (1, 2)

# département → centre de classe d'altitude (texte, « 450 ») → espèce → Σ T des pixels
TSum = dict[str, dict[str, dict[str, float]]]


@dataclass(frozen=True)
class Cell:
    id: str
    lat: float
    lon: float
    alt_classes: tuple[float, ...]  # centres des classes de 300 m présentes (150, 450, …)
    elevation_m: float | None = None  # altitude médiane de la forêt de la cellule, informative
    t_sum: TSum = field(default_factory=dict)  # version 2 seulement ; vide en version 1

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "lat": self.lat,
            "lon": self.lon,
            "alt_classes": list(self.alt_classes),
            "elevation_m": self.elevation_m,
        }

    @property
    def depts(self) -> tuple[str, ...]:
        return tuple(sorted(self.t_sum))


class WeatherGrid:
    def __init__(self, cells: list[Cell], version: int = 1):
        self.cells = {c.id: c for c in cells}
        self.version = version

    @classmethod
    def load(cls, path: Path) -> WeatherGrid:
        raw = json.loads(path.read_text(encoding="utf-8"))
        version = raw.get("version")
        if version not in SUPPORTED_VERSIONS:
            raise ValueError(f"weather_grid.json version {version!r} inconnue")
        return cls(
            [
                Cell(
                    id=c["id"],
                    lat=float(c["lat"]),
                    lon=float(c["lon"]),
                    alt_classes=tuple(float(a) for a in c["alt_classes"]),
                    elevation_m=c.get("elevation_m"),
                    t_sum={
                        dept: {str(alt): dict(by_sp) for alt, by_sp in by_alt.items()}
                        for dept, by_alt in c.get("t_sum", {}).items()
                    },
                )
                for c in raw["cells"]
            ],
            version=version,
        )

    def __len__(self) -> int:
        return len(self.cells)

    def get(self, cell_id: str) -> Cell | None:
        return self.cells.get(cell_id)

    @property
    def has_weights(self) -> bool:
        """Vrai si la grille porte les Σ T (version 2) nécessaires à l'indice par département."""
        return any(c.t_sum for c in self.cells.values())

    def departements(self) -> list[str]:
        return sorted({d for c in self.cells.values() for d in c.t_sum})

    def nearest(self, lat: float, lon: float) -> Cell | None:
        """Cellule la plus proche (équirectangulaire : suffisant à 5 km)."""
        best, best_d = None, math.inf
        k = math.cos(math.radians(lat))
        for c in self.cells.values():
            d = (c.lat - lat) ** 2 + ((c.lon - lon) * k) ** 2
            if d < best_d:
                best, best_d = c, d
        return best

    def in_bbox(self, min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> list[Cell]:
        return [
            c
            for c in self.cells.values()
            if min_lon <= c.lon <= max_lon and min_lat <= c.lat <= max_lat
        ]
