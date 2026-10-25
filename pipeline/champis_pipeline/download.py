"""Téléchargements : BD Forêt (flux Géoplateforme), SoilGrids (WCS), MNT (Copernicus / BD ALTI).

Chaque fonction est idempotente : elle ne retélécharge pas un fichier présent et vérifié.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import httpx

from .config import RAW_DIR, bdforet_url
from .progress import tqdm

log = logging.getLogger(__name__)


def _download(url: str, dest: Path, expected_md5: str | None = None) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        if expected_md5 is None or _md5(dest) == expected_md5:
            log.info("présent : %s", dest.name)
            return dest
        log.warning("md5 différent, retéléchargement : %s", dest.name)
    tmp = dest.with_suffix(f"{dest.suffix}.{os.getpid()}.part")  # plusieurs processus (batch)
    with httpx.stream("GET", url, follow_redirects=True, timeout=120) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with (
            tmp.open("wb") as f,
            tqdm(total=total, unit="B", unit_scale=True, desc=dest.name) as bar,
        ):
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                bar.update(len(chunk))
    os.replace(tmp, dest)  # le dernier arrivé gagne, les deux fichiers sont identiques
    if expected_md5 and _md5(dest) != expected_md5:
        raise RuntimeError(f"md5 incorrect pour {dest}")
    return dest


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def bdforet(dept: str) -> Path:
    """Archive 7z BD Forêt V2 du département, puis extraction du shapefile FORMATION_VEGETALE."""
    import py7zr

    url = bdforet_url(dept)
    archive = RAW_DIR / dept / "bdforet" / url.rsplit("/", 1)[-1]
    _download(url, archive)
    out_dir = archive.parent / "extracted"
    shp = next(out_dir.rglob("FORMATION_VEGETALE.shp"), None)
    if shp is None:
        log.info("extraction %s", archive.name)
        with py7zr.SevenZipFile(archive, "r") as z:
            z.extractall(out_dir)
        shp = next(out_dir.rglob("FORMATION_VEGETALE.shp"))
    return shp


def soilgrids_ph(bbox_4326: tuple[float, float, float, float]) -> Path:
    """pH H2O 0–5 cm (phh2o_0-5cm_mean, valeur × 10) via le WCS ISRIC, en EPSG:4326, ~250 m.

    bbox = (lon_min, lat_min, lon_max, lat_max). Sortie GeoTIFF brut, à reprojeter ensuite.
    """
    lon0, lat0, lon1, lat1 = bbox_4326
    dest = RAW_DIR / "soilgrids" / f"phh2o_0-5cm_{lon0:.2f}_{lat0:.2f}_{lon1:.2f}_{lat1:.2f}.tif"
    if dest.exists():
        return dest
    url = (
        "https://maps.isric.org/mapserv?map=/map/phh2o.map"
        "&SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage&COVERAGEID=phh2o_0-5cm_mean"
        "&FORMAT=image/tiff"
        f"&SUBSET=long({lon0},{lon1})&SUBSET=lat({lat0},{lat1})"
        "&SUBSETTINGCRS=http://www.opengis.net/def/crs/EPSG/0/4326"
        "&OUTPUTCRS=http://www.opengis.net/def/crs/EPSG/0/4326"
    )
    _download(url, dest)
    _assert_raster(dest)
    return dest


def _assert_raster(path: Path) -> None:
    """Un WCS peut renvoyer une erreur XML en HTTP 200 : ne jamais mettre ça en cache."""
    import rasterio

    try:
        with rasterio.open(path) as r:
            if r.width == 0 or r.height == 0:
                raise RuntimeError("raster vide")
    except Exception as e:
        head = path.read_bytes()[:300]
        path.unlink(missing_ok=True)
        raise RuntimeError(f"téléchargement invalide {path.name} : {e} — début : {head!r}") from e


def copernicus_dem_tiles(bbox_4326: tuple[float, float, float, float]) -> list[Path]:
    """Dalles Copernicus DEM GLO-30 (1° × 1°) couvrant l'emprise, depuis le bucket public AWS.

    Alternative à BD ALTI (téléchargement IGN manuel, plus lourd). Même précision utile à 25 m.
    """
    import math

    lon0, lat0, lon1, lat1 = bbox_4326
    out: list[Path] = []
    for lat in range(math.floor(lat0), math.ceil(lat1)):
        for lon in range(math.floor(lon0), math.ceil(lon1)):
            ns = f"N{lat:02d}" if lat >= 0 else f"S{-lat:02d}"
            ew = f"E{lon:03d}" if lon >= 0 else f"W{-lon:03d}"
            name = f"Copernicus_DSM_COG_10_{ns}_00_{ew}_00_DEM"
            url = f"https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif"
            try:
                out.append(_download(url, RAW_DIR / "dem" / f"{name}.tif"))
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    log.info("dalle absente (mer ?) : %s", name)
                    continue
                raise
    if not out:
        raise RuntimeError("aucune dalle DEM trouvée pour l'emprise")
    return out
