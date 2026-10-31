"""CLI `champis-api` : warm, seed, publish-seed, notify, doctor, de bout en bout sans réseau
(transport Open-Meteo factice, bucket en mémoire), avec l'affichage et les codes de retour."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from champis_api import cli
from champis_api.cache import WeatherCache
from champis_api.settings import Settings
from champis_pipeline.storage import S3Config
from conftest import GRID, NOW, TODAY, seed_from_scenario
from fixtures.open_meteo import Scenario, transport


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    """`Settings()` fige ses défauts à l'import : la CLI reçoit une fabrique qui pointe sur tmp."""
    grid_path = tmp_path / "weather_grid.json"
    grid_path.write_text(json.dumps(GRID), encoding="utf-8")
    s = Settings(
        weather_grid=grid_path,
        cache_db=tmp_path / "weather_cache.db",
        weather_seed_db=tmp_path / "weather_seed.db",
        open_meteo_api_key="test-key",
        s3=None,
        database_url=f"sqlite:///{tmp_path / 'champis.db'}",
        apns_key="",
    )
    monkeypatch.setattr(cli, "Settings", lambda: s)
    return s


@pytest.fixture
def scenario(monkeypatch) -> Scenario:
    """Toutes les sessions httpx de la CLI parlent au faux Open-Meteo."""
    sc = Scenario(elevation=600.0)
    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs.setdefault("transport", transport(sc, TODAY))
        return real(*args, **kwargs)

    monkeypatch.setattr(cli.httpx, "AsyncClient", client)
    # la CLI prend l'heure réelle ; le scénario est écrit autour de TODAY
    real_client = cli.OpenMeteoClient
    monkeypatch.setattr(
        cli, "OpenMeteoClient", lambda *a, **kw: real_client(*a, now=lambda: NOW, **kw)
    )
    return sc


def test_warm_fills_the_cache_and_computes_the_department_index(settings, scenario, capsys):
    cache = WeatherCache(settings.cache_db)
    seed_from_scenario(cache, scenario, [c["id"] for c in GRID["cells"]])
    cache.close()
    assert cli.main(["-q", "warm", "--interval", "0"]) == 0
    err = capsys.readouterr().err
    assert "[1/2] pré-chauffe du cache météo" in err and "[2/2] indice par département" in err
    assert "2 cellules, 0 indisponibles" in err and "2 départements" in err
    assert err.splitlines()[-1].startswith("pré-chauffe terminée en ")
    assert "forecast" in scenario.calls
    cache = WeatherCache(settings.cache_db)
    assert cache.dept_index_meta() is not None  # l'indice est bien stocké pour GET /departements
    cache.close()


def test_warm_without_seed_reports_unavailable_cells_and_fails(settings, scenario, capsys):
    """Skill open-meteo : une cellule sans climatologie amorcée est `unavailable` ; warm sort en 1."""
    assert cli.main(["-q", "warm", "--interval", "0"]) == 1
    err = capsys.readouterr().err
    assert "2 indisponibles" in err and "pré-chauffe incomplète" in err
    assert "c_0930_6680" in err and "amor" in err.lower()


def test_seed_then_publish_seed(settings, scenario, capsys, monkeypatch, tmp_path: Path):
    """`champis-api seed` écrit la base d'amorçage (historique J−200), `publish-seed` la dépose."""
    assert cli.main(["-q", "seed", "--interval", "0", "--climatology"]) == 0
    err = capsys.readouterr().err
    assert "historique 2/2 cellules, climatologie 2/2" in err
    assert err.splitlines()[-1].startswith("amorçage complet")
    assert settings.weather_seed_db.exists() and scenario.calls == ["archive", "climatology"]
    assert "cellule/s" not in err  # -q : pas de barre tqdm

    for name in (
        "AWS_ENDPOINT_URL",
        "AWS_S3_BUCKET_NAME",
        "CHAMPIS_S3_ENDPOINT",
        "CHAMPIS_S3_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    assert cli.main(["publish-seed"]) == 1
    assert "ECHEC bucket non configuré" in capsys.readouterr().err

    uploaded: dict[str, bytes] = {}

    class Bucket:
        def __init__(self, cfg):
            assert cfg.bucket == "champis"

        def upload(self, path, key, progress=None):
            uploaded[key] = Path(path).read_bytes()
            if progress:
                progress(len(uploaded[key]))

        def put_json(self, key, obj):
            uploaded[key] = json.dumps(obj).encode()

    monkeypatch.setattr(
        "champis_pipeline.storage.S3Config.from_env",
        classmethod(lambda cls: S3Config("https://b", "champis")),
    )
    monkeypatch.setattr("champis_pipeline.storage.Bucket", Bucket)
    assert cli.main(["-q", "publish-seed"]) == 0
    assert "weather/weather_seed.db" in uploaded and "weather/weather_seed.json" in uploaded
    meta = json.loads(uploaded["weather/weather_seed.json"])
    assert meta["history_cells"] == 2 and meta["generated"] == TODAY.isoformat()
    assert "amorçage publié" in capsys.readouterr().err

    assert cli.main(["publish-seed", "--seed", str(tmp_path / "nope.db")]) == 1
    assert "absent" in capsys.readouterr().err


def test_notify_requires_apns(settings, capsys):
    assert cli.main(["notify"]) == 1
    assert "ECHEC APNs non configuré" in capsys.readouterr().err


def test_doctor_runs_through_the_cli(settings, capsys):
    assert cli.main(["doctor", "--offline"]) == 0
    err = capsys.readouterr().err
    assert "grille météo" in err and "2 cellules, version 2" in err
    assert "manque  bucket PMTiles" in err and "manque  notifications APNs" in err
    assert err.splitlines()[-1].startswith("2 point(s) à régler") or "point(s) à régler" in err


def test_help_lists_every_command(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--help"])
    assert e.value.code == 0
    out = capsys.readouterr().out
    for name in ("warm", "seed", "publish-seed", "notify", "doctor"):
        assert name in out
