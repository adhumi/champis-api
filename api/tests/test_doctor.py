"""`champis-api doctor` : chaque point rend ok / manque / info selon l'environnement ; rien n'est écrit."""

from __future__ import annotations

import io
import json
from pathlib import Path

from champis_api import doctor
from champis_api.settings import Settings
from champis_pipeline import console
from champis_pipeline.species import all_species_hash
from champis_pipeline.storage import S3Config


def _settings(tmp_path: Path, monkeypatch, **kwargs) -> Settings:
    """`Settings` fige ses défauts à l'import : on passe les valeurs explicitement."""
    monkeypatch.delenv("CDSAPI_KEY", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    base = {
        "weather_grid": tmp_path / "weather_grid.json",
        "weather_seed_db": tmp_path / "weather_seed.db",
        "open_meteo_api_key": "",
        "s3": None,
        "database_url": f"sqlite:///{tmp_path / 'champis.db'}",
        "apns_key": "",
        "apns_key_id": "",
        "apns_team_id": "",
    }
    return Settings(**{**base, **kwargs})


def _by_name(checks: list[doctor.Check]) -> dict[str, doctor.Check]:
    return {c.name: c for c in checks}


def test_bare_environment_lists_what_is_missing(tmp_path: Path, monkeypatch):
    checks = _by_name(doctor.run_checks(_settings(tmp_path, monkeypatch), offline=True))
    assert (
        checks["grille météo"].status == doctor.MISSING
        and "absent" in checks["grille météo"].detail
    )
    assert checks["amorçage météo"].status == doctor.INFO
    assert checks["Open-Meteo"].status == doctor.INFO
    assert checks["bucket PMTiles"].status == doctor.MISSING
    assert checks["observations et coins"].status == doctor.INFO  # SQLite locale par défaut
    assert checks["notifications APNs"].status == doctor.MISSING
    assert (
        "CHAMPIS_APNS_KEY, CHAMPIS_APNS_KEY_ID, CHAMPIS_APNS_TEAM_ID"
        in checks["notifications APNs"].hint
    )
    assert checks["Climate Data Store"].status == doctor.INFO
    assert checks["espèces"].status == doctor.OK
    assert checks["vecteurs de parité"].status == doctor.OK  # le dépôt est à jour (CI)
    assert checks["disque"].status in (doctor.OK, doctor.MISSING)


def test_grid_checks_version_and_species_hash(tmp_path: Path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    grid = tmp_path / "weather_grid.json"
    grid.write_text(json.dumps({"version": 1, "cells": [{}] * 3}), encoding="utf-8")
    c = doctor._grid(settings)
    assert c.status == doctor.MISSING and "version 1" in c.detail and "weather-grid" in c.hint
    grid.write_text(
        json.dumps({"version": 2, "species_hash": "00000000", "cells": [{}] * 3}), encoding="utf-8"
    )
    c = doctor._grid(settings)
    assert c.status == doctor.MISSING and "hash espèces" in c.detail
    grid.write_text(
        json.dumps({"version": 2, "species_hash": all_species_hash(), "cells": [{}] * 3}),
        encoding="utf-8",
    )
    c = doctor._grid(settings)
    assert c.status == doctor.OK and c.detail == "3 cellules, version 2, espèces à jour"


def test_configured_environment_is_ok(tmp_path: Path, monkeypatch):
    key = tmp_path / "AuthKey.p8"
    key.write_text(
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n", encoding="utf-8"
    )
    settings = _settings(
        tmp_path,
        monkeypatch,
        s3=S3Config(endpoint="https://bucket.test", bucket="champis"),
        apns_key=str(key),
        apns_key_id="ABC123",
        apns_team_id="TEAM",
        open_meteo_api_key="k",
        database_url="postgresql+psycopg://u:secret@host/db",
    )
    monkeypatch.setenv("CDSAPI_KEY", "1:2")  # après _settings, qui l'efface
    checks = _by_name(doctor.run_checks(settings, offline=True))
    assert (
        checks["bucket PMTiles"].status == doctor.INFO
        and "--offline" in checks["bucket PMTiles"].detail
    )
    assert (
        checks["notifications APNs"].status == doctor.OK
        and "ABC123" in checks["notifications APNs"].detail
    )
    assert checks["Open-Meteo"].status == doctor.OK
    db = checks["observations et coins"]
    assert (
        db.status == doctor.OK and "secret" not in db.detail and db.detail.startswith("postgresql")
    )
    assert checks["Climate Data Store"].status == doctor.OK

    class Bucket:
        def __init__(self, cfg):
            pass

        def list(self, prefix):
            return {
                prefix + "T-x.pmtiles": 1,
                prefix + "A-y.pmtiles": 1,
                prefix + "manifest.json": 1,
            }

        def get_json(self, key):
            return {"generated": "2026-10-31T03:00:00+00:00"}

    monkeypatch.setattr("champis_pipeline.storage.Bucket", Bucket)
    c = doctor._bucket(settings, offline=False)
    assert (
        c.status == doctor.OK and c.detail == "champis : 2 PMTiles, manifeste du 2026-10-31T03:00"
    )

    class Broken(Bucket):
        def list(self, prefix):
            raise TimeoutError("trop long")

    monkeypatch.setattr("champis_pipeline.storage.Bucket", Broken)
    assert doctor._bucket(settings, offline=False).status == doctor.MISSING


def test_render_counts_missing_and_shows_hints():
    buf = io.StringIO()
    c = console.Console(stream=buf, color=False)
    checks = [
        doctor.Check("a", doctor.OK, "bien", "pas montré"),
        doctor.Check("bucket", doctor.MISSING, "absent", "AWS_*"),
        doctor.Check("c", doctor.INFO, "normal", "pour mémoire"),
    ]
    assert doctor.render(checks, c) == 1
    lines = buf.getvalue().splitlines()
    assert lines[0] == "ok      a       bien"
    assert lines[1] == "manque  bucket  absent" and lines[2] == "      - AWS_*"
    assert lines[3] == "info    c       normal" and lines[4] == "      - pour mémoire"
    assert lines[5].startswith("1 point(s) à régler en ")
    buf.truncate(0)
    buf.seek(0)
    assert doctor.render(checks[:1], c) == 0
    assert buf.getvalue().splitlines()[-1].startswith("tout est en place en ")


def test_warm_check_says_integrated_or_delegated_to_the_cron(tmp_path: Path, monkeypatch):
    """Décision du 4 nov. 2026 : la pré-chauffe est au cron ; `doctor` le dit, et vérifie qu'un
    cache a bien été publié dans le bucket ; `CHAMPIS_WARM_HOURS` posé = repli intégré."""
    s3 = S3Config(endpoint="https://bucket.test", bucket="champis")
    c = doctor._warm(_settings(tmp_path, monkeypatch, warm_hours=(3, 15)), offline=True)
    assert c.status == doctor.INFO and "intégrée à l'API à 3,15 h Paris" in c.detail
    assert "vider CHAMPIS_WARM_HOURS" in c.hint

    c = doctor._warm(_settings(tmp_path, monkeypatch, warm_hours=()), offline=True)
    assert c.status == doctor.MISSING and "sans bucket" in c.detail

    settings = _settings(tmp_path, monkeypatch, warm_hours=(), s3=s3, cache_poll_minutes=10)
    c = doctor._warm(settings, offline=True)
    assert c.status == doctor.INFO and "déléguée au service cron" in c.detail
    assert "import toutes les 10 min" in c.detail and "--offline" in c.detail

    published: dict = {}

    class Bucket:
        def __init__(self, cfg):
            pass

        def get_json(self, key):
            assert key == "weather/weather_cache.json"
            return published or None

    monkeypatch.setattr("champis_pipeline.storage.Bucket", Bucket)
    c = doctor._warm(settings, offline=False)
    assert c.status == doctor.MISSING and "aucun cache publié" in c.detail
    assert "créer le service cron" in c.hint
    published.update(
        {"published": "2026-11-04T02:41:12+00:00", "cells": 25000, "cell_weather": 50000}
    )
    c = doctor._warm(settings, offline=False)
    assert c.status == doctor.OK
    assert "dernier cache publié le 2026-11-04T02:41 (25000 cellules, 50000 entrées)" in c.detail

    class Broken(Bucket):
        def get_json(self, key):
            raise TimeoutError

    monkeypatch.setattr("champis_pipeline.storage.Bucket", Broken)
    c = doctor._warm(settings, offline=False)
    assert c.status == doctor.MISSING and "injoignable" in c.detail

    c = doctor._warm(
        _settings(tmp_path, monkeypatch, warm_hours=(), s3=s3, cache_poll_minutes=0), True
    )
    assert "import au démarrage seulement" in c.detail


def test_notify_check(tmp_path: Path, monkeypatch):
    c = doctor._notify(_settings(tmp_path, monkeypatch, notify_hours=(7,)))
    assert c.status == doctor.INFO and "par l'API à 7 h" in c.detail and "vider" in c.hint
    c = doctor._notify(_settings(tmp_path, monkeypatch, notify_hours=()))
    assert c.status == doctor.INFO and "déléguée au service cron" in c.detail and not c.hint
    names = [k.name for k in doctor.run_checks(_settings(tmp_path, monkeypatch), offline=True)]
    assert (
        names.index("pré-chauffe")
        < names.index("notification coins")
        < names.index("observations et coins")
    )
