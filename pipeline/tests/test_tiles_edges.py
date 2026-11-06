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


def _pmtiles_with_metadata(tmp_path, name: str, metadata: dict[str, str]):
    """PMTiles d'une tuile, avec les métadonnées données (comme `_mbtiles` les écrit)."""
    import sqlite3

    mbt = tmp_path / f"{name}.mbtiles"
    db = sqlite3.connect(mbt)
    db.executescript(tiles.MBTILES_SCHEMA)
    db.executemany(
        "INSERT INTO metadata VALUES (?, ?)",
        [
            ("name", name),
            ("format", "png"),
            ("minzoom", "6"),
            ("maxzoom", "6"),
            ("bounds", "5,46,6,47"),
            *metadata.items(),
        ],
    )
    tiles._insert(db, 6, [(32, 22, tiles._png(np.full((256, 256), 100, np.uint8)))])
    db.close()
    out = tmp_path / f"{name}.pmtiles"
    tiles._write_pmtiles(mbt, out)
    return out


def test_render_version_is_in_the_set_hash_and_the_metadata(tmp_path, monkeypatch):
    """Un correctif du rendu (RENDER_VERSION) change l'empreinte `d<…>`, donc le nom du jeu : rien
    (relais, navigateurs, app, `_done`) ne peut servir d'anciens octets sous le nouveau nom. Et la
    version est écrite dans les métadonnées : un PMTiles local d'une version antérieure, ou sans
    clé (rendus d'avant le 30 oct. 2026), n'est pas « déjà fait »."""
    before = tiles._depts_hash(["25", "39"])
    assert before == tiles._depts_hash(["39", "25"]) and len(before) == 6
    monkeypatch.setattr(tiles, "RENDER_VERSION", tiles.RENDER_VERSION + 1)
    assert tiles._depts_hash(["25", "39"]) != before
    monkeypatch.undo()

    current = _pmtiles_with_metadata(tmp_path, "cur", {tiles.RENDER_KEY: str(tiles.RENDER_VERSION)})
    older = _pmtiles_with_metadata(tmp_path, "old", {tiles.RENDER_KEY: "1"})
    legacy = _pmtiles_with_metadata(tmp_path, "legacy", {})
    assert tiles.render_version(current) == tiles.RENDER_VERSION
    assert tiles.render_version(older) == 1 and tiles.render_version(legacy) == 1
    assert tiles._done(current, force=False)
    assert not tiles._done(older, force=False) and not older.exists()
    assert not tiles._done(legacy, force=False) and not legacy.exists()
    assert not tiles._done(current, force=True) and current.exists()


def test_built_sets_carry_the_render_version(full_mosaic, tmp_path):
    """`_mbtiles` (T et A) écrit `champis_render` ; un extrait par département l'hérite."""
    import sqlite3

    mbt = tmp_path / "T.mbtiles"
    tiles._mbtiles(full_mosaic, mbt, "T test", "desc", "T", jobs=1)
    meta = dict(sqlite3.connect(mbt).execute("SELECT name, value FROM metadata").fetchall())
    assert meta[tiles.RENDER_KEY] == str(tiles.RENDER_VERSION)
    out = tmp_path / "T-x-00000000-p1.0.0-d000000.pmtiles"
    tiles._write_pmtiles(mbt, out)
    assert tiles.render_version(out) == tiles.RENDER_VERSION and tiles._done(out, force=False)
    with rasterio.open(full_mosaic) as src:
        bounds = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    part = tmp_path / "T-x-00000000-p1.0.0-d111111.pmtiles"
    assert tiles.extract(mbt, part, bounds, "T x 25", "desc") > 0
    assert tiles.render_version(part) == tiles.RENDER_VERSION
