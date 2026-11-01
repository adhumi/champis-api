"""Chaîne complète sur un mini-département synthétique (sans réseau) : grid → terrain → tiles."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from champis_pipeline import config, explain, rasterize, terrain, tiles, weather_grid
from champis_pipeline.check import report as check_report
from champis_pipeline.config import CRS_CALC, Grid
from champis_pipeline.scoring import NODATA_U8, decode_u8
from champis_pipeline.species import host_index, species_hash
from rasterio.transform import from_origin
from shapely.geometry import box

DEPT = "99"


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch):
    """Redirige data/ vers tmp et fabrique BD Forêt + SoilGrids + MNT synthétiques (10 km × 10 km)."""
    for name in ("DATA_DIR", "RAW_DIR", "GRID_DIR", "T_DIR", "TILES_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    monkeypatch.setattr(
        rasterize,
        "dept_dirs",
        lambda d: {"raw": tmp_path / "raw" / d, "grid": tmp_path / "grid" / d},
    )
    monkeypatch.setattr(terrain, "dept_dirs", rasterize.dept_dirs)
    monkeypatch.setattr(terrain, "T_DIR", tmp_path / "t")
    monkeypatch.setattr(tiles, "T_DIR", tmp_path / "t")
    monkeypatch.setattr(tiles, "TILES_DIR", tmp_path / "tiles")
    monkeypatch.setattr(tiles, "dept_dirs", rasterize.dept_dirs)
    monkeypatch.setattr(weather_grid, "T_DIR", tmp_path / "t")
    monkeypatch.setattr(weather_grid, "dept_dirs", rasterize.dept_dirs)
    monkeypatch.setattr(check_report, "T_DIR", tmp_path / "t")
    monkeypatch.setattr(explain, "dept_dirs", rasterize.dept_dirs)
    monkeypatch.setattr(check_report, "dept_dirs", rasterize.dept_dirs)

    # Doubs approximatif en Lambert-93 : x ≈ 930 000, y ≈ 6 680 000
    x0, y0 = 930_000.0, 6_680_000.0
    polys = [
        (box(x0, y0, x0 + 5000, y0 + 10000), "FF1-09-09"),  # hêtraie ouest
        (box(x0 + 5000, y0 + 5000, x0 + 10000, y0 + 10000), "FF2G61-61"),  # sapinière NE
        (box(x0 + 5000, y0, x0 + 10000, y0 + 5000), "FF2-64-64"),  # douglas SE
        (box(x0 + 2000, y0 + 2000, x0 + 2500, y0 + 2500), "FF0"),  # coupe rase dans la hêtraie
    ]
    gdf = gpd.GeoDataFrame(
        {"CODE_TFV": [c for _, c in polys]}, geometry=[g for g, _ in polys], crs=CRS_CALC
    )
    shp = tmp_path / "raw" / DEPT / "FORMATION_VEGETALE.shp"
    shp.parent.mkdir(parents=True)
    gdf.to_file(shp)

    grid = Grid.aligned(x0, y0, x0 + 10000, y0 + 10000, pad_m=0)

    # SoilGrids synthétique en 4326, 250 m : pH 5,0 à l'ouest, 7,5 à l'est
    bbox = rasterize.grid_bbox_4326(grid)
    w, s_, e, n = bbox
    cols, rows = 60, 60
    ph = np.zeros((rows, cols), dtype=np.int16)
    ph[:, : cols // 2] = 50
    ph[:, cols // 2 :] = 75
    ph_path = tmp_path / "raw" / "ph.tif"
    with rasterio.open(
        ph_path,
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

    # MNT synthétique en 4326 : pente régulière montant vers le nord, 600 → 1 200 m
    dem = np.linspace(1200, 600, rows)[:, None].repeat(cols, axis=1).astype(np.float32)
    dem_path = tmp_path / "raw" / "dem.tif"
    with rasterio.open(
        dem_path,
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

    return {"shp": shp, "grid": grid, "ph": ph_path, "dem": dem_path, "tmp": tmp_path}


def test_grid_terrain_tiles(sandbox):
    shp, grid = sandbox["shp"], sandbox["grid"]
    host_p, _cover_p = rasterize.host_cover(DEPT, shp, grid)
    rasterize.ph(DEPT, sandbox["ph"], grid)
    rasterize.dem_products(DEPT, [sandbox["dem"]], grid)

    with rasterio.open(host_p) as s:
        host = s.read(1)
    assert host.shape == (400, 400)
    assert host[200, 100] == host_index("hetre")
    assert host[100, 300] == host_index("sapin_epicea")
    assert host[300, 300] == host_index("douglas")
    assert host[310, 90] == host_index("sans_couvert")  # petit polygone gagne sur la hêtraie
    assert np.sum(host == NODATA_U8) == 0  # tout le carré est couvert

    with rasterio.open(sandbox["tmp"] / "grid" / DEPT / "aspect.tif") as s:
        aspect = s.read(1)
    # pente vers le sud (altitude décroît vers le sud) → exposition S = classe 4
    assert np.median(aspect[50:350, 50:350]) == 4

    t_path = terrain.compute(DEPT, "boletus_edulis")
    with rasterio.open(t_path) as s:
        t = decode_u8(s.read(1))
    t_hetre, t_sapin, t_douglas, t_coupe = t[200, 100], t[100, 300], t[300, 300], t[310, 90]
    assert t_hetre > 0.7
    assert t_sapin > 0.5
    assert t_douglas < t_sapin < t_hetre
    assert t_coupe < 0.1

    summ = terrain.summary(t_path)
    assert summ["forest_ha"] == pytest.approx(10_000, rel=0.01)

    pm = tiles.build("boletus_edulis", [DEPT])
    assert pm.exists() and pm.stat().st_size > 1000
    mbt = sqlite3.connect(next(pm.parent.rglob("T.mbtiles")))
    zooms = dict(
        mbt.execute("SELECT zoom_level, COUNT(*) FROM tiles GROUP BY zoom_level").fetchall()
    )
    assert set(zooms) == set(range(tiles.MIN_ZOOM, tiles.MAX_ZOOM + 1))
    assert zooms[15] > zooms[10] >= 1


def test_tile_position_and_value(sandbox):
    """La tuile XYZ contenant la hêtraie existe dans le PMTiles et porte la bonne valeur ; hors emprise : rien."""
    import io

    import mercantile
    from PIL import Image
    from pmtiles.reader import MmapSource, Reader
    from rasterio.warp import transform as rio_transform

    shp, grid = sandbox["shp"], sandbox["grid"]
    rasterize.host_cover(DEPT, shp, grid)
    rasterize.ph(DEPT, sandbox["ph"], grid)
    rasterize.dem_products(DEPT, [sandbox["dem"]], grid)
    t_path = terrain.compute(DEPT, "boletus_edulis")
    pm = tiles.build("boletus_edulis", [DEPT])

    # centre de la hêtraie en Lambert-93 → WGS84 → tuile z15
    x, y = 930_000 + 2_500, 6_680_000 + 7_500
    (lon,), (lat,) = rio_transform(CRS_CALC, "EPSG:4326", [x], [y])
    t = mercantile.tile(lon, lat, 15)
    with open(pm, "rb") as f:
        reader = Reader(MmapSource(f))
        png = reader.get(15, t.x, t.y)
        assert png is not None
        img = Image.open(io.BytesIO(png)).convert("LA")
        # pixel du centre de la tuile ≈ position de (lon, lat) dans la tuile
        b = mercantile.bounds(t)
        px = int((lon - b.west) / (b.east - b.west) * 256)
        py = int((b.north - lat) / (b.north - b.south) * 256)
        lum, alpha = img.getpixel((px, py))
        assert alpha == 255
        with rasterio.open(t_path) as s:
            expected = next(s.sample([(x, y)]))[0]
        assert abs(int(lum) - int(expected)) <= 1
        # tuile 20 km à l'ouest : hors emprise
        (lon2,), (lat2,) = rio_transform(CRS_CALC, "EPSG:4326", [x - 20_000], [y])
        t2 = mercantile.tile(lon2, lat2, 15)
        assert reader.get(15, t2.x, t2.y) is None


def test_extract_dept_subset_of_france_set(sandbox):
    """Un jeu par département sort du MBTiles de travail du jeu complet sans rien re-rendre : même
    tuiles sur l'emprise entière, sous-ensemble sur une emprise réduite, tuile de la hêtraie présente."""
    import mercantile
    from pmtiles.reader import MmapSource, Reader
    from rasterio.warp import transform as rio_transform

    shp, grid = sandbox["shp"], sandbox["grid"]
    rasterize.host_cover(DEPT, shp, grid)
    rasterize.ph(DEPT, sandbox["ph"], grid)
    rasterize.dem_products(DEPT, [sandbox["dem"]], grid)
    terrain.compute(DEPT, "boletus_edulis")
    full = tiles.build("boletus_edulis", [DEPT])
    src = tiles.work_mbtiles("T", full.stem.removeprefix("T-"))
    assert src.exists()
    total = sqlite3.connect(src).execute("SELECT COUNT(*) FROM tiles").fetchone()[0]

    bounds = tiles.dept_bounds("T", DEPT, "boletus_edulis")
    whole = full.parent / "T-extrait-tout.pmtiles"
    assert tiles.extract(src, whole, bounds, "T", "test") == total

    # emprise réduite autour de la hêtraie : moins de tuiles, mais la sienne y est à chaque niveau
    x, y = 930_000 + 2_500, 6_680_000 + 7_500
    (lon,), (lat,) = rio_transform(CRS_CALC, "EPSG:4326", [x], [y])
    small = full.parent / "T-extrait-hetraie.pmtiles"
    n = tiles.extract(src, small, (lon - 0.01, lat - 0.01, lon + 0.01, lat + 0.01), "T", "test")
    assert 0 < n < total
    with open(small, "rb") as f:
        reader = Reader(MmapSource(f))
        for z in (10, 15):
            t = mercantile.tile(lon, lat, z)
            assert reader.get(z, t.x, t.y) is not None, z
        (lon2,), (lat2,) = rio_transform(CRS_CALC, "EPSG:4326", [x - 20_000], [y])
        t2 = mercantile.tile(lon2, lat2, 15)
        assert reader.get(15, t2.x, t2.y) is None
    meta = (
        sqlite3.connect(tiles.work_mbtiles("T", "extrait-hetraie"))
        .execute("SELECT value FROM metadata WHERE name = 'bounds'")
        .fetchone()[0]
    )
    assert meta.startswith(f"{lon - 0.01}")

    # build_dept : le jeu d'un seul département est déjà le jeu complet ici (même hash) ; avec un
    # jeu complet absent, repli sur le rendu du département, qui existe déjà
    assert tiles.build_dept("boletus_edulis", DEPT, [DEPT]) == full
    assert tiles.build_dept("boletus_edulis", DEPT, ["00", DEPT]) == full


def test_dept_extract_rebuilds_stale_work_mbtiles(sandbox):
    """Un MBTiles de travail incomplet (rendu interrompu) ou absent ne donne plus un jeu vide et une
    IndexError de pmtiles : il est reconstruit depuis le PMTiles du jeu complet, qui fait foi."""
    import pytest

    shp, grid = sandbox["shp"], sandbox["grid"]
    rasterize.host_cover(DEPT, shp, grid)
    rasterize.ph(DEPT, sandbox["ph"], grid)
    rasterize.dem_products(DEPT, [sandbox["dem"]], grid)
    terrain.compute(DEPT, "boletus_edulis")
    full = tiles.build("boletus_edulis", [DEPT])
    tag = full.stem.removeprefix("T-")
    src = tiles.work_mbtiles("T", tag)
    total = sqlite3.connect(src).execute("SELECT COUNT(*) FROM tiles").fetchone()[0]

    db = sqlite3.connect(src)
    db.execute("DELETE FROM tiles")
    db.commit()
    db.close()
    assert tiles.full_set_source("T", tag) == src
    rows = sqlite3.connect(src).execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
    assert rows == total

    src.unlink()
    assert tiles.full_set_source("T", tag) == src
    bounds = tiles.dept_bounds("T", DEPT, "boletus_edulis")
    assert tiles.extract(src, full.parent / "T-reconstruit.pmtiles", bounds, "T", "test") == total

    assert tiles.full_set_source("T", "absent-d000") is None
    with pytest.raises(ValueError, match="aucune tuile"):
        tiles.extract(src, full.parent / "T-vide.pmtiles", (-60.0, -60.0, -59.9, -59.9), "T", "t")


def test_interrupted_pmtiles_never_counts_as_done(sandbox, monkeypatch):
    """Un PMTiles vide laissé par un plantage n'est pas « déjà fait » ; une conversion qui échoue ne
    laisse aucun fichier ; une reconstruction sans assez de disque le dit au lieu d'un disk I/O error."""
    import shutil
    from collections import namedtuple

    import pytest

    shp, grid = sandbox["shp"], sandbox["grid"]
    rasterize.host_cover(DEPT, shp, grid)
    rasterize.ph(DEPT, sandbox["ph"], grid)
    rasterize.dem_products(DEPT, [sandbox["dem"]], grid)
    terrain.compute(DEPT, "boletus_edulis")
    full = tiles.build("boletus_edulis", [DEPT])
    tag = full.stem.removeprefix("T-")
    src = tiles.work_mbtiles("T", tag)
    bounds = tiles.dept_bounds("T", DEPT, "boletus_edulis")

    # extraction vide : erreur, et ni le fichier ni son .part ne restent
    empty = full.parent / "T-vide.pmtiles"
    with pytest.raises(ValueError):
        tiles.extract(src, empty, (-60.0, -60.0, -59.9, -59.9), "T", "t")
    assert not empty.exists()
    assert not empty.with_name(empty.name + ".part").exists()

    # fichier de 0 octet (version antérieure, plantée en pleine écriture) : refait, pas sauté
    size = full.stat().st_size
    full.write_bytes(b"")
    assert tiles.build("boletus_edulis", [DEPT]) == full
    assert full.stat().st_size == size

    # reconstruction du MBTiles de travail sans assez de disque : message clair, rien d'écrit
    src.unlink()
    usage = namedtuple("usage", "total used free")
    with monkeypatch.context() as m:
        m.setattr(shutil, "disk_usage", lambda _: usage(0, 0, 1))
        with pytest.raises(OSError, match="espace disque insuffisant"):
            tiles.full_set_source("T", tag)
    assert not src.exists()
    assert tiles.full_set_source("T", tag) == src
    assert tiles.extract(src, full.parent / "T-ok.pmtiles", bounds, "T", "t") > 0


def test_alt_layer_matches_weather_grid_classes(sandbox):
    """La couche A porte l'indice de classe du pixel ; son centre est une classe de la cellule météo."""
    import io

    import mercantile
    from PIL import Image
    from pmtiles.reader import MmapSource, Reader
    from rasterio.warp import transform as rio_transform

    shp, grid = sandbox["shp"], sandbox["grid"]
    rasterize.host_cover(DEPT, shp, grid)
    rasterize.ph(DEPT, sandbox["ph"], grid)
    rasterize.dem_products(DEPT, [sandbox["dem"]], grid)
    terrain.compute(DEPT, "boletus_edulis")
    pm = tiles.build_alt([DEPT])
    assert pm.name.startswith("A-300m-d") and pm.exists()
    assert tiles.build_alt([DEPT]) == pm  # idempotent

    x, y = 930_000 + 2_500, 6_680_000 + 7_500  # centre de la hêtraie
    (lon,), (lat,) = rio_transform(CRS_CALC, "EPSG:4326", [x], [y])
    t = mercantile.tile(lon, lat, 15)
    with open(pm, "rb") as f:
        reader = Reader(MmapSource(f))
        img = Image.open(io.BytesIO(reader.get(15, t.x, t.y))).convert("LA")
        b = mercantile.bounds(t)
        px = int((lon - b.west) / (b.east - b.west) * 256)
        py = int((b.north - lat) / (b.north - b.south) * 256)
        k, alpha = img.getpixel((px, py))
        assert alpha == 255
        with rasterio.open(sandbox["tmp"] / "grid" / DEPT / "alt.tif") as s:
            alt_m = next(s.sample([(x, y)]))[0]
        assert k == alt_m // 300
        (lon2,), (lat2,) = rio_transform(CRS_CALC, "EPSG:4326", [x - 20_000], [y])
        t2 = mercantile.tile(lon2, lat2, 15)
        assert reader.get(15, t2.x, t2.y) is None  # hors emprise : pas de tuile

    out = weather_grid.build([DEPT], ["boletus_edulis"], out=sandbox["tmp"] / "weather_grid.json")
    cells = json.loads(out.read_text(encoding="utf-8"))["cells"]
    assert any(tiles.alt_class_center(k) in c["alt_classes"] for c in cells)


def test_weather_grid(sandbox):
    shp, grid = sandbox["shp"], sandbox["grid"]
    rasterize.host_cover(DEPT, shp, grid)
    rasterize.ph(DEPT, sandbox["ph"], grid)
    rasterize.dem_products(DEPT, [sandbox["dem"]], grid)
    terrain.compute(DEPT, "boletus_edulis")

    out = weather_grid.build([DEPT], ["boletus_edulis"], out=sandbox["tmp"] / "weather_grid.json")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["version"] == 2 and data["alt_class_m"] == 300 and data["species_hash"]
    cells = data["cells"]
    # carré de 10 km non aligné sur les 5 km → entre 4 et 9 cellules, toutes dans le Doubs
    assert 4 <= len(cells) <= 9
    for c in cells:
        assert 46.5 < c["lat"] < 47.8 and 5.5 < c["lon"] < 7.2
        assert c["alt_classes"] and all(a % 300 == 150 for a in c["alt_classes"])
        assert 600 <= c["elevation_m"] <= 1200
    # MNT 600 → 1 200 m : les classes 750, 1050 (et 450/1350 aux bords) doivent apparaître
    assert {750, 1050} <= {a for c in cells for a in c["alt_classes"]}
    # version 2 : Σ T par département, classe d'altitude et espèce, poids de l'indice par département
    for c in cells:
        assert set(c["t_sum"]) == {DEPT}
        by_alt = c["t_sum"][DEPT]
        assert set(by_alt) <= {str(a) for a in c["alt_classes"]}
        for by_sp in by_alt.values():
            assert set(by_sp) <= {"boletus_edulis"}
            # Σ T ≤ nombre de pixels de la classe dans la cellule (T ≤ 1), et > 0 (sinon omis)
            assert all(0 < v <= (5000 / 25) ** 2 for v in by_sp.values())
    total = sum(v for c in cells for by_sp in c["t_sum"][DEPT].values() for v in by_sp.values())
    with rasterio.open(
        sandbox["tmp"] / "t" / "boletus_edulis" / f"{DEPT}-{species_hash('boletus_edulis')}.tif"
    ) as src:
        t = src.read(1)
        expected = float(np.where(t == 255, 0, t / 254.0).sum())
    assert total == pytest.approx(expected, rel=1e-3)


class _FakeGbif:
    """Client HTTP factice : species/match puis une page occurrence/search (pas de réseau)."""

    def __init__(self, records: list[dict]):
        self.records = records
        self.calls: list[str] = []

    def get(self, url: str, params: dict | None = None):
        self.calls.append(url)
        if url.endswith("/species/match"):
            body = {"usageKey": 2526567, "matchType": "EXACT"}
        else:
            assert "coordinateUncertaintyInMeters" not in params  # tri côté client
            body = {"results": self.records, "endOfRecords": True}

        class R:
            def raise_for_status(self):
                pass

            def json(self, b=body):
                return b

        return R()


def _records(points_l93: list[tuple[float, float]], month: int) -> list[dict]:
    from rasterio.warp import transform as rio_transform

    xs, ys = zip(*points_l93, strict=True)
    lons, lats = rio_transform(CRS_CALC, "EPSG:4326", list(xs), list(ys))
    return [
        {
            "decimalLatitude": la,
            "decimalLongitude": lo,
            "eventDate": f"2024-{month:02d}-15T10:00",
            "coordinateUncertaintyInMeters": 30,
            "datasetKey": "ds",
            "license": "CC_BY_4_0",
        }
        for lo, la in zip(lons, lats, strict=True)
    ]


def test_gbif_check_report(sandbox, tmp_path):
    """Occurrences dans la hêtraie → AUC > 0,7, pH ≈ 5 aux occurrences, rapport écrit, cache relu."""
    from champis_pipeline.check import gbif, report

    shp, grid = sandbox["shp"], sandbox["grid"]
    rasterize.host_cover(DEPT, shp, grid)
    rasterize.ph(DEPT, sandbox["ph"], grid)
    rasterize.dem_products(DEPT, [sandbox["dem"]], grid)
    terrain.compute(DEPT, "boletus_edulis")

    rng = np.random.default_rng(1)
    hetraie = [
        (930_000 + rng.uniform(300, 4_700), 6_680_000 + rng.uniform(300, 9_700)) for _ in range(30)
    ]
    douglas = [
        (935_000 + rng.uniform(300, 4_700), 6_680_000 + rng.uniform(300, 4_700)) for _ in range(5)
    ]
    hors = [
        (900_000.0, 6_680_000.0)
    ]  # hors emprise : ignorée dans le calcul, comptée en occurrences
    records = _records(hetraie, 9) + _records(douglas, 10) + _records(hors, 9)
    records.append({"decimalLatitude": 47.1, "decimalLongitude": 6.3, "eventDate": "2024-09-01"})
    records.append({**records[0], "coordinateUncertaintyInMeters": 5000})  # imprécise
    records.append({"decimalLatitude": 47.1234, "decimalLongitude": 6.3})  # sans date
    fine = {**records[1]}
    del fine["coordinateUncertaintyInMeters"]  # incertitude inconnue, coordonnées fines : retenue
    records.append(fine)
    http = _FakeGbif(records)

    bbox = rasterize.grid_bbox_4326(grid)
    occ, rejected = gbif.occurrences(
        "boletus_edulis", "Boletus edulis", bbox, http, cache_dir=tmp_path / "gbif"
    )
    assert len(occ) == 37 and len(http.calls) == 2
    assert (rejected.undated, rejected.imprecise, rejected.rounded) == (1, 1, 1)
    assert rejected.imprecise_by_m == {"1000-5000": 1}
    assert sum(o.precision == "fine_coords" for o in occ) == 1
    assert gbif.occurrences(
        "boletus_edulis", "Boletus edulis", bbox, http, cache_dir=tmp_path / "gbif"
    ) == (occ, rejected)
    assert len(http.calls) == 2  # cache : pas de nouvel appel

    res = report.evaluate(DEPT, "boletus_edulis", occ, seed=0, rejected=rejected)
    assert res["n_occurrences"] == 37 and res["n_in_forest"] == 36
    assert res["precision"] == {"declared": 36, "fine_coords": 1}
    # Monde jouet : la hêtraie (T quasi constant) couvre la moitié du département, donc la moitié
    # du fond est à égalité avec les positives (ex æquo = 0,5) ; 0,7 est le critère sur données réelles.
    assert 0.6 < res["auc"] < 0.75 and res["t_mean_occurrences"] > res["t_mean_background"]
    assert res["months"]["9"] == 32 and res["months"]["10"] == 5
    assert res["ph_quantiles"]["10"] == pytest.approx(5.0, abs=0.01)  # hêtraie : pH 5,0
    assert res["ph_quantiles"]["99"] == pytest.approx(7.5, abs=0.01)  # douglas : pH 7,5
    assert res["surface_ha"]["forest_ha"] == pytest.approx(10_000, rel=0.01)

    path = report.write_report(res, occ, out_dir=tmp_path / "reports")
    page = path.read_text(encoding="utf-8")
    assert "AUC = " in page and "data:image/png;base64," in page
    assert json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["auc"] == res["auc"]


def test_auc_mann_whitney():
    from champis_pipeline.check.report import auc

    assert auc(np.array([0.9, 0.8]), np.array([0.1, 0.2])) == 1.0
    assert auc(np.array([0.1, 0.2]), np.array([0.9, 0.8])) == 0.0
    assert auc(np.array([0.5, 0.5]), np.array([0.5, 0.5])) == 0.5
    assert auc(np.array([0.3, 0.7]), np.array([0.5, 0.5])) == 0.5  # un au-dessus, un au-dessous
    assert np.isnan(auc(np.array([]), np.array([0.5])))


def test_explain_factors(sandbox, tmp_path):
    """Le diagnostic retrouve la composition du département synthétique et des « et si » cohérents."""
    shp, grid = sandbox["shp"], sandbox["grid"]
    rasterize.host_cover(DEPT, shp, grid)
    rasterize.ph(DEPT, sandbox["ph"], grid)
    rasterize.dem_products(DEPT, [sandbox["dem"]], grid)
    r = explain.explain(DEPT, "boletus_edulis", out_dir=tmp_path / "reports")
    assert r["forest_ha"] == pytest.approx(10_000, rel=0.01)
    hosts = {h["host"]: h for h in r["hosts"]}
    assert hosts["hetre"]["ha"] == pytest.approx(5_000 - 25, rel=0.02)  # moins la coupe rase
    assert hosts["hetre"]["weight"] == 1.0 and hosts["hetre"]["t_mean"] > 0.7
    assert hosts["douglas"]["t_mean"] < hosts["sapin_epicea"]["t_mean"]
    assert sum(h["share"] for h in r["hosts"]) == pytest.approx(1.0, abs=0.01)
    assert sum(r["t_hist"]) == sum(r["factors"]["host"]["hist"])
    wi = r["what_if"]
    assert wi["excellent_share_if_factor_is_one"]["ph"] >= r["excellent_share"]
    caps = wi["excellent_share_if_host_capped_at"]
    assert caps["0.9"] >= caps["0.8"] >= caps["0.7"]
    assert (tmp_path / "reports" / f"explain-boletus_edulis-{DEPT}.json").exists()


def test_cli_species_before_or_after_subcommand(monkeypatch):
    """`--species x terrain --dept 25` et `terrain --dept 25 --species x` sont équivalents."""
    from champis_pipeline import cli

    seen = []
    monkeypatch.setattr(cli, "cmd_terrain", lambda args: seen.append((args.species, args.force)))
    cli.main(["--species", "a", "--force", "terrain", "--dept", "25"])
    cli.main(["terrain", "--dept", "25", "--species", "a", "--force"])
    cli.main(["terrain", "--dept", "25", "--species", "a", "--species", "b"])
    cli.main(["terrain", "--dept", "25"])
    assert seen == [(["a"], True), (["a"], True), (["a", "b"], False), (None, False)]


def test_cli_tiles_and_weather_grid_accept_all(monkeypatch):
    """`tiles --depts all` et `weather-grid --depts all` développent la liste des 96 départements."""
    from champis_pipeline import cli, tiles, weather_grid
    from champis_pipeline.batch import DEPTS_ALL

    seen = []
    monkeypatch.setattr(
        tiles, "build", lambda sid, depts, force, jobs: seen.append(("T", sid, depts, jobs))
    )
    monkeypatch.setattr(
        tiles, "build_alt", lambda depts, force, jobs: seen.append(("A", depts, jobs))
    )
    monkeypatch.setattr(
        weather_grid, "build", lambda depts, species: seen.append(("W", depts, species))
    )
    assert cli.main(["tiles", "--depts", "all", "--species", "x", "--jobs", "3"]) == 0
    assert cli.main(["weather-grid", "--depts", "1", "2a"]) == 0
    assert seen == [("T", "x", DEPTS_ALL, 3), ("A", DEPTS_ALL, 3), ("W", ["01", "2A"], None)]


def test_cli_status_on_stderr_and_failure_code(capsys, monkeypatch):
    """Étapes `[i/n]` et ECHEC sur stderr, rien sur stdout ; une exception = code 1, pas de trace."""
    from champis_pipeline import cli, tiles

    monkeypatch.setattr(tiles, "build", lambda sid, depts, force, jobs: None)
    monkeypatch.setattr(tiles, "build_alt", lambda depts, force, jobs: None)
    assert cli.main(["tiles", "--depts", "25", "--species", "x"]) == 0
    out, err = capsys.readouterr()
    assert out == ""
    lines = err.splitlines()
    assert lines[0].startswith("tiles · 1 département(s) · 1 espèce(s)")
    assert lines[1] == "[1/2] tiles T x" and lines[2].split()[0] == "ok"
    assert lines[3] == "[2/2] tiles A (classes d'altitude)"
    assert lines[-1].startswith("tuiles prêtes en ") and "journal : " in lines[-1]

    def boom(sid, depts, force, jobs):
        raise RuntimeError("mosaïque impossible")

    monkeypatch.setattr(tiles, "build", boom)
    assert cli.main(["tiles", "--depts", "25", "--species", "x"]) == 1
    err = capsys.readouterr().err
    assert "ECHEC" in err and "RuntimeError: mosaïque impossible" in err
    assert "Traceback" not in err
    assert cli.main(["-v", "tiles", "--depts", "25", "--species", "x"]) == 1
    assert "Traceback" in capsys.readouterr().err


def test_batch_resumes_and_isolates_failures(tmp_path):
    """Un département en échec n'arrête pas les autres ; les « ok » ne sont pas refaits sans force."""
    from concurrent.futures import ThreadPoolExecutor

    from champis_pipeline import batch

    calls: list[str] = []

    def runner(dept, species, force):
        calls.append(dept)
        return batch.DeptResult(dept, dept != "02", "boom" if dept == "02" else "ok", 0.1, "t")

    status_path = tmp_path / "batch_status.json"
    st = batch.batch(
        ["01", "02", "03"],
        None,
        jobs=2,
        runner=runner,
        executor_factory=ThreadPoolExecutor,
        status_path=status_path,
    )
    assert sorted(calls) == ["01", "02", "03"]
    assert st["01"]["ok"] and not st["02"]["ok"] and st["03"]["ok"]
    assert json.loads(status_path.read_text(encoding="utf-8"))["02"]["message"] == "boom"
    calls.clear()
    batch.batch(
        ["01", "02", "03"],
        None,
        jobs=2,
        runner=runner,
        executor_factory=ThreadPoolExecutor,
        status_path=status_path,
    )
    assert calls == ["02"]  # reprise : seul l'échec est rejoué
    calls.clear()
    batch.batch(
        ["01"],
        None,
        jobs=1,
        force=True,
        runner=runner,
        executor_factory=ThreadPoolExecutor,
        status_path=status_path,
    )
    assert calls == ["01"]


def test_parse_depts():
    from champis_pipeline.batch import DEPTS_ALL, parse_depts

    assert parse_depts(["all"]) == DEPTS_ALL and len(DEPTS_ALL) == 96
    assert parse_depts(["1", "2a", "25"]) == ["01", "2A", "25"]
    with pytest.raises(SystemExit):
        parse_depts(["99"])


def test_tile_reductions():
    """Réduction 2×2 : T = moyenne des valides, A = premier valide ; tout nodata reste nodata."""
    from champis_pipeline.tiles import NODATA, TILE, reduce_nearest, reduce_t

    children = np.full((2 * TILE, 2 * TILE), NODATA, dtype=np.uint8)
    children[0, 0], children[0, 1], children[1, 0], children[1, 1] = 100, 200, NODATA, 50
    children[2, 2] = 7  # bloc (1, 1) : un seul pixel valide
    t = reduce_t(children)
    assert t[0, 0] == round((100 + 200 + 50) / 3) and t[1, 1] == 7 and t[5, 5] == NODATA
    a = reduce_nearest(children)
    assert a[0, 0] == 100 and a[1, 1] == 7 and a[5, 5] == NODATA
    children[0, 0] = NODATA
    assert (
        reduce_nearest(children)[0, 0] == 200
    )  # premier valide dans l'ordre (0,0),(0,1),(1,0),(1,1)


def test_tiles_have_no_seams(tmp_path, monkeypatch):
    """Les tuiles z15 voisines s'emboîtent sans bande nodata ni décalage.

    Mosaïque Lambert-93 entièrement valide (valeurs variées) : chaque tuile intérieure doit être
    pleine, et 2 × 2 tuiles rendues séparément doivent être identiques au rendu en une seule
    passe de leur emprise commune (même grille de sortie, donc mêmes pixels aux coutures).
    Régression : l'arrondi au plus proche de la fenêtre source perdait un pixel 25 m au bord.
    Transformateur exact (`tolerance=0`) des deux côtés : l'approximation par défaut de GDAL
    (0,125 px) dépend de l'emprise et ferait basculer quelques pixels isolés, hors sujet ici.
    """
    import functools

    import mercantile
    from champis_pipeline.tiles import NODATA, TILE, _render_tile
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds as transform_from_bounds
    from rasterio.warp import reproject as _reproject
    from rasterio.warp import transform_bounds

    reproject = functools.partial(_reproject, tolerance=0)
    monkeypatch.setattr(tiles, "reproject", reproject)

    n = 800  # 20 km × 20 km dans le Vaucluse, aligné sur la grille France
    g = config.Grid.aligned(880_000, 6_320_000, 880_000 + 25 * n, 6_320_000 + 25 * n, pad_m=0)
    rng = np.random.default_rng(0)
    data = rng.integers(0, 254, size=(n, n), dtype=np.uint8)
    path = tmp_path / "full.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=n,
        height=n,
        count=1,
        dtype="uint8",
        crs=CRS_CALC,
        transform=g.transform(),
        nodata=NODATA,
    ) as dst:
        dst.write(data, 1)

    with rasterio.open(path) as src:
        w, s, e, nn = transform_bounds(src.crs, "EPSG:4326", *src.bounds)
        ul, lr = mercantile.tile(w, nn, tiles.MAX_ZOOM), mercantile.tile(e, s, tiles.MAX_ZOOM)
        xs, ys = range(ul.x + 2, lr.x - 1), range(ul.y + 2, lr.y - 1)
        assert len(xs) >= 4 and len(ys) >= 4
        for x in xs:
            for y in ys:
                arr = _render_tile(src, mercantile.Tile(x, y, tiles.MAX_ZOOM))
                assert arr is not None and not np.any(arr == NODATA), (x, y)

        # 2 × 2 tuiles séparées == une passe 512 × 512 sur leur emprise commune
        x, y = xs[1], ys[1]
        stitched = np.empty((2 * TILE, 2 * TILE), dtype=np.uint8)
        for i in range(2):
            for j in range(2):
                stitched[j * TILE : (j + 1) * TILE, i * TILE : (i + 1) * TILE] = _render_tile(
                    src, mercantile.Tile(x + i, y + j, tiles.MAX_ZOOM)
                )
        bl, tr = (
            mercantile.xy_bounds(x, y + 1, tiles.MAX_ZOOM),
            mercantile.xy_bounds(x + 1, y, tiles.MAX_ZOOM),
        )
        whole = np.full((2 * TILE, 2 * TILE), NODATA, dtype=np.uint8)
        reproject(
            src.read(1),
            whole,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=transform_from_bounds(
                bl.left, bl.bottom, tr.right, tr.top, 2 * TILE, 2 * TILE
            ),
            dst_crs=config.CRS_TILES,
            resampling=Resampling.nearest,
            src_nodata=NODATA,
            dst_nodata=NODATA,
        )
        np.testing.assert_array_equal(stitched, whole)


class _FakeBucket:
    """Même contrat que champis_pipeline.storage.Bucket, en mémoire."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.uploads: list[str] = []

    def list(self, prefix):
        return {k: len(v) for k, v in self.objects.items() if k.startswith(prefix)}

    def upload(self, path, key, progress=None):
        self.objects[key] = Path(path).read_bytes()
        self.uploads.append(key)
        if progress:
            progress(len(self.objects[key]))
            self.progress = getattr(self, "progress", 0) + len(self.objects[key])

    def put_json(self, key, obj):
        self.objects[key] = json.dumps(obj).encode()

    def delete(self, keys):
        for k in keys:
            del self.objects[k]

    def download(self, key, path):
        Path(path).write_bytes(self.objects[key])

    def get_json(self, key):
        return json.loads(self.objects[key]) if key in self.objects else None

    def presign(self, key, expires_s):
        return f"https://bucket.test/{key}?X-Amz-Expires={expires_s}"


def test_publish_uploads_once_and_builds_manifest(tmp_path):
    from champis_pipeline import publish
    from champis_pipeline.storage import MANIFEST_KEY, parse_tile_name
    from champis_pipeline.tiles import _depts_hash as _h

    names = publish.expected_names(["25"], ["boletus_edulis"])
    assert names[0].startswith("T-boletus_edulis-") and names[1].startswith("A-300m-d")
    for n in names:
        (tmp_path / n).write_bytes(b"x" * 100)
    bucket = _FakeBucket()
    m = publish.publish(bucket, ["25"], ["boletus_edulis"], tiles_dir=tmp_path)
    assert sorted(bucket.uploads) == sorted("tiles/" + n for n in names)
    assert bucket.progress == 200  # barre globale : octets remontés par le callback
    assert [t["kind"] for t in m["tiles"]] == ["A", "T"]  # tri par nom
    t = next(t for t in m["tiles"] if t["kind"] == "T")
    assert (
        t["species"] == "boletus_edulis" and t["bytes"] == 100 and t["key"] == "tiles/" + names[0]
    )
    assert m["depts_by_hash"][t["depts_hash"]] == ["25"]
    assert bucket.get_json(MANIFEST_KEY)["tiles"] == m["tiles"]
    # relance : rien n'est renvoyé (même taille), le manifeste est réécrit
    bucket.uploads.clear()
    publish.publish(bucket, ["25"], ["boletus_edulis"], tiles_dir=tmp_path)
    assert bucket.uploads == []
    assert parse_tile_name("T-x-00000000-p1.0.0-dabcdef.pmtiles")["scoring_version"] == "1.0.0"
    assert parse_tile_name("random.pmtiles") is None
    with pytest.raises(FileNotFoundError):
        publish.publish(bucket, ["01"], ["boletus_edulis"], tiles_dir=tmp_path)
    # second jeu (25 + 39) : sans prune les deux coexistent, avec prune seul le nouveau reste
    names2 = publish.expected_names(["25", "39"], ["boletus_edulis"])
    for n in names2:
        (tmp_path / n).write_bytes(b"y" * 50)
    m = publish.publish(bucket, ["25", "39"], ["boletus_edulis"], tiles_dir=tmp_path)
    assert len(m["tiles"]) == 4 and len(m["depts_by_hash"]) == 2
    m = publish.publish(bucket, ["25", "39"], ["boletus_edulis"], tiles_dir=tmp_path, prune=True)
    assert sorted(t["name"] for t in m["tiles"]) == sorted(names2)
    assert list(m["depts_by_hash"].values()) == [["25", "39"]]
    assert MANIFEST_KEY in bucket.objects and not any("d" + _h(["25"]) in k for k in bucket.objects)


def test_publish_per_dept_adds_one_set_per_department(tmp_path):
    from champis_pipeline import publish
    from champis_pipeline.tiles import _depts_hash as _h

    assert publish.dept_sets(["39", "25"], per_dept=False) == [["25", "39"]]
    assert publish.dept_sets(["39", "25"], per_dept=True) == [["25", "39"], ["25"], ["39"]]
    assert publish.dept_sets(["25"], per_dept=True) == [["25"]]  # un seul : déjà son propre jeu

    depts, species = ["25", "39"], ["boletus_edulis"]
    names = {n for s in publish.dept_sets(depts, True) for n in publish.expected_names(s, species)}
    assert len(names) == 6  # (T + A) × (France, 25, 39)
    for n in names:
        (tmp_path / n).write_bytes(b"z" * 10)
    bucket = _FakeBucket()
    m = publish.publish(bucket, depts, species, tiles_dir=tmp_path, per_dept=True)
    assert sorted(bucket.uploads) == sorted("tiles/" + n for n in names)
    assert m["depts_by_hash"] == {_h(depts): ["25", "39"], _h(["25"]): ["25"], _h(["39"]): ["39"]}
    by_name = {t["name"]: t for t in m["tiles"]}
    assert len(by_name) == 6
    dept_of = {t["name"]: t.get("dept") for t in m["tiles"]}
    assert sorted(d for d in dept_of.values() if d) == ["25", "25", "39", "39"]
    france = [t for t in m["tiles"] if "dept" not in t]
    assert len(france) == 2 and {t["depts_hash"] for t in france} == {_h(depts)}
    # prune garde tous les jeux publiés par cet appel, et rien d'autre
    stale = "tiles/T-boletus_edulis-00000000-p1.0.0-dabcdef.pmtiles"
    bucket.objects[stale] = b"old"
    m = publish.publish(bucket, depts, species, tiles_dir=tmp_path, prune=True, per_dept=True)
    assert stale not in bucket.objects
    assert len(m["tiles"]) == 6 and len(m["depts_by_hash"]) == 3
    # sans per_dept et avec prune : les jeux par département disparaissent
    m = publish.publish(bucket, depts, species, tiles_dir=tmp_path, prune=True)
    assert len(m["tiles"]) == 2 and list(m["depts_by_hash"]) == [_h(depts)]


def test_s3_config_from_env_accepts_railway_generic_names(monkeypatch):
    from champis_pipeline.storage import S3Config

    for k in (
        "CHAMPIS_S3_ENDPOINT",
        "CHAMPIS_S3_BUCKET",
        "CHAMPIS_S3_REGION",
        "AWS_ENDPOINT_URL",
        "AWS_S3_BUCKET_NAME",
        "AWS_DEFAULT_REGION",
    ):
        monkeypatch.delenv(k, raising=False)
    assert S3Config.from_env() is None
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://storage.railway.app")
    monkeypatch.setenv("AWS_S3_BUCKET_NAME", "champis-tiles")
    cfg = S3Config.from_env()
    assert cfg.endpoint == "https://storage.railway.app" and cfg.bucket == "champis-tiles"
    assert cfg.region == "auto"
    monkeypatch.setenv("CHAMPIS_S3_BUCKET", "autre")  # CHAMPIS_* prime
    assert S3Config.from_env().bucket == "autre"


def test_load_env_does_not_override(tmp_path, monkeypatch):
    from champis_pipeline import config

    env = tmp_path / ".env"
    env.write_text("AWS_S3_BUCKET_NAME=depuis-dotenv\nCHAMPIS_TEST_ONLY=1\n", encoding="utf-8")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setenv("AWS_S3_BUCKET_NAME", "depuis-environnement")
    monkeypatch.delenv("CHAMPIS_TEST_ONLY", raising=False)
    assert config.load_env() is True
    import os

    assert os.environ["AWS_S3_BUCKET_NAME"] == "depuis-environnement"
    assert os.environ["CHAMPIS_TEST_ONLY"] == "1"
    monkeypatch.delenv("CHAMPIS_TEST_ONLY")
