"""Plusieurs workers et pré-chauffe hors de l'API (spec › Points ouverts › Tuiles hors du processus
Python, 4 nov. 2026) : instantané du cache (`VACUUM INTO`), import en une transaction, verrous de
fichier (amorçage sérialisé, un seul meneur), relais du cache publié par le bucket au démarrage."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from champis_api import app as app_module
from champis_api.app import LEADER_LOCK, SEED_LOCK, cache_refresher, create_app, lead
from champis_api.cache import WeatherCache
from champis_api.locks import file_lock, try_lock
from champis_api.settings import Settings
from champis_api.snapshot import (
    CACHE_DB_KEY,
    CACHE_META_KEY,
    META_CACHE_PUBLISHED,
    publish_cache,
    sync_cache,
)
from champis_pipeline.storage import S3Config
from conftest import GRID, NOW, TODAY, MemoryBucket, seed_from_scenario
from fixtures.open_meteo import Scenario

CELL = "c_0930_6680"
BY_ALT = {"450": {"M": 0.5, "M_eff": 0.6, "phi": 0.25}}


def _filled(path: Path, tag: str) -> WeatherCache:
    """Un cache avec une ligne dans chaque table, marquée `tag`."""
    cache = WeatherCache(path, shared=True)
    cache.put(CELL, "forecast", "2026-10-17T12", {"tag": tag}, NOW)
    day = dict.fromkeys(("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil"), 1.0)
    cache.put_history(CELL, {"2026-10-01": day}, "forecast")
    cache.put_soil_clim(CELL, {"10": [0.1] * 11})
    cache.put_dept_index([("25", "boletus_edulis", "2026-10-17", 0.5, 1.0)], TODAY, NOW)
    cache.put_cell_weather(
        [(CELL, "boletus_edulis", False, [{"date": tag, "by_alt": BY_ALT}])], TODAY, NOW
    )
    cache.meta_set("seed_generated", tag)
    return cache


def test_snapshot_is_one_self_contained_file_and_import_replaces_everything(tmp_path: Path):
    """En WAL, la base vit dans trois fichiers ; l'instantané n'en fait qu'un, cohérent. L'import
    remplace tout, y compris ce que le cache importateur avait et que l'instantané n'a pas."""
    a = _filled(tmp_path / "a.db", "A")
    assert a.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert (tmp_path / "a.db-wal").exists()
    snap = tmp_path / "a.snapshot.db"
    a.snapshot(snap)
    assert snap.exists() and not (snap.parent / "a.snapshot.db-wal").exists()
    copy = WeatherCache(snap)
    assert copy.db.execute("PRAGMA journal_mode").fetchone()[0] != "wal"
    assert copy.cell_weather([CELL], ["boletus_edulis"])[CELL, "boletus_edulis"].rows == [
        {"date": "A", "by_alt": BY_ALT}
    ]
    copy.close()

    b = _filled(tmp_path / "b.db", "B")
    b.put_soil_clim("c_only_in_b", {"10": [0.2] * 11})
    counts = b.import_cache_db(snap)
    assert counts == {
        "raw": 1,
        "history": 1,
        "soil_clim": 1,
        "dept_index": 1,
        "cell_weather": 1,
        "cell_field": 1,
        "meta": 3,  # seed_generated + les deux clés de l'indice
    }
    assert b.get(CELL, "forecast", "2026-10-17T12") == {"tag": "A"}
    assert b.meta_get("seed_generated") == "A" and b.soil_clim("c_only_in_b") is None
    assert b.cell_weather([CELL], ["boletus_edulis"])[CELL, "boletus_edulis"].rows == [
        {"date": "A", "by_alt": BY_ALT}
    ]
    assert b.db.execute("SELECT date, f FROM cell_field").fetchall() == [("A", '{"450": 0.15}')]
    # un instantané illisible : rien n'est perdu, l'erreur remonte, la base reste utilisable
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"pas une base")
    with pytest.raises(Exception):  # noqa: B017 — sqlite3.DatabaseError ou OperationalError selon la version
        b.import_cache_db(bad)
    assert b.get(CELL, "forecast", "2026-10-17T12") == {"tag": "A"}
    a.close()
    b.close()


def test_import_of_a_snapshot_without_cell_field_derives_it(tmp_path: Path, caplog):
    """Migration (spec › Points ouverts › Stockage compact par jour pour /weather/field,
    5 nov. 2026) : un instantané publié par un cron d'avant la table n'a pas `cell_field` ;
    l'import la dérive de `cell_weather` dans la même transaction, et l'API sert `/weather/field`
    comme si la passe l'avait écrite. Même chose pour une table présente mais vide."""
    days = [(TODAY + timedelta(days=k)).isoformat() for k in range(-3, 8)]
    old = _filled(tmp_path / "old.db", "old")
    old.put_cell_weather(
        [(CELL, "boletus_edulis", True, [{"date": d, "by_alt": BY_ALT} for d in days])],
        TODAY,
        NOW,
    )
    old.put_cell_weather(
        [(CELL, "cantharellus_cibarius", False, [{"date": d, "by_alt": BY_ALT} for d in days])],
        TODAY,
        NOW,
    )
    snap = tmp_path / "old.snapshot.db"
    old.snapshot(snap)
    old.close()
    legacy = sqlite3.connect(snap)
    legacy.execute("DROP TABLE cell_field")  # l'instantané tel qu'un ancien cron l'a produit
    legacy.commit()
    legacy.close()

    api = WeatherCache(tmp_path / "api.db", shared=True)
    with caplog.at_level(logging.INFO, logger="champis_api.cache"):
        counts = api.import_cache_db(snap)
    assert counts["cell_weather"] == 2 and counts["cell_field"] == 22
    assert any("sans cell_field" in r.message for r in caplog.records)
    got = api.cell_field([CELL], ["boletus_edulis", "cantharellus_cibarius"], TODAY)
    assert got[CELL].f == {"boletus_edulis": {"450": 0.15}, "cantharellus_cibarius": {"450": 0.15}}
    assert got[CELL].stale  # une des deux espèces vient d'un précalcul périmé
    assert not api.cell_field([CELL], ["cantharellus_cibarius"], TODAY)[CELL].stale
    assert api.cell_field_status().current

    # table présente mais vide (instantané d'une passe partielle d'avant la table) : pareil
    empty = sqlite3.connect(snap)
    empty.executescript(
        "CREATE TABLE cell_field (cell_id TEXT, species TEXT, date TEXT, computed_at TEXT, "
        "stale INTEGER, f TEXT, PRIMARY KEY (species, date, cell_id)) WITHOUT ROWID;"
    )
    empty.close()
    assert api.import_cache_db(snap)["cell_field"] == 22
    api.close()


def test_publish_and_sync_cache_through_the_bucket(tmp_path: Path):
    bucket = MemoryBucket()
    cron = _filled(tmp_path / "cron.db", "cron")
    at = datetime(2026, 11, 4, 2, 40, tzinfo=UTC)
    info = publish_cache(bucket, cron, tmp_path / "work", now=at)
    assert info["published"] == "2026-11-04T02:40:00+00:00"
    assert info["cells"] == 1 and info["cell_weather"] == 1 and info["today"] == TODAY.isoformat()
    assert info["cell_field"] == 1
    assert info["bytes"] == len(bucket.objects[CACHE_DB_KEY]) > 0
    assert bucket.json[CACHE_META_KEY] == info
    assert not (tmp_path / "work" / "weather_cache.publish.db").exists()
    assert cron.meta_get(META_CACHE_PUBLISHED) == info["published"]  # pas de réimport de soi-même
    assert sync_cache(cron, bucket, tmp_path / "work") is None

    api = WeatherCache(tmp_path / "api.db", shared=True)
    assert sync_cache(api, bucket, tmp_path / "work") == info
    assert api.meta_get(META_CACHE_PUBLISHED) == info["published"]
    assert api.get(CELL, "forecast", "2026-10-17T12") == {"tag": "cron"}
    assert sync_cache(api, bucket, tmp_path / "work") is None and bucket.downloads == [CACHE_DB_KEY]
    # fiche sans `published` (incomplète) : ignorée
    bucket.json[CACHE_META_KEY] = {"cells": 1}
    assert sync_cache(api, bucket, tmp_path / "work") is None
    cron.close()
    api.close()


def test_file_lock_serializes_and_try_lock_elects_one(tmp_path: Path):
    lock = tmp_path / "vol" / ".lock"
    order: list[str] = []
    inside = threading.Event()

    def holder():
        with file_lock(lock):
            inside.set()
            time.sleep(0.2)
            order.append("holder done")

    t = threading.Thread(target=holder)
    t.start()
    assert inside.wait(2)
    assert try_lock(lock) is None  # tenu par le fil : pas d'attente, pas de verrou
    with file_lock(lock):  # attend la fin du fil
        order.append("waiter in")
    t.join()
    assert order == ["holder done", "waiter in"]
    handle = try_lock(lock)
    assert handle is not None and try_lock(lock) is None
    handle.close()
    h = try_lock(lock)
    assert h is not None
    h.close()


@pytest.mark.asyncio
async def test_only_one_worker_leads_and_another_takes_over(tmp_path: Path):
    lock = tmp_path / LEADER_LOCK
    log: list[str] = []

    def background_of(name: str):
        async def background():
            log.append(f"{name} starts")
            try:
                await asyncio.sleep(3600)
            finally:
                log.append(f"{name} stops")

        return background

    first = asyncio.create_task(lead(lock, background_of("w1"), retry_s=0.01))
    await asyncio.sleep(0.05)
    second = asyncio.create_task(lead(lock, background_of("w2"), retry_s=0.01))
    await asyncio.sleep(0.1)
    assert log == ["w1 starts"]  # w2 attend
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.sleep(0.1)
    assert log == ["w1 starts", "w1 stops", "w2 starts"]  # relais pris
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert log[-1] == "w2 stops" and try_lock(lock) is not None


@pytest.mark.asyncio
async def test_cache_refresher_survives_an_unreachable_bucket(tmp_path: Path, caplog):
    class Broken(MemoryBucket):
        def get_json(self, key):
            raise TimeoutError("bucket")

    with caplog.at_level(logging.ERROR, logger="champis_api.app"):
        await cache_refresher(tmp_path / "c.db", Broken(), tmp_path, poll_minutes=0)
    assert any("import du cache publié en échec" in r.message for r in caplog.records)
    await cache_refresher(tmp_path / "c.db", None, tmp_path, poll_minutes=0)  # sans bucket : rien


@pytest.mark.asyncio
async def test_lifespan_leader_imports_the_cron_cache_and_locks_the_seed(
    tmp_path: Path, monkeypatch
):
    """Déploiement cible : pas de pré-chauffe intégrée, le worker meneur importe l'instantané
    publié par le cron dès le démarrage ; `/health` le dit ; l'amorçage passe sous verrou ; un
    second processus sur le même volume ne devient pas meneur tant que le premier vit."""
    grid_path = tmp_path / "weather_grid.json"
    grid_path.write_text(json.dumps(GRID), encoding="utf-8")
    bucket = MemoryBucket()
    cron = WeatherCache(tmp_path / "cron.db", shared=True)
    seed_from_scenario(cron, Scenario(elevation=600.0), [c["id"] for c in GRID["cells"]])
    cron.put_dept_index([("25", "boletus_edulis", TODAY.isoformat(), 0.5, 1.0)], TODAY, NOW)
    info = publish_cache(bucket, cron, tmp_path / "work")
    cron.close()
    monkeypatch.setattr(app_module, "Bucket", lambda cfg: bucket)
    vol = tmp_path / "vol"
    settings = Settings(
        weather_grid=grid_path,
        cache_db=vol / "weather_cache.db",
        weather_seed=tmp_path / "absent.json",
        s3=S3Config("https://b", "champis"),
        database_url="",
        apns_key="",
        warm_hours=(),
        warm_on_start=False,
        notify_hours=(),
        cache_poll_minutes=0,
        leader_retry_s=0.01,
        serve_web=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert (vol / SEED_LOCK).exists()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            for _ in range(100):  # l'import tourne dans un fil du meneur
                h = (await c.get("/health")).json()
                if h["cache_published"]:
                    break
                await asyncio.sleep(0.02)
            assert h["cache_published"] == info["published"]
            assert h["seeded"] == 2 and h["dept_index_at"] == info["dept_index_at"]
            assert (await c.get("/departements")).status_code == 200
        assert try_lock(vol / LEADER_LOCK) is None  # le meneur vit : pas de second meneur
    handle = try_lock(vol / LEADER_LOCK)
    assert handle is not None  # rendu à l'arrêt
    handle.close()


@pytest.mark.asyncio
async def test_lifespan_migrates_a_volume_from_before_cell_field(tmp_path: Path, caplog):
    """Premier déploiement après le 5 nov. 2026 : le volume de l'API porte `cell_weather` mais pas
    `cell_field` (ni bucket, ni amorçage à importer). Le démarrage la dérive, sous le verrou
    d'amorçage et dans un fil, et `/weather/field` répond aussitôt."""
    grid_path = tmp_path / "weather_grid.json"
    grid_path.write_text(json.dumps(GRID), encoding="utf-8")
    vol = tmp_path / "vol"
    vol.mkdir()
    today = date.today()  # le vrai client prend l'heure réelle : la passe d'hier soir, disons
    days = [(today + timedelta(days=k)).isoformat() for k in range(-3, 8)]
    old = WeatherCache(vol / "weather_cache.db", shared=True)
    old.put_cell_weather(
        [(CELL, "boletus_edulis", False, [{"date": d, "by_alt": BY_ALT} for d in days])],
        today,
        datetime.now(UTC),
    )
    old.db.execute("DROP TABLE cell_field")
    old.db.commit()
    old.close()

    settings = Settings(
        weather_grid=grid_path,
        cache_db=vol / "weather_cache.db",
        weather_seed=tmp_path / "absent.json",
        s3=None,
        database_url="",
        apns_key="",
        warm_hours=(),
        warm_on_start=False,
        notify_hours=(),
        cache_poll_minutes=0,
        leader_retry_s=0.01,
        serve_web=False,
    )
    app = create_app(settings)
    with caplog.at_level(logging.INFO, logger="champis_api.cache"):
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
        ):
            r = await c.get(
                "/weather/field",
                params={
                    "min_lon": 6.0,
                    "min_lat": 47.0,
                    "max_lon": 6.5,
                    "max_lat": 47.3,
                    "day": today.isoformat(),
                    "species": "boletus_edulis",
                },
            )
    assert r.status_code == 200, r.text
    assert [cell["id"] for cell in r.json()["cells"]] == [CELL]
    assert r.json()["cells"][0]["f"] == {"boletus_edulis": {"450": 0.15}}
    assert any("reconstruite depuis cell_weather : 11 lignes" in m.message for m in caplog.records)
    check = WeatherCache(vol / "weather_cache.db", shared=True)
    assert check.cell_field_status().current and check.cell_field_count() == 11
    check.close()
