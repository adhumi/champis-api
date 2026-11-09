"""Étape « grid » : rasters alignés 25 m d'un département — host, cover, ph, alt, slope, aspect."""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.warp import reproject, transform_bounds

from .config import CRS_CALC, RES_M, Grid, dept_dirs
from .scoring import NODATA_U8, NODATA_U16
from .species import (
    ASPECT_CLASSES,
    COVER_CLASSES,
    MAPPING_FILE,
    host_index,
    host_vocabulary,
    load_mapping,
)

log = logging.getLogger(__name__)


def _fresh(out: Path, *inputs: Path) -> bool:
    return out.exists() and all(out.stat().st_mtime >= p.stat().st_mtime for p in inputs)


def dept_grid(shp: Path) -> Grid:
    """Emprise alignée du département à partir de l'emprise de BD Forêt."""
    return Grid.aligned(*_total_bounds(shp))


def _total_bounds(shp: Path) -> tuple[float, float, float, float]:
    import pyogrio

    info = pyogrio.read_info(shp)
    xmin, ymin, xmax, ymax = info["total_bounds"]
    return float(xmin), float(ymin), float(xmax), float(ymax)


def _write(path: Path, arr: np.ndarray, grid: Grid, dtype: str, nodata: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", **grid.profile(dtype, nodata)) as dst:
        dst.write(arr.astype(dtype), 1)


# --------------------------------------------------------------------------- BD Forêt


def host_cover(dept: str, shp: Path, grid: Grid, force: bool = False) -> tuple[Path, Path]:
    d = dept_dirs(dept)["grid"]
    host_p, cover_p = d / "host.tif", d / "cover.tif"
    if not force and _fresh(host_p, shp, MAPPING_FILE) and _fresh(cover_p, shp, MAPPING_FILE):
        return host_p, cover_p

    mapping = load_mapping()["bdforet_v2"]
    log.info("lecture %s", shp)
    gdf = gpd.read_file(shp, columns=["CODE_TFV"])
    if gdf.crs is None:
        log.warning("%s sans .prj : Lambert-93 supposé (BD Forêt)", shp.name)
        gdf = gdf.set_crs(CRS_CALC)
    elif gdf.crs.to_epsg() != 2154:
        gdf = gdf.to_crs(CRS_CALC)
    gdf["CODE_TFV"] = gdf["CODE_TFV"].fillna("").astype(str).str.strip()
    unknown = sorted(set(gdf["CODE_TFV"]) - set(mapping))
    if unknown:
        raise RuntimeError(f"codes TFV absents de la table : {unknown}")
    gdf["host_idx"] = [host_index(mapping[c]["host"]) for c in gdf["CODE_TFV"]]
    gdf["cover_idx"] = [COVER_CLASSES.index(mapping[c]["cover"]) for c in gdf["CODE_TFV"]]
    # les petits polygones gagnent : rasteriser les grands d'abord
    gdf["_area"] = gdf.geometry.area
    gdf = gdf.sort_values("_area", ascending=False)

    shape = (grid.height, grid.width)
    tr = grid.transform()
    log.info("rasterisation host/cover %s (%d polygones, %dx%d)", dept, len(gdf), *shape)
    host = rasterize(
        zip(gdf.geometry, gdf["host_idx"], strict=True),
        out_shape=shape,
        transform=tr,
        fill=NODATA_U8,
        dtype="uint8",
        all_touched=False,
    )
    cover = rasterize(
        zip(gdf.geometry, gdf["cover_idx"], strict=True),
        out_shape=shape,
        transform=tr,
        fill=NODATA_U8,
        dtype="uint8",
        all_touched=False,
    )
    _write(host_p, host, grid, "uint8", NODATA_U8)
    _write(cover_p, cover, grid, "uint8", NODATA_U8)
    return host_p, cover_p


# --------------------------------------------------------------------------- lisière

EDGE_CAP_PX = 40  # 1 000 m à 25 m : au-delà, « loin de la lisière » suffit au trapèze `edge_m`
NON_FOREST_COVERS = ("sans_couvert", "non_foret")


def forest_mask(host: np.ndarray, cover: np.ndarray) -> np.ndarray:
    """Pixels de forêt au sens de la lisière : un polygone BD Forêt (ou un repli forestier) dont
    l'hôte n'est pas `herbacee` et le couvert ni `sans_couvert` ni `non_foret`. Une coupe rase ou
    une trouée sans couvert fait donc une lisière, comme le bord du massif."""
    vocab = host_vocabulary()
    forest = host != NODATA_U8
    if "herbacee" in vocab:
        forest &= host != vocab.index("herbacee")
    for name in NON_FOREST_COVERS:
        forest &= cover != COVER_CLASSES.index(name)
    return forest


def edge_distance_px(forest: np.ndarray, cap: int = EDGE_CAP_PX) -> np.ndarray:
    """Distance de chaque pixel de forêt au pixel non forestier le plus proche, en pixels, par
    érosions successives du masque (voisinage de 4 puis de 8 en alternance : distance octogonale,
    à moins de 8 % de l'euclidienne), plafonnée à `cap`. 0 hors forêt. Le bord du raster est
    traité comme de la forêt : la limite d'un département ne fait pas de fausse lisière."""
    dist = np.zeros(forest.shape, dtype=np.uint8)
    inner = forest.copy()
    for k in range(1, cap + 1):
        padded = np.pad(inner, 1, mode="edge")
        eroded = (
            padded[1:-1, 1:-1]
            & padded[:-2, 1:-1]
            & padded[2:, 1:-1]
            & padded[1:-1, :-2]
            & padded[1:-1, 2:]
        )
        if k % 2 == 0:  # voisinage de 8 un pas sur deux
            eroded &= padded[:-2, :-2] & padded[:-2, 2:] & padded[2:, :-2] & padded[2:, 2:]
        ring = inner & ~eroded
        dist[ring] = k
        inner = eroded
        if not inner.any():
            return dist
    dist[inner] = cap
    return dist


def edge(dept: str, host_p: Path, cover_p: Path, grid: Grid, force: bool = False) -> Path:
    """`grid/<dept>/edge.tif` : distance à la lisière en pixels de 25 m (palier 2, 9 nov. 2026),
    dérivée de host.tif et cover.tif, 255 = nodata (jamais : tout pixel a une distance)."""
    out = dept_dirs(dept)["grid"] / "edge.tif"
    if not force and _fresh(out, host_p, cover_p):
        return out
    with rasterio.open(host_p) as h, rasterio.open(cover_p) as c:
        host, cover = h.read(1), c.read(1)
    log.info("distance à la lisière %s (%dx%d, plafond %d px)", dept, *host.shape, EDGE_CAP_PX)
    dist = edge_distance_px(forest_mask(host, cover))
    _write(out, dist, grid, "uint8", NODATA_U8)
    return out


# --------------------------------------------------------------------------- SoilGrids


def ph(dept: str, src: Path, grid: Grid, force: bool = False) -> Path:
    """Rééchantillonnage bilinéaire de SoilGrids (pH × 10, 250 m) sur la grille 25 m. 255 = inconnu."""
    out = dept_dirs(dept)["grid"] / "ph.tif"
    if not force and _fresh(out, src):
        return out
    dst = np.full((grid.height, grid.width), NODATA_U8, dtype=np.uint8)
    with rasterio.open(src) as s:
        data = s.read(1).astype(np.float32)
        nodata = s.nodata if s.nodata is not None else 0
        data[data == nodata] = np.nan
        tmp = np.full(dst.shape, np.nan, dtype=np.float32)
        reproject(
            data,
            tmp,
            src_transform=s.transform,
            src_crs=s.crs,
            dst_transform=grid.transform(),
            dst_crs=CRS_CALC,
            resampling=Resampling.bilinear,
            src_nodata=np.nan,
            dst_nodata=np.nan,
        )
    ok = ~np.isnan(tmp)
    dst[ok] = np.clip(np.rint(tmp[ok]), 0, 254).astype(np.uint8)
    _write(out, dst, grid, "uint8", NODATA_U8)
    return out


# --------------------------------------------------------------------------- MNT


def dem_products(
    dept: str, dem_tiles: list[Path], grid: Grid, force: bool = False
) -> dict[str, Path]:
    """alt (uint16 m), slope (uint8 °), aspect (uint8 classe) — reprojection rasterio + Horn (numpy).

    Pente et exposition sont calculées sur la mosaïque reprojetée entière (pas de bord de dalle).
    """
    d = dept_dirs(dept)["grid"]
    d.mkdir(parents=True, exist_ok=True)
    outs = {"alt": d / "alt.tif", "slope": d / "slope.tif", "aspect": d / "aspect.tif"}
    if not force and all(_fresh(p, *dem_tiles) for p in outs.values()):
        return outs

    from rasterio.merge import merge

    srcs = [rasterio.open(p) for p in dem_tiles]
    mosaic, mosaic_tr = merge(srcs, nodata=-9999.0, dtype="float32")
    src_crs = srcs[0].crs
    for s in srcs:
        s.close()
    alt = np.full((grid.height, grid.width), -9999.0, dtype=np.float32)
    reproject(
        mosaic[0],
        alt,
        src_transform=mosaic_tr,
        src_crs=src_crs,
        dst_transform=grid.transform(),
        dst_crs=CRS_CALC,
        resampling=Resampling.bilinear,
        src_nodata=-9999.0,
        dst_nodata=-9999.0,
    )
    valid = alt > -9000
    z = np.where(valid, alt, np.nan).astype(np.float32)
    # Horn 1981 : dérivées par différences finies 3×3 sur grille RES_M
    pad = np.pad(z, 1, mode="edge")
    a, b, c = pad[:-2, :-2], pad[:-2, 1:-1], pad[:-2, 2:]
    dd, _, f = pad[1:-1, :-2], pad[1:-1, 1:-1], pad[1:-1, 2:]
    g, h, i = pad[2:, :-2], pad[2:, 1:-1], pad[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * dd + g)) / (8 * RES_M)
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * RES_M)
    slope = np.degrees(np.arctan(np.hypot(dzdx, dzdy)))
    # exposition : 0 = nord, sens horaire (convention gdaldem)
    # vecteur aval = (−dz/dE, −dz/dN) avec dz/dN = −dzdy (lignes vers le sud) → azimut = atan2(−dzdx, dzdy)
    aspect = np.degrees(np.arctan2(-dzdx, dzdy)) % 360.0
    ok = valid & np.isfinite(slope)

    alt_u16 = np.where(valid, np.clip(np.rint(alt), 0, NODATA_U16 - 1), NODATA_U16).astype(
        np.uint16
    )
    slope_u8 = np.where(ok, np.clip(np.rint(np.nan_to_num(slope)), 0, 90), NODATA_U8).astype(
        np.uint8
    )
    cls = (np.rint(np.nan_to_num(aspect) / 45.0).astype(np.int32)) % 8
    aspect_u8 = np.where(ok, cls, NODATA_U8).astype(np.uint8)
    aspect_u8[ok & (np.nan_to_num(slope) < 3.0)] = ASPECT_CLASSES.index("flat")

    _write(outs["alt"], alt_u16, grid, "uint16", NODATA_U16)
    _write(outs["slope"], slope_u8, grid, "uint8", NODATA_U8)
    _write(outs["aspect"], aspect_u8, grid, "uint8", NODATA_U8)
    return outs


def grid_bbox_4326(grid: Grid) -> tuple[float, float, float, float]:
    return transform_bounds(CRS_CALC, "EPSG:4326", grid.xmin, grid.ymin, grid.xmax, grid.ymax)
