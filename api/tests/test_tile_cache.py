"""Cache disque du relais `/tiles/{name}` (spec › Points ouverts › Tuiles hors du processus Python,
4 nov. 2026) : première lecture relayée et écrite, deuxième servie du disque avec les mêmes en-têtes,
rien de gardé pour les plages ouvertes ou trop grandes, éviction au-delà de la borne, écriture atomique,
cache désactivé à 0. Bucket factice, aucun réseau."""

from __future__ import annotations

import os
import time
from pathlib import Path

import httpx
import pytest
from champis_api import tile_cache
from champis_api.app import State, create_app
from champis_api.settings import Settings
from champis_api.tile_cache import Entry, TileCache, parse_content_range, parse_exact_range
from champis_api.weather_client import OpenMeteoClient
from conftest import NOW, make_client

NAME = "T-boletus_edulis-42a4fdc9-p1.0.0-d8c1f10.pmtiles"
BLOB = bytes(range(256)) * 64  # 16 Ko
_MANIFEST = {
    "version": 1,
    "generated": "2026-10-18T00:00:00+00:00",
    "depts_by_hash": {"8c1f10": ["25"]},
    "tiles": [{"kind": "T", "name": NAME, "key": "tiles/" + NAME, "bytes": len(BLOB)}],
}


class _FakeBucket:
    def get_json(self, key):
        return _MANIFEST if key == "tiles/manifest.json" else None

    def presign(self, key, expires_s):
        return f"https://bucket.test/{key}"

    def list(self, prefix):
        return {}


class _Upstream:
    """Le bucket : répond aux Range comme S3 (206 + content-range + etag), compte les requêtes."""

    def __init__(self, blob: bytes = BLOB):
        self.blob = blob
        self.requests: list[dict[str, str]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        assert req.url.host == "bucket.test"
        self.requests.append(dict(req.headers))
        rng = req.headers.get("range")
        if req.headers.get("if-none-match") == '"abc"':
            return httpx.Response(304, headers={"etag": '"abc"'})
        if not rng:
            return httpx.Response(200, content=self.blob, headers={"etag": '"abc"'})
        a, b = rng.removeprefix("bytes=").split("-")
        if not a:  # suffixe `bytes=-n`
            start, end = len(self.blob) - int(b), len(self.blob) - 1
        else:
            start = int(a)
            end = min(int(b), len(self.blob) - 1) if b else len(self.blob) - 1
        return httpx.Response(
            206,
            content=self.blob[start : end + 1],
            headers={
                "content-range": f"bytes {start}-{end}/{len(self.blob)}",
                "etag": '"abc"',
                "content-type": "application/octet-stream",
            },
        )


def _app(grid, scenario, upstream: _Upstream, cache: TileCache | None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    client = OpenMeteoClient(http, make_client(scenario).cache, now=lambda: NOW)
    return create_app(
        state=State(
            grid=grid,
            client=client,
            cache=client.cache,
            bucket=_FakeBucket(),
            tiles_mode="proxy",
            tile_cache=cache,
        )
    )


@pytest.fixture
def cache(tmp_path: Path) -> TileCache:
    return TileCache(tmp_path / "tiles", 2_000_000)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


# ------------------------------------------------------------------ relais + cache


@pytest.mark.asyncio
async def test_first_read_is_relayed_and_stored_second_is_served_from_disk(grid, scenario, cache):
    upstream = _Upstream()
    app = _app(grid, scenario, upstream, cache)
    async with app.router.lifespan_context(app), _client(app) as c:
        r1 = await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=0-127"})
        assert r1.status_code == 206 and r1.content == BLOB[:128]
        assert r1.headers["x-cache"] == "miss"
        assert cache.path(NAME, 0, 127).exists()
        assert not [p for p in cache.path(NAME, 0, 127).parent.iterdir() if ".tmp" in p.name]

        r2 = await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=0-127"})
        assert r2.status_code == 206 and r2.content == BLOB[:128]
        assert r2.headers["x-cache"] == "hit"
        for h in ("content-type", "content-length", "content-range", "accept-ranges", "etag"):
            assert r2.headers[h] == r1.headers[h], h
        assert r2.headers["content-range"] == f"bytes 0-127/{len(BLOB)}"
        assert r2.headers["content-length"] == "128"
        assert r2.headers["etag"] == '"abc"' and r2.headers["accept-ranges"] == "bytes"
        assert r2.headers["cache-control"] == "public, max-age=86400, immutable"
    assert len(upstream.requests) == 1

    # une autre plage du même fichier est une entrée à part
    async with app.router.lifespan_context(app), _client(app) as c:
        r = await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=128-255"})
        assert r.content == BLOB[128:256] and r.headers["x-cache"] == "miss"
    assert len(upstream.requests) == 2 and cache.stats().entries == 2


@pytest.mark.asyncio
async def test_cached_entry_answers_if_none_match_with_304(grid, scenario, cache):
    upstream = _Upstream()
    app = _app(grid, scenario, upstream, cache)
    async with app.router.lifespan_context(app), _client(app) as c:
        await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=0-127"})
        r = await c.get(
            f"/tiles/{NAME}", headers={"Range": "bytes=0-127", "If-None-Match": '"abc"'}
        )
        assert r.status_code == 304 and r.headers["etag"] == '"abc"' and r.content == b""
        r = await c.get(
            f"/tiles/{NAME}", headers={"Range": "bytes=0-127", "If-None-Match": '"autre"'}
        )
        assert r.status_code == 206 and r.headers["x-cache"] == "hit"
        # sans entrée en cache, If-None-Match part au bucket comme avant
        r = await c.get(
            f"/tiles/{NAME}", headers={"Range": "bytes=200-300", "If-None-Match": '"abc"'}
        )
        assert r.status_code == 304
    assert len(upstream.requests) == 2 and cache.stats().entries == 1


@pytest.mark.asyncio
async def test_unbounded_or_oversized_ranges_are_relayed_but_never_cached(
    grid, scenario, cache, monkeypatch
):
    upstream = _Upstream()
    app = _app(grid, scenario, upstream, cache)
    async with app.router.lifespan_context(app), _client(app) as c:
        r = await c.get(f"/tiles/{NAME}")  # fichier entier, en streaming
        assert r.status_code == 200 and r.content == BLOB and r.headers["x-cache"] == "miss"
        r = await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=100-"})  # ouverte
        assert r.status_code == 206 and r.content == BLOB[100:]
        r = await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=-100"})  # suffixe
        assert r.status_code == 206
        monkeypatch.setattr("champis_api.routers.tiles.MAX_ENTRY_BYTES", 64)
        r = await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=0-127"})  # trop grande
        assert r.status_code == 206 and r.content == BLOB[:128]
        r = await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=0-127"})
        assert r.headers["x-cache"] == "miss"
    assert len(upstream.requests) == 5
    assert not (cache.root).exists() or cache.stats().entries == 0


@pytest.mark.asyncio
async def test_range_past_end_of_file_stores_what_the_bucket_returned(grid, scenario, cache):
    """Dernière feuille : le client demande plus que le fichier, S3 répond la fin ; l'entrée rejoue
    exactement cette réponse (content-range réel, longueur réelle)."""
    upstream = _Upstream()
    app = _app(grid, scenario, upstream, cache)
    last = len(BLOB) - 1
    async with app.router.lifespan_context(app), _client(app) as c:
        r1 = await c.get(f"/tiles/{NAME}", headers={"Range": f"bytes={last - 9}-{last + 1000}"})
        r2 = await c.get(f"/tiles/{NAME}", headers={"Range": f"bytes={last - 9}-{last + 1000}"})
    assert r1.content == r2.content == BLOB[-10:] and r2.headers["x-cache"] == "hit"
    assert (
        r2.headers["content-range"]
        == r1.headers["content-range"]
        == f"bytes {last - 9}-{last}/{len(BLOB)}"
    )
    assert r2.headers["content-length"] == "10"


@pytest.mark.asyncio
async def test_cache_disabled_relays_everything(grid, scenario, tmp_path):
    settings = Settings(cache_db=tmp_path / "weather_cache.db", tile_cache_mb=0)
    assert TileCache.from_settings(settings) is None
    assert settings.tile_cache_path == tmp_path / "tiles"
    upstream = _Upstream()
    app = _app(grid, scenario, upstream, None)
    async with app.router.lifespan_context(app), _client(app) as c:
        for _ in range(2):
            r = await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=0-127"})
            assert r.status_code == 206 and r.headers["x-cache"] == "miss"
    assert len(upstream.requests) == 2 and not (tmp_path / "tiles").exists()


@pytest.mark.asyncio
async def test_bucket_errors_and_unknown_names_bypass_the_cache(grid, scenario, cache):
    def failing(req: httpx.Request) -> httpx.Response:
        return httpx.Response(403)

    app = _app(grid, scenario, failing, cache)  # type: ignore[arg-type]
    async with app.router.lifespan_context(app), _client(app) as c:
        r = await c.get(f"/tiles/{NAME}", headers={"Range": "bytes=0-127"})
        assert r.status_code == 403
        assert (
            await c.get("/tiles/nimporte.pmtiles", headers={"Range": "bytes=0-127"})
        ).status_code == 404
    assert not cache.root.exists()


# ------------------------------------------------------------------ module


def test_settings_defaults_and_env(monkeypatch, tmp_path):
    s = Settings(cache_db=Path("/data/weather_cache.db"))
    assert s.tile_cache_path == Path("/data/tiles") and s.tile_cache_mb == 2048
    s = Settings(cache_db=Path("/data/weather_cache.db"), tile_cache_dir=tmp_path / "x")
    assert s.tile_cache_path == tmp_path / "x"
    cache = TileCache.from_settings(Settings(tile_cache_dir=tmp_path / "y", tile_cache_mb=3))
    assert cache is not None and cache.root == tmp_path / "y" and cache.max_bytes == 3_000_000


def test_range_parsing():
    assert parse_exact_range("bytes=0-127") == (0, 127)
    assert parse_exact_range(" bytes=5-5 ") == (5, 5)
    for bad in (None, "", "bytes=0-", "bytes=-100", "bytes=10-5", "bytes=0-1,3-4", "octets=0-1"):
        assert parse_exact_range(bad) is None, bad
    assert parse_content_range("bytes 0-127/4096") == (0, 127, 4096)
    assert parse_content_range("bytes 0-127/*") == (0, 127, None)
    assert parse_content_range(None) is None and parse_content_range("bytes */4096") is None


def _entry(n: int, start: int = 0, etag: str | None = '"e"') -> Entry:
    return Entry(body=bytes(n), start=start, end=start + n - 1, total=10_000, etag=etag)


def test_entry_roundtrip_and_corruption(cache):
    e = Entry(body=b"abc", start=4, end=6, total=None, etag=None, content_type="x/y")
    assert tile_cache.decode(tile_cache.encode(e)) == e
    assert e.content_range == "bytes 4-6/*" and "etag" not in e.headers()
    assert e.headers()["content-length"] == "3" and e.headers()["content-type"] == "x/y"
    assert tile_cache.decode(b"pas de saut de ligne") is None
    assert tile_cache.decode(b"{}\nabc") is None  # longueur absente
    assert tile_cache.decode(tile_cache.encode(e)[:-1]) is None  # corps tronqué
    assert tile_cache.decode(b"[1]\nabc") is None


@pytest.mark.asyncio
async def test_put_is_atomic_and_rejects_truncated_bodies(cache, monkeypatch):
    seen: list[list[str]] = []
    real_replace = os.replace

    def spying_replace(src, dst):
        folder = Path(dst).parent
        seen.append(sorted(p.name for p in folder.iterdir()))  # avant le rename : que le temporaire
        assert not Path(dst).exists()
        real_replace(src, dst)

    monkeypatch.setattr(tile_cache.os, "replace", spying_replace)
    assert cache.put(NAME, 0, 127, _entry(128), 128)
    assert seen == [[f"0-127.{os.getpid()}-" + seen[0][0].split("-", 2)[2]]]
    assert seen[0][0].endswith(".tmp")
    assert sorted(p.name for p in (cache.root / NAME).iterdir()) == ["0-127"]
    got = await cache.get(NAME, 0, 127)
    assert got is not None and got.body == bytes(128) and got.etag == '"e"'
    # corps tronqué (client parti avant la fin) : rien n'est écrit
    assert not cache.put(NAME, 128, 255, _entry(100, 128), 128)
    assert await cache.get(NAME, 128, 255) is None
    # trop gros pour le cache
    assert not cache.put(NAME, 0, 9, _entry(10), tile_cache.MAX_ENTRY_BYTES + 1)
    # déjà présente (autre worker) : succès sans réécriture
    assert cache.put(NAME, 0, 127, _entry(128), 128) and len(seen) == 1


@pytest.mark.asyncio
async def test_get_reads_large_entries_off_the_event_loop_and_drops_corrupt_ones(
    cache, monkeypatch
):
    big = tile_cache.SMALL_READ_BYTES + 1
    assert cache.put(NAME, 0, big - 1, _entry(big), big)
    calls: list[str] = []
    real_to_thread = tile_cache.asyncio.to_thread

    async def spy(fn, *args):
        calls.append(fn.__name__)
        return await real_to_thread(fn, *args)

    monkeypatch.setattr(tile_cache.asyncio, "to_thread", spy)
    got = await cache.get(NAME, 0, big - 1)
    assert got is not None and len(got.body) == big and calls == ["_read"]
    assert cache.put(NAME, 0, 9, _entry(10), 10)
    assert (await cache.get(NAME, 0, 9)) is not None and calls == ["_read"]  # petit : direct
    # entrée corrompue sur le disque : traitée comme absente et supprimée
    cache.path(NAME, 0, 9).write_bytes(b'{"length": 99}\nabc')
    assert await cache.get(NAME, 0, 9) is None and not cache.path(NAME, 0, 9).exists()
    assert await cache.get("absent.pmtiles", 0, 9) is None


def test_touch_refreshes_old_entries_only(cache):
    assert cache.put(NAME, 0, 9, _entry(10), 10)
    path = cache.path(NAME, 0, 9)
    old = time.time() - 2 * tile_cache.TOUCH_INTERVAL_S
    os.utime(path, (old, old))
    assert cache._read(path) is not None
    assert path.stat().st_mtime > old + tile_cache.TOUCH_INTERVAL_S  # rafraîchi
    recent = time.time() - 10
    os.utime(path, (recent, recent))
    cache._read(path)
    assert path.stat().st_mtime == pytest.approx(recent, abs=1)  # pas touché


def test_trim_evicts_least_recently_used_beyond_the_bound(tmp_path, caplog):
    cache = TileCache(tmp_path / "tiles", 1_000)
    cache._written_since_trim = 0  # pas de nettoyage pendant la mise en place
    t0 = time.time() - 1000
    for i in range(10):  # 10 entrées de ~120 octets (en-tête compris) : ~1,2 Ko > 1 Ko
        assert cache.put(NAME, i * 100, i * 100 + 99, _entry(100, i * 100), 100)
        os.utime(cache.path(NAME, i * 100, i * 100 + 99), (t0 + i, t0 + i))
    # l'entrée 0 vient d'être relue : elle devient la plus récente
    os.utime(cache.path(NAME, 0, 99), None)
    before = cache.stats()
    assert before.entries == 10 and before.bytes > 1_000
    with caplog.at_level("INFO", logger="champis_api.tile_cache"):
        after = cache.trim()
    assert after.removed >= 1 and after.bytes <= 900 and after.entries == 10 - after.removed
    assert cache.path(NAME, 0, 99).exists()  # la plus récemment lue reste
    assert not cache.path(NAME, 100, 199).exists()  # la plus ancienne part en premier
    assert any("entrées supprimées" in m for m in caplog.messages)
    assert cache.trim().removed == 0  # sous la borne : rien à faire


def test_trim_runs_after_enough_writes_and_cleans_orphan_temporaries(tmp_path):
    cache = TileCache(tmp_path / "tiles", 1_000)
    assert cache.trim_threshold == 8_000_000
    # première écriture du processus : nettoyage immédiat (compteur à l'infini au départ)
    assert cache.put(NAME, 0, 99, _entry(100), 100) and cache._written_since_trim == 0
    orphan = cache.root / NAME / "5-6.999-abcd.tmp"
    orphan.write_bytes(b"x")
    fresh = cache.root / NAME / "7-8.999-abcd.tmp"
    fresh.write_bytes(b"x")
    old = time.time() - 2 * tile_cache.TMP_MAX_AGE_S
    os.utime(orphan, (old, old))
    stats = cache.stats()
    assert stats.entries == 1 and not orphan.exists() and fresh.exists()
    # sous le seuil, pas de nettoyage même au-delà de la borne…
    for i in range(1, 12):
        assert cache.put(NAME, i * 100, i * 100 + 99, _entry(100, i * 100), 100)
    assert cache.stats().entries == 12
    # …et dès que le seuil est atteint, le nettoyage ramène sous la borne
    cache._written_since_trim = cache.trim_threshold
    assert cache.put(NAME, 2000, 2099, _entry(100, 2000), 100)
    assert cache._written_since_trim == 0 and cache.stats().bytes <= 1_000


def test_trim_is_bounded_in_time(tmp_path, caplog):
    cache = TileCache(tmp_path / "tiles", 100)
    cache._written_since_trim = 0
    for i in range(3):
        assert cache.put(f"A-{i}m-d8c1f10.pmtiles", 0, 99, _entry(100), 100)
    with caplog.at_level("WARNING", logger="champis_api.tile_cache"):
        stats = cache.trim(budget_s=0)  # budget épuisé dès le premier dossier
    assert not stats.complete and stats.removed == 0 and cache.stats().entries == 3
    assert any("interrompu" in m for m in caplog.messages)
    assert TileCache(tmp_path / "absent", 100).stats() == tile_cache.Stats(0, 0)


def test_doctor_reports_the_cache(tmp_path, monkeypatch):
    from champis_api import doctor

    base = dict(cache_db=tmp_path / "weather_cache.db", tiles_mode="proxy")
    c = doctor._tile_cache(Settings(**base, tile_cache_mb=0))
    assert c.status == doctor.INFO and "désactivé" in c.detail
    c = doctor._tile_cache(Settings(**{**base, "tiles_mode": "presign"}))
    assert c.status == doctor.INFO and "presign" in c.detail
    c = doctor._tile_cache(Settings(**base))
    assert c.status == doctor.INFO and "absent" in c.detail
    cache = TileCache(tmp_path / "tiles", 10_000_000)
    cache.put(NAME, 0, 99, _entry(100), 100)
    c = doctor._tile_cache(Settings(**base, tile_cache_mb=10))
    assert c.status == doctor.OK and "1 plages" in c.detail and "sur 10 Mo" in c.detail
