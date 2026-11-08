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
    assert tools.command("soil-clim", {})[2:6] == [
        sys.executable and "champis_api.cli",
        "-q",
        "pipeline",
        "soil-clim",
    ]
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
    # lot BO4 : les étapes du pipeline passent par `champis-api pipeline <étape>` et prennent
    # une liste de départements validée
    argv = tools.command("batch", {"depts": "25, 39 1", "jobs": "2", "clean_raw": True})
    assert argv[2:6] == ["champis_api.cli", "-q", "pipeline", "batch"]
    assert argv[6:] == ["--depts", "25", "39", "01", "--jobs", "2", "--clean-raw"]
    assert tools.command("tiles", {})[6:] == [
        "--depts",
        "all",
        "--jobs",
        "2",
        "--per-dept",
        "--groups",
    ]
    assert tools.command("publish", {})[6:] == [
        "--depts",
        "all",
        "--per-dept",
        "--groups",
        "--dry-run",
    ]
    assert tools.command("weather-grid", {"depts": "all"})[6:] == ["--depts", "all", "--to-bucket"]
    assert tools.command("soil-clim", {})[2:6] == ["champis_api.cli", "-q", "pipeline", "soil-clim"]
    assert tools.validate("status", {"depts": "2a"})["depts"] == "2A"
    with pytest.raises(tools.ToolError, match="département inconnu"):
        tools.validate("batch", {"depts": "25 99"})
    with pytest.raises(tools.ToolError, match="au moins un département"):
        tools.validate("batch", {"depts": "  "})
    with pytest.raises(tools.ToolError, match="au plus 4"):
        tools.validate("batch", {"jobs": 9})
    described = {t["kind"]: t for t in tools.describe()}
    assert set(described) == set(tools.TOOLS)
    species_field = next(f for f in described["render"]["fields"] if f["name"] == "species")
    assert species_field["choices"][0] == "all" and "boletus_edulis" in species_field["choices"]
    assert described["warm"]["exclusive"] and not described["render"]["exclusive"]
    assert described["batch"]["min_free_gb"] == 10.0 and described["publish"]["confirm"]


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


def test_worker_closes_the_jobs_of_a_worker_that_no_longer_beats(tmp_path: Path):
    """Constaté le 8 nov. 2026 : un déploiement a remplacé le conteneur pendant `tiles`, la tâche
    restait « en cours » sans personne derrière. L'ancien worker ne bat plus : ses tâches sont
    closes en échec ; celles d'un worker qui bat et les passes des crons (sans worker) non."""
    settings = _settings(tmp_path)
    store = Store(settings.database_url)
    now = datetime.now(UTC)
    orphan = store.create_job("tiles", {"depts": "all"}, "admin", now - timedelta(minutes=20))
    store.claim_job(orphan, "w-dead", now - timedelta(minutes=20))
    store.worker_heartbeat("w-dead", now - timedelta(minutes=10), orphan, 90000, 100000)
    alive = store.create_job("render", {}, "admin", now - timedelta(minutes=5))
    store.claim_job(alive, "w-other", now - timedelta(minutes=5))
    store.worker_heartbeat("w-other", now - timedelta(seconds=20), alive, 90000, 100000)
    cron = store.create_job("warm", {}, "cron", now - timedelta(minutes=5), status="running")
    w = worker.Worker(
        settings, store, name="w-new", command=_fake_command("pass"), sleep=lambda s: None
    )
    assert w.run(once=True) == 0  # rien en file : la boucle rend la main après le nettoyage
    job = store.job(orphan)
    assert job["status"] == "failed" and job["exit_code"] == worker.EXIT_INTERRUPTED
    assert "à relancer" in job["summary"]
    assert any(
        e["level"] == "error" and "arrêté pendant" in e["text"] for e in store.events_of(orphan)
    )
    assert store.job(alive)["status"] == "running" and store.job(cron)["status"] == "running"
    store.close()


def test_worker_stop_interrupts_the_running_job_and_says_so(tmp_path: Path):
    """SIGTERM de Railway à un déploiement : le sous-processus s'arrête et la tâche est close en
    échec avec le mot pour la relancer, au lieu de rester « en cours »."""
    settings = _settings(tmp_path)
    store = Store(settings.database_url)
    jid = store.create_job("render", {}, "admin", NOW)
    script = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
    w = worker.Worker(settings, store, command=_fake_command(script), sleep=lambda s: None)
    polls = {"n": 0}

    def sleep(_):
        polls["n"] += 1
        if polls["n"] == 2:
            w.stop = True  # le signal d'arrêt du service

    w.sleep = sleep
    w.clock = lambda c=[0.0]: (c.__setitem__(0, c[0] + 3.0), c[0])[
        1
    ]  # 3 s par tour : SIGKILL au 3e
    code = w.run(once=True)
    assert code != 0
    job = store.job(jid)
    assert job["status"] == "failed" and job["exit_code"] == worker.EXIT_INTERRUPTED
    assert "à relancer" in job["summary"]
    store.close()


def test_a_heartbeat_forgets_workers_replaced_long_ago(tmp_path: Path):
    settings = _settings(tmp_path)
    store = Store(settings.database_url)
    now = datetime.now(UTC)
    store.worker_heartbeat("w-replaced", now - timedelta(hours=2), None, 1, 2)
    store.worker_heartbeat("w-recent", now - timedelta(minutes=10), None, 1, 2)
    store.worker_heartbeat("w-live", now, None, 1, 2)
    assert [w["name"] for w in store.list_workers()] == ["w-live", "w-recent"]
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


def test_worker_refuses_a_heavy_tool_on_a_full_volume(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path)
    store = Store(settings.database_url)
    jid = store.create_job("batch", {"depts": "all"}, "admin", NOW)
    monkeypatch.setattr(worker, "disk_mb", lambda path: (2000, 100000))  # 2 Go libres
    w = worker.Worker(settings, store, command=_fake_command("pass"), sleep=lambda s: None)
    assert w.run(once=True) == 3
    job = store.job(jid)
    assert (
        job["status"] == "failed"
        and "volume trop plein : 2.0 Go libres, 10 Go requis" in job["summary"]
    )
    # un outil léger passe quand même
    light = store.create_job("status", {}, "admin", NOW)
    assert w.run(once=True) == 0 and store.job(light)["status"] == "ok"
    store.close()


def test_pipeline_step_runs_in_the_api_cli_and_registers(tmp_path: Path, monkeypatch, capsys):
    """`champis-api pipeline status --depts 25 --no-bucket` : l'étape tourne sur la console de
    champis-api, donc inscrite dans le journal des tâches sous sa propre sorte."""
    from champis_api import cli
    from champis_pipeline import config

    for name in ("T_DIR", "GRID_DIR", "RAW_DIR", "TILES_DIR"):
        monkeypatch.setattr(config, name, tmp_path / name.lower())
    settings = _settings(tmp_path)
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    assert cli.main(["-q", "pipeline", "status", "--depts", "25", "--no-bucket"]) == 0
    out, err = capsys.readouterr()
    assert "dept" in out and "25" in out  # le tableau, sur stdout
    assert "[1/1] jeu complet · 1 département(s)" in err and "bucket non configuré" in err
    store = Store(settings.database_url)
    (job,) = store.list_jobs()
    assert job["kind"] == "status" and job["status"] == "ok"
    assert job["args"] == {"step": "status", "args": "--depts 25 --no-bucket"}
    assert any(e["text"].startswith("[1/1] jeu complet") for e in store.events_of(job["id"]))
    store.close()
    with pytest.raises(SystemExit):
        cli.main(["pipeline", "download", "--dept", "25"])  # pas dans la passerelle


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
        # cartes et sorties : sans bucket, 503
        assert (await c.get("/admin/api/renders", headers=BEARER)).status_code == 503
        assert (await c.get("/admin/api/artifacts", headers=BEARER)).status_code == 503


@pytest.mark.asyncio
async def test_artifacts_are_listed_with_their_sheet(grid, client, store):
    bucket = MemoryBucket()
    bucket.objects["artifacts/weather_grid.json"] = b"{}"
    bucket.json["artifacts/weather_grid.json.meta.json"] = {
        "name": "weather_grid.json",
        "key": "artifacts/weather_grid.json",
        "generated": "2026-11-08T18:00:00+00:00",
        "bytes": 2,
        "sha256": "ab" * 32,
        "command": "weather-grid --depts all",
        "depts": ["25"],
    }
    app = _app(grid, client, store, bucket=bucket)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        a = (await c.get("/admin/api/artifacts", headers=BEARER)).json()["artifacts"]
    assert len(a) == 1 and a[0]["name"] == "weather_grid.json"
    assert a[0]["url"] == "https://bucket.test/artifacts/weather_grid.json"
    assert a[0]["command"] == "weather-grid --depts all" and a[0]["depts"] == ["25"]


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
    # un conteneur remplacé (plus ancien, muet) ne déclenche pas d'alerte : seul le plus récent compte
    store.worker_heartbeat("champis-jobs-new", datetime.now(UTC), None, 90000, 100000)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        h = (await c.get("/admin/api/health", headers=BEARER)).json()
    assert not any("muet" in a["text"] for a in h["alerts"])
    assert [w["current"] for w in h["workers"]] == [True, False]


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
