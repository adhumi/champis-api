"""Lot BO3 (spec › Backoffice › Carte HD) : la carte de France du potentiel toute saison, rendue
depuis des PMTiles T synthétiques lus par plages dans un bucket en mémoire ; fond blanc, violet
dont l'intensité est T, limites des départements, cartouche ; nom immuable, déjà présent = rien."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pytest
from champis_api import render
from champis_api.settings import Settings
from champis_pipeline.tiles import NODATA, _png
from conftest import MemoryBucket
from PIL import Image
from pmtiles.convert import mbtiles_to_header_json
from pmtiles.tile import zxy_to_tileid
from pmtiles.writer import Writer

ZOOM = 6
KEY = "tiles/T-boletus_edulis-af34c16e-p1.0.0-dabc.pmtiles"
MANIFEST = {
    "generated": "2026-11-06T10:00:00",
    "depts_by_hash": {"dabc": ["25", "39"], "d25": ["25"]},
    "tiles": [
        {
            "kind": "T",
            "species": "boletus_edulis",
            "hash": "af34c16e",
            "depts_hash": "dabc",
            "name": KEY.split("/")[1],
            "key": KEY,
        },
        {
            "kind": "T",
            "species": "boletus_edulis",
            "hash": "af34c16e",
            "depts_hash": "d25",
            "dept": "25",
            "name": "x",
            "key": "x",
        },
        {
            "kind": "A",
            "depts_hash": "dabc",
            "name": "A-300m-dabc.pmtiles",
            "key": "tiles/A-300m-dabc.pmtiles",
        },
    ],
}


def _pmtiles(tiles: dict[tuple[int, int, int], np.ndarray]) -> bytes:
    """Un PMTiles T minimal : les tuiles données, PNG LA comme le pipeline les écrit."""
    header, metadata = mbtiles_to_header_json(
        {
            "name": "T test",
            "format": "png",
            "minzoom": str(ZOOM),
            "maxzoom": str(ZOOM),
            "bounds": "-5.4,41.2,9.8,51.2",
            "center": "2,46,6",
            "type": "overlay",
        }
    )
    path = Path("/tmp") / "champis-test.pmtiles"
    with path.open("wb") as f:
        writer = Writer(f)
        for (z, x, y), arr in sorted(tiles.items(), key=lambda kv: zxy_to_tileid(*kv[0])):
            writer.write_tile(zxy_to_tileid(z, x, y), _png(arr))
        writer.finalize(header, metadata)
    data = path.read_bytes()
    path.unlink()
    return data


@pytest.fixture
def bucket() -> MemoryBucket:
    xs, ys = render.tile_grid(ZOOM)
    full = np.full((256, 256), 200, dtype=np.uint8)  # T ≈ 0,79 partout
    half = np.full((256, 256), NODATA, dtype=np.uint8)
    half[:, :128] = 50  # T ≈ 0,2 sur la moitié gauche, rien à droite
    b = MemoryBucket()
    b.objects[KEY] = _pmtiles(
        {(ZOOM, xs.start, ys.start): full, (ZOOM, xs.start + 1, ys.start): half}
    )
    b.json["tiles/manifest.json"] = MANIFEST
    return b


def test_api_does_not_import_rasterio_and_render_matches_the_pipeline():
    """L'image de l'API n'a pas les bibliothèques de rasterio : importer `champis_api.app` ne doit
    charger ni rasterio ni `champis_pipeline.tiles` (panne du 8 nov. 2026)."""
    import subprocess
    import sys

    code = (
        "import sys, champis_api.app;"
        "bad = [m for m in ('rasterio', 'champis_pipeline.tiles', 'geopandas') if m in sys.modules];"
        "sys.exit(', '.join(bad) or 0)"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"modules lourds importés par l'API : {r.stderr.strip()}"
    from champis_pipeline import tiles

    assert render.NODATA == tiles.NODATA
    arr = np.array([[0, 127], [254, NODATA]], dtype=np.uint8)
    np.testing.assert_array_equal(render.unpng(_png(arr)), arr)


def test_keys_grid_and_entry_selection():
    assert (
        render.render_key("boletus_edulis", "af34c16e", 9)
        == "renders/T-boletus_edulis-af34c16e-z9.png"
    )
    assert render.parse_render_key("renders/T-boletus_edulis-af34c16e-z10.png") == {
        "species": "boletus_edulis",
        "hash": "af34c16e",
        "zoom": 10,
        "key": "renders/T-boletus_edulis-af34c16e-z10.png",
    }
    assert render.parse_render_key("renders/index.json") is None
    xs, ys = render.tile_grid(9)
    assert len(xs) * 256 > 5000 and len(ys) * 256 > 5000  # ≈ 5 600 × 5 600 px au niveau 9
    assert (
        render.france_entry(MANIFEST, "boletus_edulis")["key"] == KEY
    )  # le jeu, pas le département
    assert render.france_entry(MANIFEST, "cantharellus_cibarius") is None
    x, y = render.project(-5.4, 51.2, 6, *[r.start for r in render.tile_grid(6)])
    assert 0 <= x < 256 and 0 <= y < 256  # le coin nord-ouest tombe dans la première tuile


def test_render_composes_tiles_boundaries_and_cartouche(bucket: MemoryBucket, tmp_path: Path):
    xs, ys = render.tile_grid(ZOOM)
    # une « frontière » : un carré dans la première tuile
    west, north = -5.0, 51.0
    boundaries = [
        {
            "type": "Feature",
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [
                        [west, north],
                        [west + 0.5, north],
                        [west + 0.5, north - 0.5],
                        [west, north - 0.5],
                        [west, north],
                    ]
                ],
            },
        }
    ]
    seen = []
    info = render.render(
        bucket,
        "boletus_edulis",
        ZOOM,
        tmp_path,
        boundaries,
        progress=lambda d, t: seen.append((d, t)),
    )
    assert (
        not info["skipped"]
        and info["tiles"] == 2
        and info["key"] == "renders/T-boletus_edulis-af34c16e-z6.png"
    )
    assert seen[-1] == (len(xs) * len(ys), len(xs) * len(ys))
    assert info["requests"] < 3 * len(xs) * len(
        ys
    )  # les répertoires viennent du cache, pas du bucket
    img = Image.open(io.BytesIO(bucket.objects[info["key"]])).convert("RGB")
    assert img.width == len(xs) * 256 and img.height > len(ys) * 256  # plus le cartouche
    assert (info["width"], info["height"]) == img.size
    px = np.asarray(img)
    # première tuile : violet composé sur blanc (T 0,79) ; seconde : violet pâle à gauche, blanc à droite
    assert tuple(px[128, 128]) != (255, 255, 255) and px[128, 128][1] < 150
    assert tuple(px[128, 256 + 200]) == (255, 255, 255)
    pale = px[128, 256 + 60]
    assert tuple(pale) != (255, 255, 255) and pale[1] > px[128, 128][1]
    # hors des tuiles écrites : blanc ; la frontière a tracé du gris dans la première tuile
    assert tuple(px[600, 600]) == (255, 255, 255)
    assert any(
        tuple(px[y, x]) == render.BOUNDARY_RGB for y in range(0, 256) for x in range(0, 256, 4)
    )
    # cartouche : du texte sombre dans la bande du bas
    band = px[len(ys) * 256 :, :]
    assert (band.min(axis=2) < 80).any()

    # même jeu, même espèce : rien à refaire ; `force` rerend
    again = render.render(bucket, "boletus_edulis", ZOOM, tmp_path, boundaries)
    assert again["skipped"] and again["bytes"] == info["bytes"]
    assert not render.render(bucket, "boletus_edulis", ZOOM, tmp_path, boundaries, force=True)[
        "skipped"
    ]
    assert render.list_renders(bucket) == [
        {
            "species": "boletus_edulis",
            "hash": "af34c16e",
            "zoom": 6,
            "key": info["key"],
            "bytes": len(bucket.objects[info["key"]]),
        }
    ]
    with pytest.raises(LookupError, match="aucun PMTiles T publié"):
        render.render(bucket, "cantharellus_cibarius", ZOOM, tmp_path)


def test_render_cli_goes_through_the_bucket(
    bucket: MemoryBucket, tmp_path: Path, monkeypatch, capsys
):
    from champis_api import cli
    from champis_pipeline.storage import S3Config

    s = Settings(
        cache_db=tmp_path / "weather_cache.db",
        database_url=f"sqlite:///{tmp_path / 'champis.db'}",
        s3=S3Config("https://b", "champis"),
        apns_key="",
    )
    monkeypatch.setattr(cli, "Settings", lambda: s)
    monkeypatch.setattr("champis_pipeline.storage.Bucket", lambda cfg: bucket)
    monkeypatch.setattr(render, "fetch_boundaries", lambda site_url, http=None: [])
    assert cli.main(["-q", "render", "--species", "boletus_edulis", "--zoom", "9"]) == 0
    err = capsys.readouterr().err
    assert "[2/2] carte boletus_edulis" in err and "renders/T-boletus_edulis-af34c16e-z9.png" in err
    assert err.splitlines()[-1].startswith("cartes à jour")
    assert "renders/T-boletus_edulis-af34c16e-z9.png" in bucket.objects
    # inscrite dans le journal des tâches, avec sa progression en tuiles
    from champis_api.store import Store

    store = Store(s.database_url)
    (job,) = store.list_jobs()
    assert job["kind"] == "render" and job["status"] == "ok"
    assert job["log_key"].startswith("jobs/")
    store.close()
    # toutes les espèces : celles sans tuiles publiées sont sautées et comptées
    assert cli.main(["-q", "render"]) == 1
    assert "sans tuiles publiées" in capsys.readouterr().err
    assert cli.main(["-q", "render", "--species", "amanita"]) == 1
    assert "espèce inconnue" in capsys.readouterr().err
