"""Grille météo : les cellules ~5 km de `data/weather_grid.json` (produit par le pipeline).

L'API ne connaît pas les pixels, seulement ces cellules et leurs classes d'altitude.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Cell:
    id: str
    lat: float
    lon: float
    alt_classes: tuple[float, ...]  # centres des classes de 300 m présentes (150, 450, …)
    elevation_m: float | None = None  # altitude médiane de la forêt de la cellule, informative

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "lat": self.lat,
            "lon": self.lon,
            "alt_classes": list(self.alt_classes),
            "elevation_m": self.elevation_m,
        }


class WeatherGrid:
    def __init__(self, cells: list[Cell]):
        self.cells = {c.id: c for c in cells}

    @classmethod
    def load(cls, path: Path) -> WeatherGrid:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            raise ValueError(f"weather_grid.json version {raw.get('version')!r} inconnue")
        return cls(
            [
                Cell(
                    id=c["id"],
                    lat=float(c["lat"]),
                    lon=float(c["lon"]),
                    alt_classes=tuple(float(a) for a in c["alt_classes"]),
                    elevation_m=c.get("elevation_m"),
                )
                for c in raw["cells"]
            ]
        )

    def __len__(self) -> int:
        return len(self.cells)

    def get(self, cell_id: str) -> Cell | None:
        return self.cells.get(cell_id)

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
