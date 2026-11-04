"""Pas de couture entre tuiles : les bords d'une tuile z15 rendue depuis une mosaïque pleine sont
pleins, et la dernière ligne d'une tuile prolonge la première de sa voisine du dessous.

Régression de la fenêtre source couvrante (30 oct. 2026) : un arrondi au plus proche perdait la
dernière colonne ou ligne de pixels 25 m, d'où un trait transparent le long de chaque couture.
"""

from __future__ import annotations

import mercantile
import numpy as np
import pytest
import rasterio
from champis_pipeline import tiles
from champis_pipeline.config import CRS_CALC
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds


@pytest.fixture
def full_mosaic(tmp_path):
    """Raster 25 m uniforme (valeur 100) de 10 km × 10 km en Lambert-93, nodata 255."""
    n = 400
    x0, y0 = 930_000.0, 6_680_000.0
    path = tmp_path / "mosaic.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=n,
        height=n,
        count=1,
        dtype="uint8",
        crs=CRS_CALC,
        transform=from_origin(x0, y0 + n * 25, 25, 25),
        nodata=tiles.NODATA,
    ) as dst:
        dst.write(np.full((n, n), 100, np.uint8), 1)
    return path


def test_interior_tiles_have_full_edges(full_mosaic):
    with rasterio.open(full_mosaic) as src:
        w, s, e, n = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        ul, lr = mercantile.tile(w, n, tiles.MAX_ZOOM), mercantile.tile(e, s, tiles.MAX_ZOOM)
        checked = 0
        for x in range(ul.x + 2, lr.x - 1):
            for y in range(ul.y + 2, lr.y - 1):
                arr = tiles._render_tile(src, mercantile.Tile(x, y, tiles.MAX_ZOOM))
                assert arr is not None
                valid = arr != tiles.NODATA
                for edge in (valid[0], valid[-1], valid[:, 0], valid[:, -1]):
                    assert edge.all(), (x, y)
                checked += 1
        assert checked > 10
