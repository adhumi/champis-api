"""PMTiles G, un par groupe d'affichage (spec › Points ouverts › PMTiles par groupe, 4 nov. 2026) :
construits en empilant les MBTiles de travail de jeux T synthétiques, sans réseau ni rendu.

Format : PNG RGBA 256 px, canal i = T × 254 de la i-ème espèce du groupe (ordre alphabétique),
255 = sans donnée pour ce canal (pixel nodata de l'espèce, ou tuile absente de son jeu) ; pour
trois espèces, le 4e canal est tout à 255. Nom `G-<groupe>-<hash>-p<version>-d<hash départements>`.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import sqlite3
from pathlib import Path

import mercantile
import numpy as np
import pytest
import rasterio
from champis_pipeline import SCORING_VERSION, config, publish, status, tiles
from champis_pipeline.species import (
    GROUP_CHANNELS,
    group_hash,
    group_sets,
    species_groups,
    species_hash,
)
from champis_pipeline.storage import MANIFEST_KEY, TILES_PREFIX, build_manifest, parse_tile_name
from champis_pipeline.tiles import NODATA, TILE
from PIL import Image
from pmtiles.reader import MmapSource, Reader
from rasterio.transform import from_origin

CEPES = ["boletus_aereus", "boletus_edulis", "boletus_pinophilus", "boletus_reticulatus"]
GIROLLES = ["cantharellus_alborufescens", "cantharellus_cibarius", "cantharellus_pallens"]
LACTAIRES = ["lactarius_deliciosus"]
LON, LAT = 6.37, 47.19  # une hêtraie du Doubs
DEPT = "99"
BOUNDS = (LON - 0.05, LAT - 0.05, LON + 0.05, LAT + 0.05)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "TILES_DIR", tmp_path / "tiles")
    monkeypatch.setattr(config, "T_DIR", tmp_path / "t")
    monkeypatch.setattr(tiles, "TILES_DIR", tmp_path / "tiles")
    monkeypatch.setattr(tiles, "T_DIR", tmp_path / "t")
    return tmp_path


def _tile(z: int) -> mercantile.Tile:
    return mercantile.tile(LON, LAT, z)


def _full(value: int, hole: bool = False) -> np.ndarray:
    arr = np.full((TILE, TILE), value, dtype=np.uint8)
    if hole:
        arr[0, 0] = NODATA
    return arr


def _t_set(
    species_id: str,
    depts: list[str],
    content: dict[tuple[int, int, int], np.ndarray],
    bounds=BOUNDS,
) -> Path:
    """Jeu T synthétique : MBTiles de travail + PMTiles, comme `tiles.build` les laisse."""
    tag = tiles.t_tag(species_id, depts)
    mbt = tiles.work_mbtiles("T", tag)
    mbt.parent.mkdir(parents=True, exist_ok=True)
    mbt.unlink(missing_ok=True)
    db = sqlite3.connect(mbt)
    db.executescript(tiles.MBTILES_SCHEMA)
    db.executemany(
        "INSERT INTO metadata VALUES (?, ?)",
        [
            ("name", f"T {species_id}"),
            ("format", "png"),
            ("minzoom", str(tiles.MIN_ZOOM)),
            ("maxzoom", str(tiles.MAX_ZOOM)),
            ("bounds", ",".join(map(str, bounds))),
        ],
    )
    for (z, x, y), arr in content.items():
        tiles._insert(db, z, [(x, y, tiles._png(arr))])
    db.close()
    out = tiles.TILES_DIR / f"T-{tag}.pmtiles"
    out.parent.mkdir(parents=True, exist_ok=True)
    tiles._write_pmtiles(mbt, out)
    return out


def _t_tif(species_id: str, dept: str) -> Path:
    """Raster T minimal du département en WGS84, pour `dept_bounds`."""
    path = tiles.T_DIR / species_id / f"{dept}-{species_hash(species_id)}.tif"
    path.parent.mkdir(parents=True, exist_ok=True)
    w, s, e, n = BOUNDS
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="uint8",
        crs="EPSG:4326",
        transform=from_origin(w, n, (e - w) / 4, (n - s) / 4),
        nodata=NODATA,
    ) as dst:
        dst.write(np.zeros((4, 4), dtype=np.uint8), 1)
    return path


def _cepes_sets(depts: list[str]) -> None:
    """Quatre jeux T aux contenus connus : chaque cas du format est couvert par un canal."""
    along = {(z, _tile(z).x, _tile(z).y) for z in range(tiles.MIN_ZOOM, tiles.MAX_ZOOM + 1)}
    t15 = _tile(15)
    # aereus : partout, valeur 10, un pixel nodata (→ 255 dans le canal R)
    _t_set(CEPES[0], depts, {k: _full(10, hole=True) for k in along})
    # edulis : partout, valeur 100, plus une tuile voisine à z15 que les autres n'ont pas
    _t_set(CEPES[1], depts, {**{k: _full(100) for k in along}, (15, t15.x + 1, t15.y): _full(42)})
    # pinophilus : une seule tuile, à z15
    _t_set(CEPES[2], depts, {(15, t15.x, t15.y): _full(200)})
    # reticulatus : partout sauf à z15 (→ canal A à 255 sur la tuile z15, 254 ailleurs)
    _t_set(CEPES[3], depts, {k: _full(254) for k in along if k[0] != 15})


def _read(pm: Path, z: int, x: int, y: int) -> np.ndarray | None:
    with open(pm, "rb") as f:
        data = Reader(MmapSource(f)).get(z, x, y)
    if data is None:
        return None
    img = Image.open(io.BytesIO(data))
    assert img.mode == "RGBA" and img.size == (TILE, TILE)
    assert not img.info.get("interlace")
    return np.asarray(img)


def test_species_groups_and_hash_follow_the_species_files():
    """Groupes et ordre des canaux depuis `species/*.json` ; hash = sha256 des hashes concaténés."""
    assert species_groups() == {"cepes": CEPES, "girolles": GIROLLES, "lactaires": LACTAIRES}
    assert species_groups(["boletus_edulis", "cantharellus_pallens", "boletus_aereus"]) == {
        "cepes": ["boletus_aereus", "boletus_edulis"],
        "girolles": ["cantharellus_pallens"],
    }
    assert group_sets() == [
        ("cepes", 1, CEPES),
        ("girolles", 1, GIROLLES),
        ("lactaires", 1, LACTAIRES),
    ]
    expected = hashlib.sha256("".join(species_hash(s) for s in CEPES).encode()).hexdigest()[:8]
    assert group_hash(CEPES) == expected and len(expected) == 8
    assert group_hash(CEPES) != group_hash(list(reversed(CEPES)))  # l'ordre des canaux compte
    # cinq espèces : deux fichiers, le second suffixé -2 (jamais nécessaire aujourd'hui)
    with pytest.raises(ValueError, match="1 à 4"):
        tiles.build_group("cepes", [*CEPES, "boletus_zeta"], ["99"])
    assert tiles.group_tag("cepes", CEPES, ["99"], part=2).endswith(
        f"-d{tiles._depts_hash(['99'])}-2"
    )
    assert GROUP_CHANNELS == 4


def test_build_group_stacks_t_sets_channel_by_channel(sandbox, caplog):
    depts = [DEPT]
    _cepes_sets(depts)
    with caplog.at_level(logging.INFO, logger="champis_pipeline.tiles"):
        pm = tiles.build_group("cepes", CEPES, depts)
    dh = tiles._depts_hash(depts)
    assert pm.name == f"G-cepes-{group_hash(CEPES)}-p{SCORING_VERSION}-d{dh}.pmtiles"
    assert parse_tile_name(pm.name) == {
        "kind": "G",
        "group": "cepes",
        "hash": group_hash(CEPES),
        "scoring_version": SCORING_VERSION,
        "depts_hash": dh,
    }
    assert any("z15 : 2 tuiles" in r.message for r in caplog.records)

    t15 = _tile(15)
    arr = _read(pm, 15, t15.x, t15.y)
    # canal R = aereus (10, pixel nodata → 255), G = edulis (100), B = pinophilus (200),
    # A = reticulatus sans tuile à z15 → 255 : l'alpha est une donnée, pas une transparence
    assert arr[1, 1].tolist() == [10, 100, 200, 255]
    assert arr[0, 0].tolist() == [255, 100, 200, 255]
    assert np.all(arr[..., 3] == 255)
    # la tuile voisine n'existe que chez edulis : les autres canaux sont à 255
    side = _read(pm, 15, t15.x + 1, t15.y)
    assert side[7, 7].tolist() == [255, 42, 255, 255]
    assert _read(pm, 15, t15.x + 2, t15.y) is None
    # niveaux inférieurs : pris tels quels dans les T (déjà réduits), pinophilus absent → 255
    for z in (14, 10, 6):
        t = _tile(z)
        assert _read(pm, z, t.x, t.y)[5, 5].tolist() == [10, 100, 255, 254], z
    # union des (z, x, y) : 10 niveaux + la tuile voisine, ni plus ni moins
    with open(pm, "rb") as f:
        assert Reader(MmapSource(f)).header()["addressed_tiles_count"] == 11
    db = sqlite3.connect(tiles.work_mbtiles("G", pm.stem.removeprefix("G-")))
    meta = dict(db.execute("SELECT name, value FROM metadata").fetchall())
    assert meta["format"] == "png" and meta["name"] == "G cepes"
    assert (
        "R = boletus_aereus" in meta["description"]
        and "A = boletus_reticulatus" in meta["description"]
    )
    assert meta["bounds"] == ",".join(map(str, BOUNDS))
    # allers-retours PNG
    np.testing.assert_array_equal(tiles._unpng_rgba(tiles._png_rgba(arr)), arr)

    # idempotent : le fichier n'est pas réécrit ; `--force` le refait à l'identique
    mtime = pm.stat().st_mtime_ns
    assert tiles.build_group("cepes", CEPES, depts) == pm and pm.stat().st_mtime_ns == mtime
    size = pm.stat().st_size
    assert tiles.build_group("cepes", CEPES, depts, force=True) == pm
    assert pm.stat().st_size == size
    # ordre des canaux imposé par l'appelant : un autre ordre est un autre fichier
    other = tiles.build_group("cepes", list(reversed(CEPES)), depts)
    assert other != pm and _read(other, 15, t15.x, t15.y)[1, 1].tolist() == [255, 200, 100, 10]


def test_three_species_fill_the_fourth_channel_with_nodata(sandbox):
    t15 = _tile(15)
    for i, sid in enumerate(GIROLLES, 1):
        _t_set(sid, [DEPT], {(15, t15.x, t15.y): _full(i)}, bounds=(LON - i, LAT, LON, LAT + i))
    pm = tiles.build_group("girolles", GIROLLES, [DEPT], jobs=1)
    arr = _read(pm, 15, t15.x, t15.y)
    assert arr[100, 100].tolist() == [1, 2, 3, 255]
    with open(pm, "rb") as f:
        h = Reader(MmapSource(f)).header()
    # emprise = réunion des emprises des jeux T
    assert h["min_lon_e7"] == round((LON - 3) * 1e7) and h["max_lat_e7"] == round((LAT + 3) * 1e7)


def test_parallel_build_gives_the_same_bytes(sandbox):
    """`jobs > 1` : lots empilés par des processus `spawn`, même résultat que le chemin séquentiel."""
    _cepes_sets([DEPT])
    one = tiles.build_group("cepes", CEPES, [DEPT], jobs=1)
    rows1 = _rows(tiles.work_mbtiles("G", one.stem.removeprefix("G-")))
    two = tiles.build_group("cepes", CEPES, [DEPT], force=True, jobs=2)
    assert two == one and _rows(tiles.work_mbtiles("G", one.stem.removeprefix("G-"))) == rows1


def _rows(mbt: Path) -> list[tuple]:
    return sqlite3.connect(mbt).execute("SELECT * FROM tiles ORDER BY 1, 2, 3").fetchall()


def test_group_needs_the_t_sets_first(sandbox):
    _t_set(CEPES[0], [DEPT], {(15, _tile(15).x, _tile(15).y): _full(1)})
    with pytest.raises(FileNotFoundError, match="jeu T de boletus_edulis absent"):
        tiles.build_group("cepes", CEPES, [DEPT])
    assert not (tiles.TILES_DIR / f"G-{tiles.group_tag('cepes', CEPES, [DEPT])}.pmtiles").exists()


def test_group_dept_set_is_extracted_from_the_full_set(sandbox):
    """Jeu par département : extrait du MBTiles de travail du G complet (comme les T) ; sans G
    complet, construit depuis les jeux T du département."""
    for sid in CEPES:
        _t_tif(sid, DEPT)
    # sans jeu complet : repli sur les T du département seul
    _cepes_sets([DEPT])
    alone = tiles.build_group_dept("cepes", CEPES, DEPT, ["00", DEPT])
    assert alone.name == f"G-{tiles.group_tag('cepes', CEPES, [DEPT])}.pmtiles"
    assert tiles.work_mbtiles("G", alone.stem.removeprefix("G-")).exists()

    # avec un jeu complet (00 + 99) : extrait, MBTiles intermédiaire effacé
    _cepes_sets(["00", DEPT])
    full = tiles.build_group("cepes", CEPES, ["00", DEPT])
    assert full != alone
    out = tiles.build_group_dept("cepes", CEPES, DEPT, ["00", DEPT], force=True)
    assert out == alone
    t15 = _tile(15)
    assert _read(out, 15, t15.x, t15.y)[1, 1].tolist() == [10, 100, 200, 255]
    assert _read(out, 10, _tile(10).x, _tile(10).y)[1, 1].tolist() == [10, 100, 255, 254]
    assert not tiles.work_mbtiles("G", out.stem.removeprefix("G-")).exists()
    with open(out, "rb") as f:
        h = Reader(MmapSource(f)).header()
    assert h["min_lon_e7"] == round(BOUNDS[0] * 1e7)
    assert parse_tile_name(out.name)["depts_hash"] == tiles._depts_hash([DEPT])
    assert tiles.group_dept_bounds(DEPT, CEPES) == pytest.approx(BOUNDS)
    # déjà fait
    assert tiles.build_group_dept("cepes", CEPES, DEPT, ["00", DEPT]) == out


class _FakeBucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def list(self, prefix):
        return {k: len(v) for k, v in self.objects.items() if k.startswith(prefix)}

    def upload(self, path, key, progress=None):
        self.objects[key] = Path(path).read_bytes()

    def put_json(self, key, obj):
        self.objects[key] = json.dumps(obj).encode()

    def delete(self, keys):
        for k in keys:
            del self.objects[k]

    def get_json(self, key):
        return json.loads(self.objects[key]) if key in self.objects else None


def test_publish_lists_groups_with_their_channel_order(tmp_path: Path, caplog):
    depts, species = ["25", "39"], [*CEPES, *GIROLLES]
    names = publish.expected_names(depts, species, groups=True)
    g_names = [n for n in names if n.startswith("G-")]
    assert g_names == [
        f"G-{tiles.group_tag('cepes', CEPES, depts)}.pmtiles",
        f"G-{tiles.group_tag('girolles', GIROLLES, depts)}.pmtiles",
    ]
    assert publish.expected_names(depts, species) == names[: -len(g_names)]  # sans --groups
    # un groupe partiel n'a pas de G ; un groupe entier, si
    assert publish.expected_names(depts, ["boletus_edulis"], groups=True)[-1].startswith("A-")
    assert publish.partial_groups(["boletus_edulis", *GIROLLES]) == ["cepes"]
    assert publish.complete_group_sets(["boletus_edulis", *GIROLLES]) == [("girolles", 1, GIROLLES)]

    all_names = {
        n for s in publish.dept_sets(depts, True) for n in publish.expected_names(s, species, True)
    }
    for n in all_names:
        (tmp_path / n).write_bytes(b"g" * 7)
    bucket = _FakeBucket()
    with pytest.raises(FileNotFoundError, match="G-"):
        publish.plan(bucket, depts, species, tiles_dir=tmp_path / "vide", groups=True)
    m = publish.publish(bucket, depts, species, tiles_dir=tmp_path, per_dept=True, groups=True)
    g = {t["name"]: t for t in m["tiles"] if t["kind"] == "G"}
    assert len(g) == 6  # 2 groupes × (France, 25, 39)
    cepes = g[g_names[0]]
    assert cepes["group"] == "cepes" and cepes["species"] == CEPES
    assert cepes["hash"] == group_hash(CEPES) and cepes["scoring_version"] == SCORING_VERSION
    assert cepes["depts_hash"] == tiles._depts_hash(depts) and "dept" not in cepes
    dept_g = g[f"G-{tiles.group_tag('girolles', GIROLLES, ['25'])}.pmtiles"]
    assert dept_g["dept"] == "25" and dept_g["species"] == GIROLLES
    assert bucket.get_json(MANIFEST_KEY)["tiles"] == m["tiles"]

    # publier sans --groups ni --per-dept avec --prune : les G du jeu courant restent (même hash
    # de départements), ceux des jeux par département partent avec leur jeu
    m = publish.publish(bucket, depts, species, tiles_dir=tmp_path, prune=True)
    kept = [t["name"] for t in m["tiles"] if t["kind"] == "G"]
    assert kept == g_names and all(TILES_PREFIX + n in bucket.objects for n in g_names)
    # un G d'un modèle d'espèce disparu : ses espèces viennent de l'ancien manifeste ; sans
    # manifeste qui le décrive, il sort du manifeste avec un avertissement
    old = f"G-cepes-00000000-p{SCORING_VERSION}-d{tiles._depts_hash(depts)}.pmtiles"
    bucket.objects[TILES_PREFIX + old] = b"ancien"
    manifest = bucket.get_json(MANIFEST_KEY)
    manifest["tiles"].append({"kind": "G", "group": "cepes", "hash": "00000000", "species": ["x"]})
    bucket.put_json(MANIFEST_KEY, manifest)
    m = publish.publish(bucket, depts, species, tiles_dir=tmp_path)
    assert next(t for t in m["tiles"] if t["name"] == old)["species"] == ["x"]
    with caplog.at_level(logging.WARNING):
        m = build_manifest(bucket.list(TILES_PREFIX), {}, "2026-11-04T00:00:00+00:00")
    assert old not in [t["name"] for t in m["tiles"]]
    assert any(old in r.message and "inconnues" in r.message for r in caplog.records)
    assert not any(t["kind"] == "G" for t in m["tiles"])  # sans table des espèces : aucun G


def test_status_counts_groups_when_asked(sandbox):
    names = publish.expected_names(["25"], [*CEPES, *GIROLLES], groups=True)
    for n in names:
        p = config.TILES_DIR / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    assert status.dept_status("25", [*CEPES, *GIROLLES], {}).tiles_expected == 8
    with_g = status.dept_status("25", [*CEPES, *GIROLLES], {}, groups=True)
    assert (with_g.tiles, with_g.tiles_expected) == (10, 10)
    s = status.set_status(["25"], [*CEPES, *GIROLLES], groups=True)
    assert s.expected == names and s.local == names


def test_cli_tiles_groups_builds_each_complete_group(monkeypatch, capsys, tmp_path: Path):
    from champis_pipeline import cli

    seen: list[tuple] = []
    out = tmp_path / "x.pmtiles"  # ce que les doublures « écrivent » : un fichier de 3 octets
    out.write_bytes(b"xyz")
    monkeypatch.setattr(tiles, "build", lambda sid, depts, force, jobs: seen.append(("T", sid)))
    monkeypatch.setattr(tiles, "build_alt", lambda depts, force, jobs: seen.append(("A",)))
    monkeypatch.setattr(
        tiles,
        "build_group",
        lambda gid, ids, depts, part, force, jobs: seen.append(("G", gid, ids, depts, part, jobs)),
    )
    monkeypatch.setattr(
        tiles, "build_dept", lambda sid, d, depts, force, jobs: seen.append(("Td", sid, d)) or out
    )
    monkeypatch.setattr(
        tiles, "build_alt_dept", lambda d, depts, force, jobs: seen.append(("Ad", d)) or out
    )
    monkeypatch.setattr(
        tiles,
        "build_group_dept",
        lambda gid, ids, d, depts, part, force, jobs: seen.append(("Gd", gid, d, part)) or out,
    )
    sp = [a for s in (*CEPES, *GIROLLES) for a in ("--species", s)]
    assert (
        cli.main(["tiles", "--depts", "25", "39", *sp, "--groups", "--per-dept", "--jobs", "2"])
        == 0
    )
    err = capsys.readouterr().err
    assert (
        "· 2 groupe(s)" in err
        and "[9/12] tiles G cepes" in err
        and "[10/12] tiles G girolles" in err
    )
    assert ("G", "cepes", CEPES, ["25", "39"], 1, 2) in seen
    assert ("G", "girolles", GIROLLES, ["25", "39"], 1, 2) in seen
    assert [c for c in seen if c[0] == "Gd"] == [
        ("Gd", "cepes", "25", 1),
        ("Gd", "girolles", "25", 1),
        ("Gd", "cepes", "39", 1),
        ("Gd", "girolles", "39", 1),
    ]
    assert "10 fichiers · 30 o" in err  # par département : 7 T + A + 2 G

    # une espèce seule avec --groups : avertissement, pas de G
    seen.clear()
    assert cli.main(["tiles", "--depts", "25", "--species", "boletus_edulis", "--groups"]) == 0
    err = capsys.readouterr().err
    assert "groupe cepes incomplet" in err and not any(c[0] == "G" for c in seen)
    assert "[1/2] tiles T boletus_edulis" in err
