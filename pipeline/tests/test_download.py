"""Étape « download » sans réseau : idempotence, sommes de contrôle, fichiers `.part` par processus,
WCS en erreur XML, dalles DEM absentes (spec › Pipeline : « cache local et sommes de contrôle »)."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from champis_pipeline import download


class FakeHTTP:
    """Remplace `httpx.stream` : une réponse par URL, et le compte des appels."""

    def __init__(self):
        self.responses: dict[str, tuple[int, bytes]] = {}
        self.calls: list[str] = []

    def add(self, url: str, body: bytes, status: int = 200) -> None:
        self.responses[url] = (status, body)

    @contextmanager
    def stream(self, method: str, url: str, **kwargs):
        self.calls.append(url)
        status, body = self.responses.get(url, (404, b"not found"))
        response = httpx.Response(
            status,
            content=body,
            headers={"content-length": str(len(body))},
            request=httpx.Request(method, url),
        )
        yield response


@pytest.fixture
def http(monkeypatch, tmp_path: Path) -> FakeHTTP:
    fake = FakeHTTP()
    monkeypatch.setattr(download.httpx, "stream", fake.stream)
    monkeypatch.setattr(download, "RAW_DIR", tmp_path / "raw")
    return fake


def test_download_is_idempotent_and_checks_md5(http: FakeHTTP, tmp_path: Path):
    body = b"x" * 3000
    md5 = hashlib.md5(body).hexdigest()
    url = "https://example.test/file.bin"
    http.add(url, body)
    dest = tmp_path / "raw" / "file.bin"

    assert download._download(url, dest, md5) == dest
    assert dest.read_bytes() == body and http.calls == [url]
    assert not list(dest.parent.glob("*.part"))  # le `.part` par pid est renommé à la fin

    # présent et vérifié : pas de nouvel appel
    download._download(url, dest, md5)
    assert http.calls == [url]
    # présent, md5 inconnu : on fait confiance au fichier
    download._download(url, dest)
    assert http.calls == [url]

    # md5 différent : retéléchargement
    dest.write_bytes(b"corrompu")
    download._download(url, dest, md5)
    assert http.calls == [url, url] and dest.read_bytes() == body

    # le serveur renvoie autre chose que prévu : erreur, le fichier n'est pas gardé comme bon
    http.add(url, b"autre")
    dest.unlink()
    with pytest.raises(RuntimeError, match="md5 incorrect"):
        download._download(url, dest, md5)


def test_download_raises_on_http_error(http: FakeHTTP, tmp_path: Path):
    url = "https://example.test/missing.bin"
    http.add(url, b"", status=503)
    with pytest.raises(httpx.HTTPStatusError):
        download._download(url, tmp_path / "raw" / "missing.bin")
    assert not (tmp_path / "raw" / "missing.bin").exists()


def test_soilgrids_rejects_an_xml_error_served_as_200(http: FakeHTTP, tmp_path: Path, monkeypatch):
    """Skill geo-pipeline : un WCS renvoie des erreurs XML en HTTP 200 ; jamais mises en cache."""
    xml = b'<?xml version="1.0"?><ows:ExceptionReport>trop grand</ows:ExceptionReport>'
    monkeypatch.setattr(
        download.httpx, "stream", _any_url(http, xml)
    )  # l'URL WCS exacte importe peu
    with pytest.raises(RuntimeError, match="téléchargement invalide"):
        download.soilgrids_ph((5.7, 46.5, 7.1, 47.6))
    assert (
        len(http.calls) == 1
        and "maps.isric.org" in http.calls[0]
        and "phh2o_0-5cm_mean" in http.calls[0]
    )
    assert not list((tmp_path / "raw" / "soilgrids").glob("*.tif"))  # rien en cache


def test_soilgrids_cached_file_is_reused(http: FakeHTTP, tmp_path: Path):
    bbox = (5.7, 46.5, 7.1, 47.6)
    dest = tmp_path / "raw" / "soilgrids" / "phh2o_0-5cm_5.70_46.50_7.10_47.60.tif"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"deja la")
    assert download.soilgrids_ph(bbox) == dest
    assert http.calls == []


def test_copernicus_skips_sea_tiles_but_needs_at_least_one(
    http: FakeHTTP, tmp_path: Path, monkeypatch
):
    """Dalles 1° : une 404 (mer) est ignorée ; aucune dalle du tout est une erreur."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    # une vraie petite dalle GeoTIFF pour N47 E006, rien pour N46 E006 (404)
    src = tmp_path / "dalle.tif"
    with rasterio.open(
        src,
        "w",
        driver="GTiff",
        width=4,
        height=4,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=from_origin(6, 48, 0.25, 0.25),
        nodata=-9999,
    ) as d:
        d.write(np.full((4, 4), 500.0, dtype=np.float32), 1)
    name = "Copernicus_DSM_COG_10_N47_00_E006_00_DEM"
    http.add(f"https://copernicus-dem-30m.s3.amazonaws.com/{name}/{name}.tif", src.read_bytes())

    out = download.copernicus_dem_tiles((6.2, 46.4, 6.8, 47.6))  # lat 46 et 47, lon 6
    assert [p.name for p in out] == [f"{name}.tif"]
    assert len(http.calls) == 2  # N46 demandée (404, ignorée) et N47

    with pytest.raises(RuntimeError, match="aucune dalle"):
        download.copernicus_dem_tiles((20.2, 10.4, 20.8, 10.6))


def test_bdforet_extracts_the_shapefile_once(http: FakeHTTP, tmp_path: Path, monkeypatch):
    """Archive 7z du flux Géoplateforme → `FORMATION_VEGETALE.shp` extrait une fois, puis réutilisé."""
    import io

    import py7zr
    from champis_pipeline.config import bdforet_url

    buf = io.BytesIO()
    with py7zr.SevenZipFile(buf, "w") as z:
        z.writestr(b"shp", "BDFORET/1_DONNEES/FORMATION_VEGETALE.shp")
        z.writestr(b"dbf", "BDFORET/1_DONNEES/FORMATION_VEGETALE.dbf")
    url = bdforet_url("25")
    assert url.endswith("BDFORET_2-0__SHP_LAMB93_D025_2016-02-16.7z")  # édition de la spec (Doubs)
    http.add(url, buf.getvalue())

    shp = download.bdforet("25")
    assert shp.name == "FORMATION_VEGETALE.shp" and shp.read_bytes() == b"shp"
    assert shp.is_relative_to(tmp_path / "raw" / "25" / "bdforet" / "extracted")
    assert download.bdforet("25") == shp and http.calls == [url]


def _any_url(http: FakeHTTP, body: bytes):
    @contextmanager
    def stream(method: str, url: str, **kwargs):
        http.calls.append(url)
        yield httpx.Response(200, content=body, request=httpx.Request(method, url))

    return stream
