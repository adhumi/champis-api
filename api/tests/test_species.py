"""Lot BO5 (spec › Backoffice › Espèces) : l'état de chaque espèce à chaque étape, déduit du
fichier, du manifeste du bucket et de l'inventaire que le worker publie avec son battement ; la
route `GET /admin/api/species` ; l'inventaire du worker ; le champ espèces des outils."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from champis_api import tools, worker
from champis_api.admin import species as view
from champis_api.app import State, create_app
from champis_api.settings import Settings
from champis_api.store import Store
from champis_pipeline.species import all_species_hash, group_hash, species_groups, species_hash
from conftest import MemoryBucket

KEY = "une-cle-de-test-suffisamment-longue-0123456789"
BEARER = {"Authorization": f"Bearer {KEY}"}
NOW = datetime(2026, 11, 8, 19, 0, tzinfo=UTC)
CEPE = "boletus_edulis"
GIROLLE = "cantharellus_cibarius"


def _manifest() -> dict:
    cepes = species_groups()["cepes"]
    h = species_hash(CEPE)
    return {
        "generated": "2026-11-06T10:00:00",
        "depts_by_hash": {"dfr": [f"{d:02d}" for d in range(1, 97)], "d25": ["25"]},
        "tiles": [
            {
                "kind": "T",
                "species": CEPE,
                "hash": h,
                "depts_hash": "dfr",
                "bytes": 2_900_000_000,
                "name": "T-x",
                "key": "tiles/T-x",
            },
            {
                "kind": "T",
                "species": CEPE,
                "hash": h,
                "depts_hash": "d25",
                "dept": "25",
                "bytes": 1,
                "name": "T-y",
                "key": "tiles/T-y",
            },
            {
                "kind": "T",
                "species": GIROLLE,
                "hash": "00000000",
                "depts_hash": "dfr",
                "bytes": 10,
                "name": "T-z",
                "key": "tiles/T-z",
            },
            {
                "kind": "G",
                "group": "cepes",
                "hash": group_hash(cepes),
                "depts_hash": "dfr",
                "species": cepes,
                "name": "G-c",
                "key": "tiles/G-c",
            },
            {
                "kind": "G",
                "group": "girolles",
                "hash": "deadbeef",
                "depts_hash": "dfr",
                "species": [GIROLLE],
                "name": "G-g",
                "key": "tiles/G-g",
            },
        ],
    }


def _inventory() -> dict:
    h = species_hash(CEPE)
    return {
        "at": "2026-11-08T18:59:00+00:00",
        "depts": 96,
        "t": {CEPE: {"current": 96, "stale": 3}, GIROLLE: {"current": 10, "stale": 86}},
        "tiles": [
            f"T-{CEPE}-{h}-p1.0.0-d0a1b2c.pmtiles",
            f"T-{CEPE}-00000000-p1.0.0-d0a1b2c.pmtiles",
            "A-300m-d0a1b2c.pmtiles",
        ],
        "batch": {"ok": 96, "failed": 0, "last": "2026-11-07T20:00:00+00:00"},
    }


def test_gather_states_sources_and_jobs(tmp_path: Path):
    bucket = MemoryBucket()
    bucket.json["tiles/manifest.json"] = _manifest()
    bucket.objects[f"renders/T-{CEPE}-{species_hash(CEPE)}-z9.png"] = b"png"
    store = Store("sqlite://")
    store.create_job(
        "tiles", {"depts": "all", "species": GIROLLE, "jobs": "2"}, "admin", NOW, status="running"
    )
    store.bump(NOW.date(), "layer_app:cepes", 30)
    store.bump(NOW.date(), "layer_app:girolles", 10)
    store.bump(NOW.date(), "layer_web:boletus_edulis", 5)
    grid = tmp_path / "weather_grid.json"
    grid.write_text(json.dumps({"version": 2, "species_hash": "stale000"}))
    settings = Settings(weather_grid=grid, s3=None, apns_key="")
    workers = [
        {
            "name": "champis-jobs",
            "heartbeat_at": "2026-11-08T18:59:30+00:00",
            "inventory": _inventory(),
        }
    ]

    d = view.gather(settings, bucket, store, workers)
    by_id = {s["id"]: s for s in d["species"]}
    assert d["grid"] == {"hash": "stale000", "current_hash": all_species_hash(), "current": False}
    assert d["inventory_at"] == "2026-11-08T18:59:00+00:00"

    cepe = by_id[CEPE]
    assert (
        cepe["state"] == "a_jour"
        and cepe["name"] == "Cèpe de Bordeaux"
        and cepe["group"]["id"] == "cepes"
    )
    assert cepe["published"]["depts"] == 96 and cepe["published"]["dept_sets"] == 1
    assert cepe["published"]["group_current"] is True
    assert cepe["worker"] == {
        "t_current": 96,
        "t_stale": 3,
        "depts": 96,
        "tiles_current": 1,
        "tiles_stale": 1,
        "batch_last": "2026-11-07T20:00:00+00:00",
        "at": "2026-11-08T18:59:00+00:00",
    }
    assert cepe["maps"][0]["current"] and cepe["jobs"] == []
    # usage : l'espèce plus son groupe, en part du total app / site sur 30 jours
    assert cepe["usage"]["app"] == 30 and cepe["usage"]["app_share"] == 75
    assert cepe["usage"]["web"] == 5 and cepe["usage"]["web_share"] == 100

    girolle = by_id[GIROLLE]
    assert girolle["state"] == "en_cours" and girolle["jobs"][0]["kind"] == "tiles"
    assert (
        girolle["published"]["hash"] == "00000000"
        and girolle["published"]["group_current"] is False
    )
    assert girolle["worker"]["t_current"] == 10 and girolle["worker"]["tiles_current"] == 0

    # sans tâche, la girolle est périmée ; une espèce sans T publié n'a jamais été publiée
    store.close()
    store = Store("sqlite://")
    d = view.gather(settings, bucket, store, workers)
    by_id = {s["id"]: s for s in d["species"]}
    assert by_id[GIROLLE]["state"] == "perimee"
    others = [s for s in d["species"] if s["id"] not in (CEPE, GIROLLE)]
    assert others and all(s["state"] == "jamais_publiee" for s in others)
    # sans bucket ni worker : rien d'inventé
    d = view.gather(settings, None, None, [])
    assert d["species"][0]["published"] is None and d["species"][0]["worker"] is None
    assert d["species"][0]["usage"]["app_share"] is None
    store.close()


def test_concerns_matches_species_group_and_global_jobs():
    assert view._concerns({"kind": "batch", "args": {"species": ""}}, CEPE, ["cepes"])
    assert view._concerns({"kind": "batch", "args": {"species": CEPE}}, CEPE, ["cepes"])
    assert not view._concerns({"kind": "batch", "args": {"species": GIROLLE}}, CEPE, ["cepes"])
    assert view._concerns(
        {"kind": "tiles", "args": {"step": "tiles", "args": f"--depts all --species {CEPE}"}},
        CEPE,
        ["cepes"],
    )
    assert view._concerns({"kind": "render", "args": {"species": "all"}}, CEPE, ["cepes"])
    assert not view._concerns({"kind": "render", "args": {"species": GIROLLE}}, CEPE, ["cepes"])
    assert view._concerns({"kind": "weather-grid", "args": {}}, CEPE, ["cepes"])
    assert not view._concerns({"kind": "warm", "args": {}}, CEPE, ["cepes"])


def test_worker_inventory_reads_the_volume(tmp_path: Path, monkeypatch):
    from champis_pipeline import config

    monkeypatch.setattr(config, "T_DIR", tmp_path / "T")
    monkeypatch.setattr(config, "TILES_DIR", tmp_path / "tiles")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    h = species_hash(CEPE)
    (tmp_path / "T" / CEPE).mkdir(parents=True)
    (tmp_path / "T" / CEPE / f"25-{h}.tif").write_bytes(b"x")
    (tmp_path / "T" / CEPE / f"39-{h}.tif").write_bytes(b"x")
    (tmp_path / "T" / CEPE / "70-00000000.tif").write_bytes(b"x")
    (tmp_path / "tiles").mkdir()
    (tmp_path / "tiles" / "A-300m-dabc.pmtiles").write_bytes(b"x")
    (tmp_path / "batch_status.json").write_text(
        json.dumps(
            {
                "25": {"ok": True, "finished": "2026-11-08T10:00:00+00:00"},
                "39": {"ok": False, "finished": "2026-11-08T11:00:00+00:00"},
            }
        )
    )
    inv = worker.inventory()
    assert inv["t"][CEPE] == {"current": 2, "stale": 1} and inv["t"][GIROLLE] == {
        "current": 0,
        "stale": 0,
    }
    assert inv["tiles"] == ["A-300m-dabc.pmtiles"] and inv["depts"] == 96
    assert inv["batch"] == {"ok": 1, "failed": 1, "last": "2026-11-08T11:00:00+00:00"}
    assert inv["data_dir"] == str(tmp_path)
    # le battement le range en base, la liste des workers le rend
    store = Store("sqlite://")
    store.worker_heartbeat("w", NOW, None, 1000, 2000, "b", inventory=inv)
    (row,) = store.list_workers()
    assert row["inventory"]["t"][CEPE]["current"] == 2
    store.worker_heartbeat("w", NOW, None, 1000, 2000, "b")  # sans inventaire : l'ancien reste
    assert store.list_workers()[0]["inventory"]["tiles"] == ["A-300m-dabc.pmtiles"]
    store.close()


def test_tools_species_field():
    assert tools.command("batch", {"species": f"{CEPE} {GIROLLE}"})[6:] == [
        "--depts",
        "all",
        "--species",
        CEPE,
        "--species",
        GIROLLE,
        "--jobs",
        "2",
        "--clean-raw",
    ]
    assert tools.validate("tiles", {"species": ""})["species"] == ""
    assert tools.validate("publish", {"species": f"{CEPE}, {CEPE}"})["species"] == CEPE
    with pytest.raises(tools.ToolError, match="espèce inconnue amanita"):
        tools.validate("batch", {"species": "amanita"})


def test_data_dir_follows_the_environment(tmp_path: Path):
    code = "from champis_pipeline import config; print(config.DATA_DIR, config.T_DIR)"
    r = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"CHAMPIS_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [str(tmp_path), str(tmp_path / "T")]


@pytest.mark.asyncio
async def test_species_route(grid, client, store):
    bucket = MemoryBucket()
    bucket.json["tiles/manifest.json"] = _manifest()
    store.worker_heartbeat(
        "champis-jobs", datetime.now(UTC), None, 90000, 100000, "x", inventory=_inventory()
    )
    app = create_app(
        settings=Settings(admin_key=KEY, s3=None, apns_key=""),
        state=State(grid=grid, client=client, cache=client.cache, store=store, bucket=bucket),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        assert (await c.get("/admin/api/species")).status_code == 401
        r = await c.get("/admin/api/species", headers=BEARER)
    assert r.status_code == 200
    d = r.json()
    assert len(d["species"]) == len(species_groups()["cepes"]) + sum(
        len(v) for g, v in species_groups().items() if g != "cepes"
    )
    cepe = next(s for s in d["species"] if s["id"] == CEPE)
    assert cepe["state"] == "a_jour" and cepe["worker"]["t_current"] == 96
