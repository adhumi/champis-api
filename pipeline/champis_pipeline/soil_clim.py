"""Climatologie du sol depuis ERA5-Land (Climate Data Store) → `soil_clim` de la base d'amorçage.

Décision du 25 oct. 2026 (option a) : plutôt que 26 appels pondérés Open-Meteo par cellule, les
10 ans d'humidité du sol 0–7 cm viennent du CDS Copernicus (gratuit, compte requis), jeu
`derived-era5-land-daily-statistics` (moyenne journalière de l'horaire ERA5-Land, 0,1°), variable
`volumetric_soil_water_layer_1` (m³/m³). C'est la source d'Open-Meteo `soil_moisture_0_to_7cm_mean`,
donc le percentile calculé est le même que celui que `champis-api seed --climatology` obtiendrait.

Étapes : une requête CDS par année (NetCDF, parfois dans un zip) dans `data/era5land/` ; quantiles
0, 0,1 … 1 par mois et par pixel ; chaque cellule de la grille météo prend le pixel valide le plus
proche (la mer est NaN : une cellule côtière cherche dans son voisinage) ; écriture dans
`soil_clim` de `data/weather_seed.db` (schéma `weather_store`), `meta.seed_generated` = aujourd'hui.

Identifiants CDS : `~/.cdsapirc` ou les variables `CDSAPI_URL` / `CDSAPI_KEY` (le `.env` convient).
"""

from __future__ import annotations

import json
import logging
import zipfile
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from .config import DATA_DIR
from .weather_store import META_GENERATED, connect

log = logging.getLogger(__name__)

DATASET = "derived-era5-land-daily-statistics"
VARIABLE = "volumetric_soil_water_layer_1"
AREA_FRANCE = [51.5, -5.5, 41.0, 10.0]  # nord, ouest, sud, est (Corse incluse)
CLIMATOLOGY_YEARS = 10  # comme le client Open-Meteo : 10 années civiles complètes
QUANTILES = [i / 10 for i in range(11)]
ERA5_DIR = DATA_DIR / "era5land"
SEED_DB = DATA_DIR / "weather_seed.db"
# Une cellule côtière (pixel ERA5-Land en mer = NaN) prend le pixel valide le plus proche à ≤ 0,5°
# (Ouessant est à 0,3° de la côte) ; sans aucun pixel valide dans ce rayon, la cellule est sautée
# avec un avertissement (elle restera « amorçage requis » dans l'API). Au-delà d'un pixel et demi du
# pixel le plus proche, la grille est hors de l'emprise téléchargée : erreur.
NEIGHBOURHOOD = 5
MAX_OFFSET_DEG = 0.15


def default_years(today: date | None = None) -> list[int]:
    y = (today or date.today()).year
    return list(range(y - CLIMATOLOGY_YEARS, y))


# ---------------------------------------------------------------------- téléchargement


def request(year: int, area: list[float] = AREA_FRANCE) -> dict[str, Any]:
    return {
        "variable": [VARIABLE],
        "year": str(year),
        "month": [f"{m:02d}" for m in range(1, 13)],
        "day": [f"{d:02d}" for d in range(1, 32)],
        "daily_statistic": "daily_mean",
        "time_zone": "utc+01:00",
        "frequency": "1_hourly",
        "area": area,
        "data_format": "netcdf",
    }


def download(years: list[int], out_dir: Path = ERA5_DIR, client: Any = None) -> list[Path]:
    """Une requête CDS par année manquante ; renvoie les NetCDF (zip dépaqueté si besoin)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for year in years:
        nc = out_dir / f"swvl1_{year}.nc"
        if not nc.exists():
            if client is None:
                import cdsapi

                client = cdsapi.Client()
            tmp = out_dir / f"swvl1_{year}.download"
            log.info("CDS %s %d → %s", DATASET, year, nc)
            client.retrieve(DATASET, request(year), str(tmp))
            _unpack(tmp, nc)
        paths.append(nc)
    return paths


def _unpack(tmp: Path, nc: Path) -> None:
    if zipfile.is_zipfile(tmp):
        with zipfile.ZipFile(tmp) as z:
            names = [n for n in z.namelist() if n.endswith(".nc")]
            if len(names) != 1:
                raise ValueError(f"{tmp} : {len(names)} NetCDF dans l'archive")
            nc.write_bytes(z.read(names[0]))
        tmp.unlink()
    else:
        tmp.replace(nc)


# ---------------------------------------------------------------------- climatologie


def climatology(paths: list[Path]) -> xr.DataArray:
    """Quantiles (month, quantile, lat, lon) de la moyenne journalière sur toutes les années."""
    parts = []
    for p in paths:
        with xr.open_dataset(p) as ds:
            da = _soil_var(ds)
            tdim = next(d for d in da.dims if d in ("valid_time", "time"))
            da = da.rename({tdim: "time", _dim(da, "lat"): "lat", _dim(da, "lon"): "lon"})
            parts.append(da.load())
    da = xr.concat(parts, dim="time").sortby("time")
    q = da.groupby("time.month").quantile(QUANTILES, dim="time", skipna=False)
    return q.transpose("month", "quantile", "lat", "lon")


def _soil_var(ds: xr.Dataset) -> xr.DataArray:
    for name in ("swvl1", VARIABLE):
        if name in ds:
            return ds[name]
    names = [n for n in ds.data_vars if ds[n].ndim == 3]
    if len(names) != 1:
        raise ValueError(f"variable d'humidité du sol introuvable parmi {list(ds.data_vars)}")
    return ds[names[0]]


def _dim(da: xr.DataArray, kind: str) -> str:
    cands = ("lat", "latitude") if kind == "lat" else ("lon", "longitude")
    return next(d for d in da.dims if d in cands)


def sample(
    clim: xr.DataArray, cells: dict[str, tuple[float, float]]
) -> tuple[dict[str, dict[str, list[float]]], list[str]]:
    """Par cellule (lat, lon) : {"1": [11 quantiles], …, "12": […]} du pixel valide le plus proche,
    et la liste des cellules sautées (aucun pixel valide à ≤ NEIGHBOURHOOD pixels : îlots).

    Lève si une cellule est hors de l'emprise téléchargée.
    """
    lats, lons = clim["lat"].values, clim["lon"].values
    values = clim.values  # (12, 11, nlat, nlon)
    valid = ~np.isnan(values[0, 0])
    out, skipped = {}, []
    for cid, (lat, lon) in cells.items():
        i, j = int(np.abs(lats - lat).argmin()), int(np.abs(lons - lon).argmin())
        if abs(lats[i] - lat) > MAX_OFFSET_DEG or abs(lons[j] - lon) > MAX_OFFSET_DEG:
            raise ValueError(f"({lat:.3f}, {lon:.3f}) hors de l'emprise ERA5-Land téléchargée")
        if not valid[i, j]:
            near = _nearest_valid(valid, lats, lons, lat, lon, i, j)
            if near is None:
                log.warning(
                    "%s (%.3f, %.3f) : aucun pixel ERA5-Land valide à ≤ %.1f°, sautée",
                    cid,
                    lat,
                    lon,
                    NEIGHBOURHOOD / 10,
                )
                skipped.append(cid)
                continue
            i, j = near
        out[cid] = {str(m + 1): [float(v) for v in values[m, :, i, j]] for m in range(12)}
    return out, skipped


def _nearest_valid(valid, lats, lons, lat, lon, i, j) -> tuple[int, int] | None:
    best, best_d = None, np.inf
    for di in range(-NEIGHBOURHOOD, NEIGHBOURHOOD + 1):
        for dj in range(-NEIGHBOURHOOD, NEIGHBOURHOOD + 1):
            ii, jj = i + di, j + dj
            if 0 <= ii < len(lats) and 0 <= jj < len(lons) and valid[ii, jj]:
                d = (lats[ii] - lat) ** 2 + (lons[jj] - lon) ** 2
                if d < best_d:
                    best, best_d = (ii, jj), d
    return best


# ---------------------------------------------------------------------- base d'amorçage


def write_seed(
    clims: dict[str, dict[str, list[float]]], seed_db: Path = SEED_DB, today: date | None = None
) -> int:
    seed_db.parent.mkdir(parents=True, exist_ok=True)
    db = connect(seed_db)
    try:
        db.executemany(
            "INSERT OR REPLACE INTO soil_clim VALUES (?, ?)",
            [(cid, json.dumps(months)) for cid, months in clims.items()],
        )
        db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)",
            (META_GENERATED, (today or date.today()).isoformat()),
        )
        db.commit()
    finally:
        db.close()
    return len(clims)


def grid_cells(grid_path: Path) -> dict[str, tuple[float, float]]:
    raw = json.loads(grid_path.read_text(encoding="utf-8"))
    return {c["id"]: (float(c["lat"]), float(c["lon"])) for c in raw["cells"]}


def build(
    grid_path: Path,
    years: list[int] | None = None,
    seed_db: Path = SEED_DB,
    era5_dir: Path = ERA5_DIR,
    client: Any = None,
) -> int:
    """Télécharge (si besoin), calcule, échantillonne, écrit. Renvoie le nombre de cellules."""
    years = years or default_years()
    paths = download(years, era5_dir, client)
    clim = climatology(paths)
    cells = grid_cells(grid_path)
    clims, skipped = sample(clim, cells)
    n = write_seed(clims, seed_db)
    log.info(
        "climatologie du sol : %d cellules, %d sautées (%s), années %d–%d → %s",
        n,
        len(skipped),
        ", ".join(skipped) or "aucune",
        years[0],
        years[-1],
        seed_db,
    )
    return n
