"""Lot BO3 (spec › Backoffice › Tâches) : le catalogue d'outils aux options bornées, le service
d'exécution qui sert la file une tâche à la fois (prise, battement, annulation, sortes
exclusives), et les routes du backoffice qui mettent en file, annulent et listent les cartes."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from champis_api import tools, worker
from champis_api.app import State, create_app
from champis_api.settings import Settings
from champis_api.store import Store
from conftest import MemoryBucket

KEY = "une-cle-de-test-suffisamment-longue-0123456789"
ACTION = {"X-Champis-Admin": "1", "Authorization": f"Bearer {KEY}"}
BEARER = {"Authorization": f"Bearer {KEY}"}
NOW = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)


# ------------------------------------------------------------------ catalogue


def test_tools_validate_and_build_commands():
    assert tools.validate("warm", {}) == {"pull": "True", "publish": "True", "interval": "2"}
    assert tools.validate("warm", {"pull": False, "interval": "0.5"}) == {
        "pull": "False",
        "publish": "True",
        "interval": "0.5",
    }
    argv = tools.command("warm", {"pull": "oui", "publish": "0", "interval": 3})
    assert argv[:5] == [sys.executable, "-m", "champis_api.cli", "-q", "warm"]
    assert argv[5:] == ["--pull", "--interval", "3"]
    assert tools.command("publish-cache", None)[3:] == ["-q", "publish-cache"]
    assert tools.command("soil-clim", {})[2] == "champis_pipeline.cli"
    assert tools.command("render", {"species": "boletus_edulis", "zoom": "10", "force": True})[
        5:
    ] == [
        "--species",
        "boletus_edulis",
        "--zoom",
        "10",
        "--force",
    ]
    assert tools.validate("render", {})["species"] == "all"
    for kind, args, message in (
        ("nope", {}, "outil inconnu"),
        ("warm", {"rm": "-rf"}, "options refusées : rm"),
        ("warm", {"interval": "vite"}, "nombre attendu"),
        ("warm", {"interval": 99}, "au plus 10"),
        ("seed", {"interval": -1}, "au moins 0"),
        ("render", {"species": "amanita_phalloides"}, "valeur inconnue"),
        ("render", {"zoom": "3"}, "valeur inconnue"),
    ):
        with pytest.raises(tools.ToolError, match=message):
            tools.validate(kind, args)
    described = {t["kind"]: t for t in tools.describe()}
    assert set(described) == set(tools.TOOLS)
    species_field = next(f for f in described["render"]["fields"] if f["name"] == "species")
    assert species_field["choices"][0] == "all" and "boletus_edulis" in species_field["choices"]
    assert described["warm"]["exclusive"] and not described["render"]["exclusive"]


# ------------------------------------------------------------------ worker


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        cache_db=tmp_path / "data" / "weather_cache.db",
        database_url=f"sqlite:///{tmp_path / 'champis.db'}",
        s3=None,
        apns_key="",
    )


def _fake_command(script: str):
    def command(kind: str, args: dict) -> list[str]:
        return [sys.executable, "-c", script]

    return command


def test_worker_takes_the_oldest_queued_job_runs_it_and_beats(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.database_url)
    first = store.create_job("render", {"species": "all"}, "admin", NOW)
    store.create_job("render", {"species": "all"}, "admin", NOW + timedelta(seconds=1))
    # la commande factice : s'inscrit comme une vraie commande le ferait, puis sort en 0
    script = (
        "import os, sys; from champis_api.store import Store;"
        "from datetime import datetime, UTC;"
        "s = Store(os.environ['CHAMPIS_DATABASE_URL']); jid = os.environ['CHAMPIS_JOB_ID'];"
        "s.add_event(jid, 'step', '[1/1] rendu', datetime.now(UTC));"
        "s.finish_job(jid, 'ok', 0, datetime.now(UTC), 'cartes à jour'); s.close()"
    )
    w = worker.Worker(
        settings,
        store,
        name="w-test",
        command=_fake_command(script),
        poll_s=0.01,
        sleep=lambda s: None,
    )
    import os

    os.environ["CHAMPIS_DATABASE_URL"] = settings.database_url
    try:
        assert w.run(once=True) == 0
    finally:
        os.environ.pop("CHAMPIS_DATABASE_URL", None)
    job = store.job(first)
    assert job["status"] == "ok" and job["worker"] == "w-test" and job["summary"] == "cartes à jour"
    assert [e["level"] for e in store.events_of(first)][:2] == ["info", "step"]
    (other,) = [j for j in store.list_jobs() if j["id"] != first]
    assert other["status"] == "queued"  # une seule à la fois
    (beat,) = store.list_workers()
    assert beat["name"] == "w-test" and beat["current_job"] is None and beat["disk_total_mb"]
    store.close()


def test_worker_closes_a_job_whose_command_did_not(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.database_url)
    jid = store.create_job("publish-cache", {}, "admin", NOW)
    w = worker.Worker(
        settings, store, command=_fake_command("import sys; sys.exit(3)"), sleep=lambda s: None
    )
    assert w.run(once=True) == 3
    job = store.job(jid)
    assert job["status"] == "failed" and job["exit_code"] == 3 and job["finished_at"]
    store.close()


def test_worker_cancels_a_running_job_and_a_queued_one(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.database_url)
    queued = store.create_job("render", {}, "admin", NOW)
    store.request_cancel(queued, NOW)
    running = store.create_job("render", {}, "admin", NOW + timedelta(seconds=1))
    # la commande ignore SIGTERM : c'est SIGKILL, après le délai de grâce, qui l'arrête
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    polls = {"n": 0}

    def sleep(_):
        polls["n"] += 1
        if polls["n"] == 2:
            store.request_cancel(running, datetime.now(UTC))

    w = worker.Worker(settings, store, command=_fake_command(script), grace_s=0.5, sleep=sleep)
    code = w.run(once=True)
    assert code != 0
    assert store.job(queued)["status"] == "cancelled"
    job = store.job(running)
    assert job["status"] == "cancelled" and job["cancel_requested"]
    assert any("annulation demandée" in e["text"] for e in store.events_of(running))
    store.close()


def test_worker_waits_while_an_exclusive_kind_runs_elsewhere(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.database_url)
    cron = store.create_job("warm", {}, "cron", NOW, status="running")  # le cron Railway
    mine = store.create_job("warm", {}, "admin", NOW + timedelta(seconds=1))
    other = store.create_job("render", {}, "admin", NOW + timedelta(seconds=2))
    w = worker.Worker(settings, store, command=_fake_command("pass"), sleep=lambda s: None)
    assert w.next_job()["id"] == other  # la pré-chauffe attend, le rendu passe devant
    store.finish_job(cron, "ok", 0, NOW + timedelta(minutes=40))
    assert w.next_job()["id"] == mine
    bad = store.create_job("nope", {}, "admin", NOW - timedelta(hours=1))
    assert w.next_job()["id"] == mine  # l'outil inconnu est marqué en échec au passage
    assert store.job(bad)["status"] == "failed"
    store.close()


def test_worker_main_requires_a_database(tmp_path: Path):
    with pytest.raises(SystemExit, match="DATABASE_URL"):
        worker.main(Settings(database_url="", s3=None, apns_key=""), once=True)


# ------------------------------------------------------------------ routes du backoffice


def _app(grid, client, store, bucket=None):
    s = Settings(admin_key=KEY, s3=None, apns_key="")
    return create_app(
        settings=s,
        state=State(grid=grid, client=client, cache=client.cache, store=store, bucket=bucket),
    )


@pytest.mark.asyncio
async def test_backoffice_queues_refuses_and_cancels(grid, client, store):
    store.worker_heartbeat("champis-jobs", datetime.now(UTC), None, 90000, 100000, "abc1234")
    app = _app(grid, client, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        t = (await c.get("/admin/api/tools", headers=BEARER)).json()
        assert {x["kind"] for x in t["tools"]} == set(tools.TOOLS) and t["queue"] == []
        assert (
            t["workers"][0]["name"] == "champis-jobs" and t["workers"][0]["disk_free_mb"] == 90000
        )
        # lancer : action (en-tête), options validées, exclusivité
        r = await c.post(
            "/admin/api/jobs", json={"kind": "warm", "args": {"pull": True}}, headers=BEARER
        )
        assert r.status_code == 403  # sans X-Champis-Admin
        r = await c.post(
            "/admin/api/jobs", json={"kind": "warm", "args": {"interval": 99}}, headers=ACTION
        )
        assert r.status_code == 422 and "au plus" in r.json()["detail"]
        r = await c.post(
            "/admin/api/jobs", json={"kind": "warm", "args": {"pull": False}}, headers=ACTION
        )
        assert r.status_code == 201
        job_id = r.json()["id"]
        assert r.json()["args"] == {"pull": "False", "publish": "True", "interval": "2"}
        r = await c.post("/admin/api/jobs", json={"kind": "warm"}, headers=ACTION)
        assert r.status_code == 409 and job_id in r.json()["detail"]
        assert (
            await c.post("/admin/api/jobs", json={"kind": "render"}, headers=ACTION)
        ).status_code == 201
        t = (await c.get("/admin/api/tools", headers=BEARER)).json()
        assert [j["kind"] for j in t["queue"]] == ["warm", "render"]
        assert t["queue"][0]["origin"] == "admin" and t["queue"][0]["status"] == "queued"
        # annuler : en file → annulée ; inconnue → 409
        r = await c.post(f"/admin/api/jobs/{job_id}/cancel", headers=ACTION)
        assert r.status_code == 200 and r.json()["status"] == "cancelled"
        assert (await c.post(f"/admin/api/jobs/{job_id}/cancel", headers=ACTION)).status_code == 409
        assert (await c.post("/admin/api/jobs/nope/cancel", headers=ACTION)).status_code == 409
        # santé : le service d'exécution est là
        h = (await c.get("/admin/api/health", headers=BEARER)).json()
        assert h["workers"][0]["name"] == "champis-jobs" and h["workers"][0]["silent_min"] < 2
        # cartes : sans bucket, 503
        assert (await c.get("/admin/api/renders", headers=BEARER)).status_code == 503


@pytest.mark.asyncio
async def test_health_warns_about_a_silent_worker(grid, client, store):
    store.worker_heartbeat(
        "champis-jobs", datetime.now(UTC) - timedelta(minutes=30), None, 1000, 100000
    )
    app = _app(grid, client, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        h = (await c.get("/admin/api/health", headers=BEARER)).json()
    texts = [a["text"] for a in h["alerts"]]
    assert any("muet depuis 30 min" in t for t in texts)
    assert any("moins de 5 Go libres sur le volume" in t for t in texts)


@pytest.mark.asyncio
async def test_renders_page_lists_species_and_present_maps(grid, client, store):
    bucket = MemoryBucket()
    bucket.json["tiles/manifest.json"] = {
        "generated": "2026-11-06T10:00:00",
        "depts_by_hash": {"dabc": ["25", "39"], "d25": ["25"]},
        "tiles": [
            {
                "kind": "T",
                "species": "boletus_edulis",
                "hash": "af34c16e",
                "depts_hash": "dabc",
                "name": "T-boletus_edulis-af34c16e-p1.0.0-ddabc.pmtiles",
                "key": "tiles/T-boletus_edulis-af34c16e-p1.0.0-ddabc.pmtiles",
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
        ],
    }
    bucket.objects["renders/T-boletus_edulis-af34c16e-z9.png"] = b"png"
    bucket.objects["renders/T-boletus_edulis-00000000-z10.png"] = b"old"
    bucket.objects["renders/junk.txt"] = b"?"
    app = _app(grid, client, store, bucket=bucket)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        r = (await c.get("/admin/api/renders", headers=BEARER)).json()
    assert r["zooms"] == [9, 10] and r["generated"] == "2026-11-06T10:00:00"
    by_id = {s["species"]: s for s in r["species"]}
    cepe = by_id["boletus_edulis"]
    assert cepe["published"] and cepe["hash"] == "af34c16e" and cepe["name"] == "Cèpe de Bordeaux"
    assert [(f["zoom"], f["current"]) for f in cepe["files"]] == [(10, False), (9, True)]
    assert cepe["files"][1]["url"] == "https://bucket.test/renders/T-boletus_edulis-af34c16e-z9.png"
    assert (
        not by_id["cantharellus_cibarius"]["published"]
        and by_id["cantharellus_cibarius"]["files"] == []
    )
