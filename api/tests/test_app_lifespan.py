"""Démarrage réel de l'API (`lifespan`) sur des réglages de développement : sans bucket, sans APNs,
base SQLite, grille versionnée ; et les planificateurs de pré-chauffe et de notification."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from champis_api import app as app_module
from champis_api.app import create_app, next_warm, notify_scheduler, warm_scheduler
from champis_api.settings import Settings
from conftest import GRID

PARIS = ZoneInfo("Europe/Paris")


@pytest.mark.asyncio
async def test_lifespan_without_bucket_or_apns_serves_a_degraded_api(tmp_path: Path, caplog):
    """api/README : sans bucket `/tiles` répond `configured: false` ; sans APNs pas de notification ;
    base SQLite en local ; `/health` compte les cellules de la grille ; aucun réseau au démarrage."""
    grid_path = tmp_path / "weather_grid.json"
    grid_path.write_text(json.dumps(GRID), encoding="utf-8")
    settings = Settings(
        weather_grid=grid_path,
        cache_db=tmp_path / "cache" / "weather_cache.db",
        weather_seed=tmp_path / "absent.json",
        s3=None,
        database_url=f"sqlite:///{tmp_path / 'champis.db'}",
        apns_key="",
        warm_hours=(),
        warm_on_start=False,
        notify_hours=(7,),
        serve_web=True,
    )
    app = create_app(settings)
    with caplog.at_level(logging.WARNING, logger="champis_api.app"):
        async with app.router.lifespan_context(app):
            st = app.state.champis
            assert st.bucket is None and st.apns is None and st.store is not None
            assert st.store.dialect == "sqlite" and len(st.grid) == 2
            assert (tmp_path / "cache" / "weather_cache.db").exists()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://t"
            ) as c:
                h = (await c.get("/health")).json()
                assert h["cells"] == 2 and h["seeded"] == 0 and h["dept_index_at"] is None
                t = (await c.get("/tiles")).json()
                assert t["configured"] is False and t["tiles"] == []
                assert (await c.get("/", follow_redirects=False)).status_code in (302, 307)
                assert (await c.get("/shared/palette.json")).status_code == 200
                assert (await c.get("/species/boletus_edulis.json")).status_code == 200
    messages = [r.message for r in caplog.records]
    assert any("pas de bucket" in m for m in messages)
    assert any("APNs non configuré" in m for m in messages)
    assert any("pas d'amorçage" in m for m in messages)  # weather_seed absent et pas de bucket


@pytest.mark.asyncio
async def test_lifespan_without_database_turns_field_endpoints_off(tmp_path: Path):
    grid_path = tmp_path / "weather_grid.json"
    grid_path.write_text(json.dumps(GRID), encoding="utf-8")
    settings = Settings(
        weather_grid=grid_path,
        cache_db=tmp_path / "c.db",
        weather_seed=tmp_path / "absent.json",
        s3=None,
        database_url="",
        apns_key="",
        warm_hours=(),
        warm_on_start=False,
        serve_web=False,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        assert app.state.champis.store is None
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            assert (await c.get("/", follow_redirects=False)).status_code == 404
            r = await c.get(
                "/observations", headers={"X-Champis-User": "11111111-1111-1111-1111-111111111111"}
            )
            assert r.status_code == 503


@pytest.mark.asyncio
async def test_warm_scheduler_runs_on_start_then_waits_for_the_next_local_hour(monkeypatch):
    """Spec › Fraîcheur : pré-chauffe à 3 h et 15 h Paris ; `warm_on_start` pour un cache vide."""
    warmed: list[str] = []
    sleeps: list[float] = []

    async def fake_warm_once(grid, client, store=None):
        warmed.append("warm")

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(app_module, "_warm_once", fake_warm_once)
    monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)

    await warm_scheduler(object(), object(), hours=(), on_start=True)
    assert warmed == ["warm"] and sleeps == []  # sans heures : une seule passe, puis fin

    with pytest.raises(asyncio.CancelledError):
        await warm_scheduler(object(), object(), hours=(3, 15), on_start=False)
    assert warmed == ["warm", "warm"]  # une pré-chauffe après la première attente
    now = datetime.now(PARIS)
    expected = (next_warm(now, (3, 15)) - now).total_seconds()
    assert 0 < sleeps[0] <= 12 * 3600 and abs(sleeps[0] - expected) < 5


def test_next_warm_is_strictly_after_now_in_paris_time():
    at = datetime(2026, 10, 31, 15, 0, tzinfo=PARIS)
    assert next_warm(at, (3, 15)) == datetime(
        2026, 11, 1, 3, 0, tzinfo=PARIS
    )  # 15 h pile : la suivante
    assert next_warm(datetime(2026, 10, 31, 2, 59, tzinfo=PARIS), (3, 15)) == datetime(
        2026, 10, 31, 3, 0, tzinfo=PARIS
    )
    utc = datetime(2026, 10, 31, 13, 30, tzinfo=ZoneInfo("UTC"))  # 14 h 30 Paris
    assert next_warm(utc, (3, 15)) == datetime(2026, 10, 31, 15, 0, tzinfo=PARIS)


@pytest.mark.asyncio
async def test_notify_scheduler_survives_a_failing_notification(monkeypatch, caplog):
    """Spec › Mes coins : notification après la pré-chauffe du matin ; un échec est journalisé, la
    boucle continue jusqu'au lendemain."""
    calls: list[int] = []
    sleeps: list[float] = []

    async def fake_notify_all(store, grid, client, apns):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("APNs 500")

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(app_module, "notify_all", fake_notify_all)
    monkeypatch.setattr(app_module.asyncio, "sleep", fake_sleep)
    with (
        caplog.at_level(logging.ERROR, logger="champis_api.app"),
        pytest.raises(asyncio.CancelledError),
    ):
        await notify_scheduler(object(), object(), object(), object(), hours=(7,))
    assert calls == [1, 1]  # l'échec de la première n'a pas arrêté la seconde
    assert any("notification des coins en échec" in r.message for r in caplog.records)
    assert all(0 < s <= 24 * 3600 + 1 for s in sleeps)
    assert sleeps[1] >= 24 * 3600 - timedelta(hours=1).total_seconds() or sleeps[1] > 0
