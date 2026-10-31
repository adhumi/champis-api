"""Étape « grid » : règles du skill geo-pipeline sur BD Forêt, SoilGrids et le MNT.

Jointure sur `CODE_TFV` exact (`FF1-00` ≠ `FF1-00-00`), codes `strip()`és, code inconnu bloquant,
petits polygones gagnants, shapefile sans `.prj` = Lambert-93 supposé ; pH nodata → 255 jamais
une moyenne ; exposition 8 classes + `flat` sous 3°, pente par Horn.
"""

from __future__ import annotations

import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from champis_pipeline import rasterize
from champis_pipeline.config import CRS_CALC, Grid
from champis_pipeline.scoring import NODATA_U8, NODATA_U16
from champis_pipeline.species import ASPECT_CLASSES, COVER_CLASSES, host_index
from rasterio.transform import from_origin
from shapely.geometry import box

X0, Y0 = 930_000.0, 6_680_000.0  # Doubs approximatif, Lambert-93
DEPT = "98"


@pytest.fixture
def dept_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(
        rasterize,
        "dept_dirs",
        lambda d: {"raw": tmp_path / "raw" / d, "grid": tmp_path / "grid" / d},
    )
    return tmp_path


def _shapefile(path: Path, polys: list[tuple], crs: str | None = CRS_CALC) -> Path:
    gdf = gpd.GeoDataFrame(
        {"CODE_TFV": [c for _, c in polys]}, geometry=[g for g, _ in polys], crs=crs
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path)
    return path


def _grid() -> Grid:
    return Grid.aligned(X0, Y0, X0 + 2000, Y0 + 2000, pad_m=0)


def _read(path: Path) -> np.ndarray:
    with rasterio.open(path) as s:
        return s.read(1)


def test_code_tfv_exact_join_strip_and_small_polygons_win(dept_dir: Path):
    grid = _grid()
    shp = _shapefile(
        dept_dir / "raw" / DEPT / "FORMATION_VEGETALE.shp",
        [
            (box(X0, Y0, X0 + 1000, Y0 + 2000), "FF1-00"),  # feuillus indéterminés, ouest
            (box(X0 + 1000, Y0, X0 + 2000, Y0 + 2000), " FF1-00-00 "),  # mélange, est, avec espaces
            (
                box(X0 + 200, Y0 + 200, X0 + 400, Y0 + 400),
                "FF2G61-61",
            ),  # petite sapinière dans l'ouest
        ],
    )
    host_p, cover_p = rasterize.host_cover(DEPT, shp, grid)
    host, cover = _read(host_p), _read(cover_p)
    assert host.shape == (80, 80)
    # lignes du haut = nord ; colonne 10 = x0 + 250 m, colonne 60 = x0 + 1 500 m
    assert host[40, 10] == host_index("feuillus_indet")
    assert host[40, 60] == host_index("melange_feuillus")  # FF1-00-00 ≠ FF1-00, espaces retirés
    assert host[70, 10] == host_index("sapin_epicea")  # y0 + 250 m : le petit polygone gagne
    assert np.all(cover[host != NODATA_U8] == COVER_CLASSES.index("fermee"))
    assert np.sum(host == NODATA_U8) == 0

    # idempotence : la sortie plus récente que l'entrée n'est pas refaite
    before = host_p.stat().st_mtime
    rasterize.host_cover(DEPT, shp, grid)
    assert host_p.stat().st_mtime == before


def test_unknown_code_is_a_blocking_error(dept_dir: Path):
    shp = _shapefile(
        dept_dir / "raw" / DEPT / "FORMATION_VEGETALE.shp",
        [(box(X0, Y0, X0 + 2000, Y0 + 2000), "FF1-09")],
    )
    with pytest.raises(RuntimeError, match=r"codes TFV absents de la table : \['FF1-09'\]"):
        rasterize.host_cover(DEPT, shp, _grid())


@pytest.mark.filterwarnings("ignore:'crs' was not provided:UserWarning")
def test_shapefile_without_prj_is_assumed_lambert93(dept_dir: Path, caplog):
    shp = _shapefile(
        dept_dir / "raw" / DEPT / "FORMATION_VEGETALE.shp",
        [(box(X0, Y0, X0 + 2000, Y0 + 2000), "FF1-09-09")],
        crs=None,
    )
    assert not shp.with_suffix(".prj").exists()
    with caplog.at_level(logging.WARNING, logger="champis_pipeline.rasterize"):
        host_p, _ = rasterize.host_cover(DEPT, shp, _grid())
    assert any("sans .prj" in r.message and "Lambert-93" in r.message for r in caplog.records)
    assert np.all(_read(host_p) == host_index("hetre"))


def test_dept_grid_is_aligned_on_the_france_origin(dept_dir: Path):
    """Grille France : origine (99 975, 6 049 975), 25 m, marge de 500 m, emboîtement sans rééchantillonnage."""
    shp = _shapefile(
        dept_dir / "raw" / DEPT / "FORMATION_VEGETALE.shp",
        [(box(X0 + 13, Y0 + 7, X0 + 1990, Y0 + 1980), "FF1-09-09")],
    )
    grid = rasterize.dept_grid(shp)
    assert (grid.xmin - 99_975) % 25 == 0 and (grid.ymin - 6_049_975) % 25 == 0
    assert grid.xmin <= X0 + 13 - 500 < grid.xmin + 25
    assert grid.xmax >= X0 + 1990 + 500 and grid.ymax >= Y0 + 1980 + 500
    assert grid.width * 25 == grid.xmax - grid.xmin


def test_ph_bilinear_keeps_nodata_as_255_never_a_mean(dept_dir: Path):
    grid = _grid()
    w, s_, e, n = rasterize.grid_bbox_4326(grid)
    cols = rows = 20
    ph = np.full((rows, cols), 55, dtype=np.int16)
    ph[:, : cols // 2] = 0  # nodata à l'ouest
    src = dept_dir / "ph.tif"
    with rasterio.open(
        src,
        "w",
        driver="GTiff",
        width=cols,
        height=rows,
        count=1,
        dtype="int16",
        crs="EPSG:4326",
        transform=from_origin(w, n, (e - w) / cols, (n - s_) / rows),
        nodata=0,
    ) as d:
        d.write(ph, 1)
    out = _read(rasterize.ph(DEPT, src, grid))
    assert out.dtype == np.uint8
    assert np.all(out[:, 60:] == 55)  # est : pH 5,5 × 10
    assert np.all(out[:, :15] == NODATA_U8)  # ouest : inconnu reste inconnu
    assert set(np.unique(out)) <= {55, NODATA_U8}  # jamais une valeur interpolée entre 0 et 55


def test_dem_products_slope_aspect_and_flat(dept_dir: Path):
    """Horn sur la mosaïque reprojetée : plan incliné vers l'est → exposition E (classe 2) ;
    plateau → `flat` (classe 8) ; altitude en uint16 m ; pente en degrés."""
    grid = _grid()
    w, s_, e, n = rasterize.grid_bbox_4326(grid)
    cols = rows = 40
    # moitié ouest : plan qui descend vers l'est (~10 m par pas de 100 m) ; moitié est : plateau à 400 m
    x = np.arange(cols, dtype=np.float32)
    dem = np.repeat((900 - 25 * x)[None, :], rows, axis=0)
    dem[:, cols // 2 :] = 400.0
    src = dept_dir / "dem.tif"
    with rasterio.open(
        src,
        "w",
        driver="GTiff",
        width=cols,
        height=rows,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(w, n, (e - w) / cols, (n - s_) / rows),
        nodata=-9999,
    ) as d:
        d.write(dem, 1)
    outs = rasterize.dem_products(DEPT, [src], grid)
    alt, slope, aspect = _read(outs["alt"]), _read(outs["slope"]), _read(outs["aspect"])
    assert alt.dtype == np.uint16 and slope.dtype == np.uint8 and aspect.dtype == np.uint8
    assert 380 <= alt[40, 70] <= 420 and alt[40, 10] > alt[40, 30] > 400
    assert np.all(alt != NODATA_U16)
    west = (slice(10, 70), slice(5, 30))
    east = (slice(10, 70), slice(50, 75))
    assert np.median(slope[west]) >= 3 and np.median(slope[east]) == 0
    assert np.median(aspect[west]) == ASPECT_CLASSES.index("E")  # 0 = N, sens horaire
    assert np.all(aspect[east] == ASPECT_CLASSES.index("flat"))  # pente < 3°
