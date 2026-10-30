"""Étape « weather-grid » : les cellules météo ~5 km où T > 0 pour au moins une espèce.

Sortie `data/weather_grid.json` (contrat lu par l'API, `champis_api.grid`), version 2 :

    {"version": 2, "cell_m": 5000, "alt_class_m": 300, "species_hash": "af34c16e",
     "cells": [{"id": "c_0930_6680", "lat": 47.12, "lon": 6.31,
                "alt_classes": [450, 750], "elevation_m": 612.0,
                "t_sum": {"25": {"450": {"boletus_edulis": 1234.5, …}, "750": {…}}}}, …]}

Une cellule = un carré de 5 km de la grille Lambert-93 (id = coin SO en km). `alt_classes` =
centres des classes de 300 m présentes sous la forêt de la cellule (pixels où T > 0) ;
`elevation_m` = altitude médiane de ces pixels, informative.

`t_sum` (version 2, spec › Scoring › indice par département, 30 oct. 2026) : par département
que la cellule touche, par classe d'altitude et par espèce, la somme des T (0–1) des pixels de la
cellule. C'est le poids de l'indice par département `I = Σ T × M_eff × Φ / Σ T`, calculé par
l'API à la fin de chaque pré-chauffe ; une cellule à cheval sur deux départements porte les deux.
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
from .scoring import decode_u8
from .species import all_species_hash, list_species, load_palette, species_hash

log = logging.getLogger(__name__)

VERSION = 2
CELL_M = 5000
ALT_CLASS_M = int(load_palette()["alt_encoding"]["class_m"])  # partagé avec les clients
OUT = DATA_DIR / "weather_grid.json"


def alt_class_center(alt_m: float) -> int:
    return int(alt_m // ALT_CLASS_M * ALT_CLASS_M + ALT_CLASS_M // 2)


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
        classes = sorted({alt_class_center(a) for a in alts})
        t_sum = {
            dept: {
                str(center): {sid: round(v, 2) for sid, v in sorted(by_sp.items()) if v > 0}
                for center, by_sp in sorted(by_alt.items())
            }
            for dept, by_alt in sorted(c["t_sum"].items())
        }
        rows.append(
            {
                "id": cid,
                "lat": round(float(lat), 5),
                "lon": round(float(lon), 5),
                "alt_classes": classes,
                "elevation_m": round(float(np.median(alts)), 1),
                "t_sum": t_sum,
            }
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "version": VERSION,
                "cell_m": CELL_M,
                "alt_class_m": ALT_CLASS_M,
                "species_hash": all_species_hash(),
                "cells": rows,
            },
            ensure_ascii=False,
            indent=1,
        ),
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
                t_by_species = {
                    sid: decode_u8(h.read(1, window=win))
                    for sid, h in zip(species, handles, strict=True)
                }
                forest = np.zeros((r1 - r0, c1 - c0), dtype=bool)
                for t in t_by_species.values():
                    forest |= np.nan_to_num(t, nan=0.0) > 0
                if not forest.any():
                    continue
                a = alt.read(1, window=win)
                valid = forest if alt.nodata is None else forest & (a != alt.nodata)
                if not valid.any():
                    continue
                cid = f"c_{cx // 1000:04d}_{cy // 1000:04d}"
                cell = cells.setdefault(cid, {"x": cx, "y": cy, "alts": [], "t_sum": {}})
                cell["alts"].append(a[valid].astype(float))
                # Σ T par classe d'altitude et par espèce, pour ce département
                sums = cell["t_sum"].setdefault(dept, {})
                centers = (a[valid] // ALT_CLASS_M).astype(int) * ALT_CLASS_M + ALT_CLASS_M // 2
                for center in np.unique(centers):
                    mask = centers == center
                    by_sp = sums.setdefault(int(center), {})
                    for sid, t in t_by_species.items():
                        v = float(np.nansum(t[valid][mask]))
                        by_sp[sid] = by_sp.get(sid, 0.0) + v
    for h in handles:
        h.close()
