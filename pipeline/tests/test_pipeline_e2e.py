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
from champis_pipeline.species import host_index
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
    assert data["version"] == 1
    cells = data["cells"]
    # carré de 10 km non aligné sur les 5 km → entre 4 et 9 cellules, toutes dans le Doubs
    assert 4 <= len(cells) <= 9
    for c in cells:
        assert 46.5 < c["lat"] < 47.8 and 5.5 < c["lon"] < 7.2
        assert c["alt_classes"] and all(a % 300 == 150 for a in c["alt_classes"])
        assert 600 <= c["elevation_m"] <= 1200
    # MNT 600 → 1 200 m : les classes 750, 1050 (et 450/1350 aux bords) doivent apparaître
    assert {750, 1050} <= {a for c in cells for a in c["alt_classes"]}


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
    cli.main(["tiles", "--depts", "all", "--species", "x", "--jobs", "3"])
    cli.main(["weather-grid", "--depts", "1", "2a"])
    assert seen == [("T", "x", DEPTS_ALL, 3), ("A", DEPTS_ALL, 3), ("W", ["01", "2A"], None)]


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

    def get_json(self, key):
        return json.loads(self.objects[key]) if key in self.objects else None

    def presign(self, key, expires_s):
        return f"https://bucket.test/{key}?X-Amz-Expires={expires_s}"


def test_publish_uploads_once_and_builds_manifest(tmp_path):
    from champis_pipeline import publish
    from champis_pipeline.storage import MANIFEST_KEY, parse_tile_name

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
