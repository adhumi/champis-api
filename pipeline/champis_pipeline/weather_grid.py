"""Étape « weather-grid » : les cellules météo ~5 km où T > 0 pour au moins une espèce.

Sortie `data/weather_grid.json` (contrat lu par l'API, `champis_api.grid`) :

    {"version": 1, "cell_m": 5000,
     "cells": [{"id": "c_0930_6680", "lat": 47.12, "lon": 6.31,
                "alt_classes": [450, 750], "elevation_m": 612.0}, …]}

Une cellule = un carré de 5 km de la grille Lambert-93 (id = coin SO en km). `alt_classes` =
centres des classes de 300 m présentes sous la forêt de la cellule (pixels où T > 0) ;
`elevation_m` = altitude médiane de ces pixels, informative.
"""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path

import numpy as np
import rasterio
from pyproj import Transformer
from rasterio.windows import Window

from .config import CRS_CALC, DATA_DIR, RES_M, T_DIR, dept_dirs
from .scoring import NODATA_U8
from .species import list_species, load_palette, species_hash

log = logging.getLogger(__name__)

CELL_M = 5000
ALT_CLASS_M = int(load_palette()["alt_encoding"]["class_m"])  # partagé avec les clients
OUT = DATA_DIR / "weather_grid.json"


def build(depts: list[str], species: list[str] | None = None, out: Path = OUT) -> Path:
    species = species or list_species()
    cells: dict[str, dict] = {}
    for dept in depts:
        _dept_cells(dept, species, cells)
    to_wgs = Transformer.from_crs(CRS_CALC, "EPSG:4326", always_xy=True)
    rows = []
    for cid, c in sorted(cells.items()):
        lon, lat = to_wgs.transform(c["x"] + CELL_M / 2, c["y"] + CELL_M / 2)
        alts = np.concatenate(c["alts"])
        classes = sorted({int(a // ALT_CLASS_M * ALT_CLASS_M + ALT_CLASS_M // 2) for a in alts})
        rows.append(
            {
                "id": cid,
                "lat": round(float(lat), 5),
                "lon": round(float(lon), 5),
                "alt_classes": classes,
                "elevation_m": round(float(np.median(alts)), 1),
            }
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"version": 1, "cell_m": CELL_M, "cells": rows}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    log.info("écrit %s : %d cellules", out, len(rows))
    return out


def _dept_cells(dept: str, species: list[str], cells: dict[str, dict]) -> None:
    t_paths = [T_DIR / sid / f"{dept}-{species_hash(sid)}.tif" for sid in species]
    missing = [p for p in t_paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"T manquants : {missing}")
    alt_path = dept_dirs(dept)["grid"] / "alt.tif"
    handles = [rasterio.open(p) for p in t_paths]
    with rasterio.open(alt_path) as alt:
        ref = handles[0]
        step = int(CELL_M // RES_M)
        left, bottom, right, top = ref.bounds
        # cellules 5 km alignées sur les multiples de 5 km de Lambert-93 (cx, cy = coin SO)
        for cy in range(int(bottom // CELL_M) * CELL_M, math.ceil(top / CELL_M) * CELL_M, CELL_M):
            for cx in range(
                int(left // CELL_M) * CELL_M, math.ceil(right / CELL_M) * CELL_M, CELL_M
            ):
                row0, col0 = ref.index(cx, cy + CELL_M)  # coin NO de la cellule
                r0, c0 = max(int(row0), 0), max(int(col0), 0)
                r1, c1 = min(int(row0) + step, ref.height), min(int(col0) + step, ref.width)
                if r1 <= r0 or c1 <= c0:
                    continue
                win = Window(c0, r0, c1 - c0, r1 - r0)
                forest = np.zeros((r1 - r0, c1 - c0), dtype=bool)
                for h in handles:
                    t = h.read(1, window=win)
                    forest |= (t != NODATA_U8) & (t > 0)
                if not forest.any():
                    continue
                a = alt.read(1, window=win)[forest]
                if alt.nodata is not None:
                    a = a[a != alt.nodata]
                if a.size == 0:
                    continue
                cid = f"c_{cx // 1000:04d}_{cy // 1000:04d}"
                cells.setdefault(cid, {"x": cx, "y": cy, "alts": []})["alts"].append(
                    a.astype(float)
                )
    for h in handles:
        h.close()
