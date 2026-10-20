"""Étape « terrain » : T par espèce, par blocs, depuis les rasters grid/ du département."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import Window

from .config import BLOCK, T_DIR, dept_dirs
from .progress import tqdm
from .scoring import NODATA_U8, encode_u8, terrain_potential
from .species import load_species, species_hash

log = logging.getLogger(__name__)


def compute(dept: str, species_id: str, force: bool = False) -> Path:
    g = dept_dirs(dept)["grid"]
    srcs = {k: g / f"{k}.tif" for k in ("host", "cover", "ph", "alt", "slope", "aspect")}
    missing = [k for k, p in srcs.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"rasters grid manquants pour {dept} : {missing}")
    out = T_DIR / species_id / f"{dept}-{species_hash(species_id)}.tif"
    if out.exists() and not force:
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    sp = load_species(species_id)

    handles = {k: rasterio.open(p) for k, p in srcs.items()}
    ref = handles["host"]
    profile = ref.profile.copy()
    profile.update(dtype="uint8", nodata=NODATA_U8, compress="DEFLATE", predictor=2, tiled=True)
    windows = [
        Window(c, r, min(BLOCK, ref.width - c), min(BLOCK, ref.height - r))
        for r in range(0, ref.height, BLOCK)
        for c in range(0, ref.width, BLOCK)
    ]
    with rasterio.open(out, "w", **profile) as dst:
        for w in tqdm(windows, desc=f"T {species_id} {dept}", unit="bloc"):
            host = handles["host"].read(1, window=w)
            t = terrain_potential(
                sp,
                host,
                handles["cover"].read(1, window=w),
                handles["ph"].read(1, window=w),
                handles["alt"].read(1, window=w),
                handles["slope"].read(1, window=w),
                handles["aspect"].read(1, window=w),
            )
            dst.write(encode_u8(t, nodata_mask=(host == NODATA_U8)), 1, window=w)
    for h in handles.values():
        h.close()
    log.info("écrit %s", out)
    return out


def summary(t_path: Path) -> dict[str, float]:
    """Surface par classe (ha) pour le rapport."""
    with rasterio.open(t_path) as s:
        v = s.read(1)
        px_ha = (s.res[0] * s.res[1]) / 10_000
    valid = v != NODATA_U8
    t = v[valid] / 254.0
    edges = [0.0, 0.15, 0.35, 0.55, 0.75, 1.01]
    labels = ["tres_faible", "faible", "moyen", "bon", "excellent"]
    hist, _ = np.histogram(t, bins=edges)
    out = {lab: float(n * px_ha) for lab, n in zip(labels, hist, strict=True)}
    out["forest_ha"] = float(valid.sum() * px_ha)
    return out
