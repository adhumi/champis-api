"""CLI `champis-api` : warm, seed, publish-seed, notify, doctor, de bout en bout sans réseau
(transport Open-Meteo factice, bucket en mémoire), avec l'affichage et les codes de retour."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from champis_api import cli
from champis_api.cache import WeatherCache
from champis_api.settings import Settings
from champis_pipeline.storage import S3Config
from conftest import GRID, NOW, TODAY, MemoryBucket, seed_from_scenario
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
    for name in ("warm", "seed", "publish-seed", "notify", "publish-cache", "doctor"):
        assert name in out


# ------------------------------------------------------------------ service cron (4 nov. 2026)


@pytest.fixture
def bucket(settings, monkeypatch) -> MemoryBucket:
    """Le bucket Railway du cron : l'amorçage y est déjà publié (`publish-seed`)."""
    from champis_api.seed import publish_seed

    b = MemoryBucket()
    monkeypatch.setattr(
        cli, "Settings", lambda: replace(settings, s3=S3Config("https://b", "champis"))
    )
    monkeypatch.setattr("champis_pipeline.storage.Bucket", lambda cfg: b)
    seed_db = WeatherCache(settings.weather_seed_db)
    seed_from_scenario(seed_db, Scenario(elevation=600.0), [c["id"] for c in GRID["cells"]])
    seed_db.meta_set("seed_generated", TODAY.isoformat())
    seed_db.close()
    publish_seed(b, settings.weather_seed_db)
    return b


def test_warm_pull_requires_a_bucket(settings, scenario, capsys):
    assert cli.main(["-q", "warm", "--pull"]) == 1
    assert "ECHEC bucket non configuré (--pull/--publish)" in capsys.readouterr().err


def test_cron_pass_pulls_warms_and_publishes_what_the_api_imports(
    settings, scenario, bucket: MemoryBucket, capsys, tmp_path: Path
):
    """`railway.warm.toml` : `warm --pull --publish` dans un conteneur sans volume. Première passe :
    rien de publié, l'amorçage du bucket suffit ; l'instantané déposé contient l'indice et la
    météo précalculée ; l'API l'importe une fois (`sync_cache`), puis plus rien tant qu'il ne
    change pas. Seconde passe, cache vide : elle repart de l'instantané, sans refaire l'amorçage."""
    from champis_api.snapshot import CACHE_DB_KEY, CACHE_META_KEY, sync_cache

    assert cli.main(["-q", "warm", "--interval", "0", "--pull", "--publish"]) == 0
    err = capsys.readouterr().err
    assert "[1/5] cache publié dans le bucket" in err and "deja fait" in err and "aucun" in err
    assert "[2/5] amorçage" in err and "2 cellules importées" in err
    assert "[3/5] pré-chauffe" in err and "[4/5] indice" in err
    assert "[5/5] publication du cache dans le bucket" in err and "2 cellules" in err
    assert bucket.uploads[-1] == CACHE_DB_KEY and bucket.downloads == ["weather/weather_seed.db"]
    meta = bucket.json[CACHE_META_KEY]
    assert meta["cells"] == 2 and meta["cell_weather"] > 0 and meta["today"] == TODAY.isoformat()
    assert meta["dept_index_at"] and meta["published"] and meta["bytes"] > 0
    # le cron garde la trace de sa publication : la passe suivante ne la retélécharge pas
    local = WeatherCache(settings.cache_db)
    assert local.meta_get("cache_published") == meta["published"]
    local.close()
    assert cli.main(["-q", "warm", "--interval", "0", "--pull"]) == 0
    assert "rien de nouveau" in capsys.readouterr().err
    assert bucket.downloads == ["weather/weather_seed.db"]

    # l'API : import en une fois, idempotent
    (tmp_path / "api").mkdir()
    api_cache = WeatherCache(tmp_path / "api" / "weather_cache.db", shared=True)
    assert sync_cache(api_cache, bucket, tmp_path / "api") == meta
    assert api_cache.dept_index_meta() is not None and api_cache.seeded_cells() == 2
    assert api_cache.cell_weather_count() == meta["cell_weather"]
    assert (
        api_cache.meta_get("seed_published")
        == bucket.json["weather/weather_seed.json"]["published"]
    )
    assert sync_cache(api_cache, bucket, tmp_path / "api") is None
    assert not (tmp_path / "api" / "weather_cache.download.db").exists()
    assert sync_cache(api_cache, None, tmp_path / "api") is None  # sans bucket : rien
    api_cache.close()

    # seconde passe du cron, conteneur neuf (cache vide) : repart de l'instantané
    settings.cache_db.unlink()
    assert cli.main(["-q", "warm", "--interval", "0", "--pull", "--publish"]) == 0
    err = capsys.readouterr().err
    assert "[1/5] cache publié dans le bucket" in err and f"{meta['published']}, 2 cellules" in err
    assert "[2/5] amorçage" in err and "2 cellules amorcées" in err  # déjà dans l'instantané
    assert bucket.downloads[-1] == CACHE_DB_KEY and bucket.uploads.count(CACHE_DB_KEY) == 2


def test_publish_cache_command(settings, scenario, bucket: MemoryBucket, capsys):
    """Migration : une API qui pré-chauffait elle-même dépose son cache avant le premier cron."""
    from champis_api.snapshot import CACHE_DB_KEY

    assert cli.main(["-q", "publish-cache"]) == 1
    assert "absent : rien à publier" in capsys.readouterr().err
    assert cli.main(["-q", "warm", "--interval", "0", "--pull"]) == 0
    capsys.readouterr()
    assert cli.main(["-q", "publish-cache"]) == 0
    err = capsys.readouterr().err
    assert "[1/1] publication du cache dans le bucket" in err and "cache publié" in err
    assert bucket.uploads[-1] == CACHE_DB_KEY and bucket.uploads.count(CACHE_DB_KEY) == 1


def test_notify_pull_takes_the_published_cache_first(
    settings, scenario, bucket: MemoryBucket, capsys, monkeypatch, tmp_path: Path
):
    """`railway.notify.toml` : `notify --pull` à 7 h reprend la passe de 3 h avant d'annoncer."""
    from champis_api.coins import NotifyReport
    from champis_api.snapshot import CACHE_DB_KEY

    assert cli.main(["-q", "warm", "--interval", "0", "--pull", "--publish"]) == 0
    settings.cache_db.unlink()
    key = tmp_path / "AuthKey.p8"
    key.write_text(
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n", encoding="utf-8"
    )
    base = cli.Settings()
    monkeypatch.setattr(
        cli, "Settings", lambda: replace(base, apns_key=str(key), apns_key_id="K", apns_team_id="T")
    )
    monkeypatch.setattr(cli, "ApnsClient", lambda *a, **kw: object())
    seen = {}

    async def fake_notify_all(store, grid, client, apns):
        seen["cell_weather"] = client.cache.cell_weather_count()
        return NotifyReport(devices=1, sent=1, skipped=0, failed=0, tokens_cleared=0)

    monkeypatch.setattr(cli, "notify_all", fake_notify_all)
    capsys.readouterr()
    assert cli.main(["-q", "notify", "--pull"]) == 0
    err = capsys.readouterr().err
    assert "[1/3] cache publié dans le bucket" in err and "[3/3] notification « mes coins »" in err
    assert "1 appareils, 1 envoyées" in err
    assert bucket.downloads[-1] == CACHE_DB_KEY and seen["cell_weather"] > 0
