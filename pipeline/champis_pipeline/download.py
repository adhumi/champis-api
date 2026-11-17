"""Téléchargements : BD Forêt (flux Géoplateforme), SoilGrids (WCS), MNT (Copernicus / BD ALTI),
BD TOPO (WFS Géoplateforme, palier 3 du 12 nov. 2026).

Chaque fonction est idempotente : elle ne retélécharge pas un fichier présent et vérifié.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
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


# --------------------------------------------------------------------------- BD TOPO (WFS)

WFS_URL = "https://data.geopf.fr/wfs/ows"
WFS_PAGE = 5000  # maximum servi par page par la Géoplateforme
# Un département demande des dizaines de pages d'une dizaine de Mo : une page qui échoue (erreur
# HTTP, connexion coupée, corps vide ou qui n'est pas du JSON, le serveur répondant parfois 200
# avec une page d'erreur) est redemandée après ces attentes, en secondes, avant d'abandonner.
WFS_RETRY_WAITS = (5, 20, 60)


class WfsError(RuntimeError):
    """Le flux WFS n'a pas rendu une page GeoJSON lisible, malgré les nouvelles tentatives."""


def _wfs_page(http: httpx.Client, params: dict, what: str) -> dict:
    """Une page GeoJSON du WFS, redemandée sur toute réponse inutilisable. Le message d'échec dit
    ce que le serveur a renvoyé (code, type, début du corps) : un `JSONDecodeError` nu ne disait
    rien de la cause (lot France du 16 nov. 2026)."""
    last = ""
    for attempt in range(len(WFS_RETRY_WAITS) + 1):
        if attempt:
            wait = WFS_RETRY_WAITS[attempt - 1]
            log.warning("%s : %s ; nouvel essai dans %d s", what, last, wait)
            time.sleep(wait)
        try:
            r = http.get(WFS_URL, params=params)
        except httpx.HTTPError as e:
            last = f"{type(e).__name__} : {e}"
            continue
        status = getattr(r, "status_code", 200)
        if status >= 400:
            last = f"HTTP {status}"
            if status < 500 and status not in (408, 429):
                break  # requête refusée : la redemander ne change rien
            continue
        try:
            body = r.json()
        except ValueError:
            kind = getattr(r, "headers", {}).get("content-type", "type inconnu")
            start = " ".join(getattr(r, "text", "")[:200].split())
            last = f"réponse {status} qui n'est pas du JSON ({kind}) : {start or 'corps vide'}"
            continue
        if isinstance(body, dict) and isinstance(body.get("features"), list):
            return body
        last = f"JSON sans liste `features` : {str(body)[:200]}"
    raise WfsError(f"{what} : {last}")


BDTOPO_LAYERS = {
    # couche WFS → (fichier, filtre CQL en plus de l'emprise, propriétés gardées)
    "zone_de_vegetation": (
        "vegetation.geojson",
        "nature IN ('Peupleraie','Bois','Verger','Haie','Forêt ouverte','Lande ligneuse')",
        "nature,geometrie",
    ),
    "troncon_hydrographique": ("troncons.geojson", "fictif = false", "nature,geometrie"),
    "surface_hydrographique": ("surfaces.geojson", None, "nature,geometrie"),
}


def bdtopo(
    dept: str,
    bbox_2154: tuple[float, float, float, float],
    http: httpx.Client | None = None,
    force: bool = False,
) -> dict[str, Path]:
    """Les objets BD TOPO V3 de l'emprise du département (Lambert-93), par le flux WFS de la
    Géoplateforme : zones de végétation hors forêt fermée, tronçons hydrographiques non fictifs,
    surfaces en eau ; GeoJSON réduit à la nature et à la géométrie, pages de `WFS_PAGE` objets (chacune redemandée si le serveur
    rend autre chose qu'un GeoJSON, `_wfs_page`),
    un fichier par couche dans data/raw/<dept>/bdtopo/. Idempotent : un fichier présent est gardé."""
    out_dir = RAW_DIR / dept / "bdtopo"
    out_dir.mkdir(parents=True, exist_ok=True)
    xmin, ymin, xmax, ymax = bbox_2154
    own = http is None
    http = http or httpx.Client(timeout=300.0, headers={"User-Agent": "champis-pipeline"})
    paths: dict[str, Path] = {}
    try:
        for layer, (name, extra, props) in BDTOPO_LAYERS.items():
            dest = out_dir / name
            paths[layer] = dest
            if dest.exists() and not force:
                log.info("présent : %s", dest.name)
                continue
            cql = f"BBOX(geometrie,{xmin:.0f},{ymin:.0f},{xmax:.0f},{ymax:.0f},'EPSG:2154')"
            if extra:
                cql += f" AND {extra}"
            features: list[dict] = []
            offset = 0
            while True:
                body = _wfs_page(
                    http,
                    {
                        "SERVICE": "WFS",
                        "VERSION": "2.0.0",
                        "REQUEST": "GetFeature",
                        "TYPENAMES": f"BDTOPO_V3:{layer}",
                        "OUTPUTFORMAT": "application/json",
                        "SRSNAME": "EPSG:2154",
                        "PROPERTYNAME": props,
                        "COUNT": WFS_PAGE,
                        "STARTINDEX": offset,
                        "CQL_FILTER": cql,
                    },
                    f"BD TOPO {dept} {layer} (objets {offset} à {offset + WFS_PAGE})",
                )
                page = body["features"]
                features += page
                total = body.get("totalFeatures") or body.get("numberMatched")
                log.info("BD TOPO %s %s : %d objets", dept, layer, len(features))
                if len(page) < WFS_PAGE or (isinstance(total, int) and len(features) >= total):
                    break
                offset += WFS_PAGE
            tmp = dest.with_suffix(f".{os.getpid()}.part")
            tmp.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "crs": {"type": "name", "properties": {"name": "EPSG:2154"}},
                        "features": features,
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, dest)
    finally:
        if own:
            http.close()
    return paths


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
