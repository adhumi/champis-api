"""Lot BO4 (spec › Backoffice › Tâches) : le pipeline sur le service d'exécution. `batch
--clean-raw` efface les entrées brutes d'un département une fois ses T écrits ; `weather-grid
--to-bucket` dépose le fichier versionné dans le bucket sous `artifacts/`, avec sa fiche."""

from __future__ import annotations

import json
from pathlib import Path

from champis_pipeline import artifacts, batch, cli, config


class MemoryBucket:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.json: dict[str, object] = {}

    def upload(self, path, key, progress=None):
        self.objects[key] = Path(path).read_bytes()

    def put_json(self, key, obj):
        self.json[key] = obj

    def get_json(self, key):
        return self.json.get(key)

    def list(self, prefix):
        return {k: len(v) for k, v in self.objects.items() if k.startswith(prefix)} | {
            k: 1 for k in self.json if k.startswith(prefix)
        }


def test_publish_and_list_artifacts(tmp_path: Path):
    b = MemoryBucket()
    f = tmp_path / "weather_grid.json"
    f.write_text('{"version": 2}')
    meta = artifacts.publish_artifact(
        b, f, "weather_grid.json", "weather-grid --depts all", {"depts": ["25"]}
    )
    assert meta["key"] == "artifacts/weather_grid.json" and meta["bytes"] == 14
    assert meta["sha256"].startswith("") and meta["depts"] == ["25"] and meta["generated"]
    assert b.objects["artifacts/weather_grid.json"] == b'{"version": 2}'
    assert b.json["artifacts/weather_grid.json.meta.json"] == meta
    (entry,) = artifacts.list_artifacts(b)
    assert entry["name"] == "weather_grid.json" and entry["command"] == "weather-grid --depts all"
    assert entry["bytes"] == 14


def test_batch_clean_raw_removes_the_department_inputs_only(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "RAW_DIR", tmp_path / "raw")
    (tmp_path / "raw" / "25" / "bdforet").mkdir(parents=True)
    (tmp_path / "raw" / "25" / "bdforet" / "D025.7z").write_bytes(b"x" * 1000)
    (tmp_path / "raw" / "dem").mkdir()
    (tmp_path / "raw" / "dem" / "tile.tif").write_bytes(b"shared")
    (tmp_path / "raw" / "39" / "bdforet").mkdir(parents=True)
    (tmp_path / "raw" / "39" / "bdforet" / "D039.7z").write_bytes(b"y")

    def runner(dept, species, force):
        return batch.DeptResult(dept, dept == "25", "fait" if dept == "25" else "KO", 1.0, "t")

    class Inline:
        def __init__(self, n):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def submit(self, fn, *a):
            from concurrent.futures import Future

            f = Future()
            f.set_result(fn(*a))
            return f

    seen = []
    status = batch.batch(
        ["25", "39"],
        None,
        jobs=1,
        runner=runner,
        executor_factory=Inline,
        status_path=tmp_path / "status.json",
        report=seen.append,
        clean_raw=True,
    )
    assert status["25"]["ok"] and not status["39"]["ok"]
    assert not (tmp_path / "raw" / "25").exists()  # effacé : T écrits
    assert (tmp_path / "raw" / "39").exists()  # gardé : en échec, à reprendre
    assert (tmp_path / "raw" / "dem" / "tile.tif").exists()  # partagé, jamais touché
    ok = next(r for r in seen if r.dept == "25")
    assert "0,0 Go d'entrées brutes effacés" in ok.message.replace(".", ",")
    assert batch.clean_dept_raw("25") == 0  # déjà parti


def test_weather_grid_to_bucket_goes_through_the_cli(tmp_path: Path, monkeypatch, capsys):
    out = tmp_path / "weather_grid.json"
    out.write_text(json.dumps({"version": 2, "cells": []}))
    monkeypatch.setattr(cli.weather_grid, "build", lambda depts, species: out)
    monkeypatch.setattr(cli, "_depts", lambda args: ["25"])
    for name in (
        "AWS_ENDPOINT_URL",
        "AWS_S3_BUCKET_NAME",
        "CHAMPIS_S3_ENDPOINT",
        "CHAMPIS_S3_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["-q", "weather-grid", "--depts", "25", "--to-bucket"]) == 1
    assert "bucket non configuré" in capsys.readouterr().err

    b = MemoryBucket()
    from champis_pipeline.storage import S3Config

    monkeypatch.setattr(
        "champis_pipeline.storage.S3Config.from_env",
        classmethod(lambda cls: S3Config("https://b", "champis")),
    )
    monkeypatch.setattr("champis_pipeline.storage.Bucket", lambda cfg: b)
    assert cli.main(["-q", "weather-grid", "--depts", "25", "--to-bucket"]) == 0
    err = capsys.readouterr().err
    assert "[2/2] dépôt dans le bucket" in err and "artifacts/weather_grid.json" in err
    assert b.objects["artifacts/weather_grid.json"] == out.read_bytes()
    assert b.json["artifacts/weather_grid.json.meta.json"]["depts"] == ["25"]
    # sans --to-bucket : une seule étape, rien dans le bucket
    b.objects.clear()
    assert cli.main(["-q", "weather-grid", "--depts", "25"]) == 0
    assert "[1/1] weather-grid" in capsys.readouterr().err and not b.objects
