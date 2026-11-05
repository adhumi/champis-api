"""Service HTTP et client Open-Meteo, sur fixtures synthétiques (skill open-meteo : scénarios)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from champis_api.cache import WeatherCache
from champis_api.scoring import phenology_window
from champis_api.weather_client import WeatherUnavailableError, soil_percentile
from champis_api.weather_service import SeriesGapError, build_series, cell_weather
from champis_pipeline.scoring import trapezoid
from champis_pipeline.species import list_species, load_species
from conftest import NOW, TODAY, make_client
from fixtures.open_meteo import Scenario

EDULIS = load_species("boletus_edulis")
CELL = "c_0930_6680"
J = TODAY.isoformat()


def _alt(resp: dict, sid: str, day: date, alt: int) -> dict:
    rows = {r["date"]: r for r in resp["species"][sid]}
    return rows[day.isoformat()]["by_alt"][str(alt)]


# ------------------------------------------------------------------ forme des réponses


@pytest.mark.asyncio
async def test_health_and_species(api):
    r = await api.get("/health")
    assert r.json() == {
        "status": "ok",
        "cells": 2,
        "seeded": 2,
        "dept_index_at": None,
        "cache_published": None,
    }
    r = await api.get("/species")
    body = r.json()
    assert body["scoring_version"] == "1.0.0"
    assert body["palette"]["thresholds"][0]["label"] == "excellent"
    assert body["species"]["boletus_edulis"]["group"]["id"] == "cepes"
    assert len(body["species"]) == 8


@pytest.mark.asyncio
async def test_weather_shape(api):
    r = await api.get("/weather", params={"cell": CELL, "species": "boletus_edulis"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cell"]["id"] == CELL
    assert body["stale"] is False
    assert body["cell_elevation_m"] == 600.0
    rows = body["species"]["boletus_edulis"]
    assert rows[0]["date"] == (TODAY - timedelta(days=3)).isoformat()
    assert len(rows) == 11
    today = _alt(body, "boletus_edulis", TODAY, 450)
    assert set(today) == {"M", "M_eff", "phi", "summary", "trigger_days"} | {"factors"}
    assert set(today["factors"]) == {
        "temp",
        "rain",
        "trigger",
        "soil_moisture",
        "frost",
        "water_balance",
        "heat",
        "memory",
    }
    assert 0 <= today["M_eff"] <= 1
    assert today["phi"] == phenology_window(EDULIS, TODAY, 450.0)
    assert set(rows[0]["by_alt"]) == {"450", "1350"}


@pytest.mark.asyncio
async def test_weather_by_lat_lon_and_errors(api):
    r = await api.get("/weather", params={"lat": 47.12, "lon": 6.37})
    assert r.json()["cell"]["id"] == "c_0935_6680"
    assert set(r.json()["species"]) == set(load_species_ids())
    assert (await api.get("/weather", params={"cell": "nope"})).status_code == 404
    assert (await api.get("/weather")).status_code == 422
    r = await api.get("/weather", params={"cell": CELL, "species": "amanita_phalloides"})
    assert r.status_code == 404


def load_species_ids() -> list[str]:
    from champis_pipeline.species import list_species

    return list_species()


@pytest.mark.asyncio
async def test_bbox(api):
    r = await api.get(
        "/weather/bbox",
        params={
            "min_lon": 6.3,
            "min_lat": 47.0,
            "max_lon": 6.35,
            "max_lat": 47.2,
            "species": "cantharellus_cibarius",
            "day": J,
        },
    )
    body = r.json()
    assert [c["cell"]["id"] for c in body["cells"]] == [CELL]
    assert body["cells"][0]["cell"]["alt_classes"] == [450, 1350]
    rows = body["cells"][0]["species"]["cantharellus_cibarius"]
    assert len(rows) == 1 and rows[0]["date"] == J
    assert "factors" not in rows[0]["by_alt"]["450"]


# ------------------------------------------------------------------ scénarios météo


def _cell(grid):
    return grid.get(CELL)


@pytest.mark.asyncio
async def test_big_rain_then_fair_weather(grid):
    """Grosse pluie J−12, puis beau temps sec : trigger optimal, bilan hydrique neutre."""
    sc = Scenario(elevation=600.0, rain=lambda d: 0.0).override(
        rain={TODAY - timedelta(days=12): 40.0}
    )
    raw = (await make_client(sc).fetch_cells([_cell(grid)]))[CELL]
    resp = cell_weather(_cell(grid), raw, ["boletus_edulis"], TODAY)
    f = _alt(resp, "boletus_edulis", TODAY, 450)["factors"]
    assert f["trigger"] == 1.0  # 12 j ∈ [10, 20]
    assert f["water_balance"] == 1.0  # 40 − 14 × 1,5 = +19 mm


@pytest.mark.asyncio
async def test_best_event_not_last(grid):
    """Petite pluie récente après grosse pluie ancienne : le meilleur événement gagne."""
    old, recent = TODAY - timedelta(days=15), TODAY - timedelta(days=2)
    sc = Scenario(elevation=600.0, rain=lambda d: 0.0).override(rain={old: 25.0, recent: 22.0})
    raw = (await make_client(sc).fetch_cells([_cell(grid)]))[CELL]
    f = _alt(
        cell_weather(_cell(grid), raw, ["boletus_edulis"], TODAY), "boletus_edulis", TODAY, 450
    )
    lag = EDULIS["weather"]["trigger"]["lag_days"]
    assert f["factors"]["trigger"] == max(trapezoid(15, lag), trapezoid(2, lag)) == 1.0


@pytest.mark.asyncio
async def test_snow_is_not_a_trigger(grid):
    day = TODAY - timedelta(days=12)
    sc = Scenario(elevation=600.0, rain=lambda d: 0.0).override(rain={day: 40.0}, snow={day: 5.0})
    raw = (await make_client(sc).fetch_cells([_cell(grid)]))[CELL]
    f = _alt(
        cell_weather(_cell(grid), raw, ["boletus_edulis"], TODAY), "boletus_edulis", TODAY, 450
    )
    assert f["factors"]["trigger"] == 0.0


@pytest.mark.asyncio
async def test_frost_only_at_altitude(grid):
    """Tmin 2 °C à 600 m : pas de gel à 450 m, gel à 1 350 m (2 − 0,006 × 750 = −2,5 °C)."""
    sc = Scenario(elevation=600.0, t_min=lambda d: 2.0)
    raw = (await make_client(sc).fetch_cells([_cell(grid)]))[CELL]
    resp = cell_weather(_cell(grid), raw, ["boletus_edulis"], TODAY)
    low, high = _alt(resp, "boletus_edulis", TODAY, 450), _alt(resp, "boletus_edulis", TODAY, 1350)
    assert low["factors"]["frost"] == 1.0
    assert high["factors"]["frost"] == pytest.approx(EDULIS["weather"]["frost"]["factor"])
    assert high["M_eff"] < low["M_eff"]


# ------------------------------------------------------------------ cache et panne


@pytest.mark.asyncio
async def test_cache_hits_within_bucket(grid, scenario):
    client = make_client(scenario)
    cells = [grid.get(CELL), grid.get("c_0935_6680")]
    await client.fetch_cells(cells)
    assert scenario.calls == [
        "forecast"
    ]  # 2 points, un seul appel ; l'archive n'est jamais appelée
    await client.fetch_cells(cells)
    assert len(scenario.calls) == 1  # rien de nouveau dans le même seau de 12 h


@pytest.mark.asyncio
async def test_outage_serves_stale(grid, scenario):
    cache = WeatherCache()
    fresh = (await make_client(scenario, cache).fetch_cells([_cell(grid)]))[CELL]
    scenario.fail = {"forecast"}
    later = make_client(scenario, cache, now=NOW + timedelta(hours=13))  # seau suivant
    stale = (await later.fetch_cells([_cell(grid)]))[CELL]
    assert stale.stale is True
    assert stale.forecast == fresh.forecast


@pytest.mark.asyncio
async def test_outage_without_cache_is_503(api, scenario):
    scenario.fail = {"forecast"}
    r = await api.get("/weather", params={"cell": CELL})
    assert r.status_code == 503
    assert "indisponible" in r.json()["detail"]
    assert "Retry-After" not in r.headers


@pytest.mark.asyncio
async def test_quota_429_is_503_with_retry_after(api, scenario):
    scenario.fail, scenario.fail_status = {"forecast"}, 429
    r = await api.get("/weather", params={"cell": CELL})
    assert r.status_code == 503
    assert r.headers["Retry-After"] == "42"


@pytest.mark.asyncio
async def test_short_429_is_retried_once(grid, scenario):
    scenario.fail, scenario.fail_status, scenario.fail_once = {"forecast"}, 429, True
    scenario.retry_after = "0"
    raws = await make_client(scenario).fetch_cells([_cell(grid)])
    assert raws[CELL].stale is False
    assert scenario.calls.count("forecast") == 2


@pytest.mark.asyncio
async def test_bbox_partial_when_a_cold_cell_fails(api, grid, scenario, client):
    """Une cellule en cache + une cellule froide en 429 : la première est servie, la seconde listée."""
    await client.fetch_cells([_cell(grid)])  # c_0930_6680 complète en cache
    scenario.fail, scenario.fail_status = {"forecast"}, 429
    r = await api.get(
        "/weather/bbox",
        params={"min_lon": 6.0, "min_lat": 47.0, "max_lon": 6.5, "max_lat": 47.3, "day": J},
    )
    assert r.status_code == 200
    body = r.json()
    assert [c["cell"]["id"] for c in body["cells"]] == [CELL]
    assert body["unavailable"] == ["c_0935_6680"]
    assert body["retry_after"] == 42


@pytest.mark.asyncio
async def test_bbox_too_large_is_422(api, monkeypatch):
    from champis_api.routers import weather as router

    monkeypatch.setattr(router, "MAX_BBOX_CELLS", 1)
    r = await api.get(
        "/weather/bbox", params={"min_lon": 0, "min_lat": 40, "max_lon": 10, "max_lat": 50}
    )
    assert r.status_code == 422
    assert "zoomez" in r.json()["detail"]


# ------------------------------------------------------------------ séries et percentiles


def test_soil_percentile():
    clim = {"months": {"10": [0.05 + 0.04 * i for i in range(11)]}}
    d = date(2026, 10, 17)
    assert soil_percentile(0.01, clim, d) == 0.0
    assert soil_percentile(0.99, clim, d) == 1.0
    assert soil_percentile(0.25, clim, d) == pytest.approx(0.5)
    assert soil_percentile(0.27, clim, d) == pytest.approx(0.55)
    assert soil_percentile(None, clim, d) is None


@pytest.mark.asyncio
async def test_forecast_nulls_are_filled_from_history(grid):
    """Le modèle de prévision ne remonte que ~60 j : avant, l'historique amorcé prend le relais."""
    cut = TODAY - timedelta(days=60)
    sc = Scenario(elevation=600.0, forecast_null_before=cut)
    # pluie mémoire uniquement dans l'historique (J−100) : doit compter dans `memory`
    sc.override(rain={TODAY - timedelta(days=100): 80.0})
    raw = (await make_client(sc).fetch_cells([_cell(grid)]))[CELL]
    s = build_series(raw)
    assert s.dates[0] == TODAY - timedelta(days=200)
    assert s.dates[-1] == TODAY + timedelta(days=7)
    assert s.rain[s.i(TODAY - timedelta(days=100))] == 80.0
    resp = cell_weather(_cell(grid), raw, ["boletus_edulis"], TODAY)
    assert _alt(resp, "boletus_edulis", TODAY, 450)["M_eff"] > 0


@pytest.mark.asyncio
async def test_forecast_past_days_accumulate_in_history(grid, scenario):
    """Chaque prévision range ses ~60 jours passés : demain, l'historique couvre un jour de plus."""
    client = make_client(scenario, seeded=False)
    assert client.cache.history_days(CELL) == 0
    scenario.fail = set()
    res = await client.fetch_cells_partial([_cell(grid)])
    assert CELL in res.unavailable and "amorçage" in res.unavailable[CELL]  # pas de climatologie
    assert client.cache.history_days(CELL) == 92  # mais le passé de la prévision est rangé (max)
    rows = client.cache.history(CELL, TODAY - timedelta(days=60), TODAY)
    assert (TODAY - timedelta(days=1)).isoformat() in rows and TODAY.isoformat() not in rows


def test_seed_never_overwrites_forecast_history():
    from champis_api.cache import WeatherCache

    cache = WeatherCache()
    day = {"t_mean": 1, "t_min": 0, "t_max": 2, "rain": 5.0, "snow": 0, "et0": 1, "soil": 0.3}
    cache.put_history("c", {"2026-09-01": day}, "forecast")
    cache.put_history("c", {"2026-09-01": {**day, "rain": 99.0}, "2026-08-31": day}, "seed")
    rows = cache.history("c", date(2026, 8, 31), date(2026, 9, 1))
    assert rows["2026-09-01"]["rain"] == 5.0 and rows["2026-08-31"]["rain"] == 5.0
    cache.put_history(
        "c", {"2026-09-01": {**day, "rain": None}}, "forecast"
    )  # jour incomplet ignoré
    assert cache.history("c", date(2026, 9, 1), date(2026, 9, 1))["2026-09-01"]["rain"] == 5.0


@pytest.mark.asyncio
async def test_seed_command_writes_db_and_imports(grid, scenario, tmp_path):
    """`champis-api seed` : archive → base SQLite par lots → import par ATTACH (bucket ou local)."""
    from champis_api.cache import WeatherCache
    from champis_api.seed import import_seed, seed

    client = make_client(scenario, seeded=False, api_key="test-key")  # archive via customer-*
    out = tmp_path / "weather_seed.db"
    info = await seed(grid, client, out, today=TODAY)  # historique seul par défaut (option a)
    assert info == {
        "version": 2,
        "generated": TODAY.isoformat(),
        "cells": 0,
        "history_cells": 2,
        "bytes": out.stat().st_size,
        "grid_cells": 2,
    }
    assert scenario.calls == ["archive"]  # un lot de 2 cellules : une requête
    info = await seed(grid, client, out, today=TODAY, climatology=True)
    assert scenario.calls == ["archive", "climatology"] and info["cells"] == 2
    # reprise : rien à refaire
    await seed(grid, client, out, today=TODAY, climatology=True)
    assert len(scenario.calls) == 2
    cache = WeatherCache()
    assert import_seed(cache, out) == 2
    assert cache.history_days(CELL) == 200 and cache.soil_clim(CELL) is not None
    assert cache.meta_get("seed_generated") == TODAY.isoformat()
    # l'ancien JSON reste importable
    legacy = tmp_path / "weather_seed.json"
    day = dict.fromkeys(("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil"), 1.0)
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "generated": "2026-10-01",
                "cells": {"x": {"history": {"2026-09-30": day}, "soil_clim": {"1": [0.1] * 11}}},
            }
        )
    )
    assert import_seed(WeatherCache(), legacy) == 1


@pytest.mark.asyncio
async def test_seed_waits_out_a_quota_429_and_resumes_the_same_batch(grid, scenario, tmp_path):
    """Un 429 de l'archive ne fait pas planter l'amorçage : attente puis même lot."""
    from champis_api.seed import seed

    scenario.fail, scenario.fail_status, scenario.fail_once = {"archive"}, 429, True
    scenario.retry_after = "120"
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    client = make_client(scenario, seeded=False)
    info = await seed(grid, client, tmp_path / "seed.db", today=TODAY, sleep=fake_sleep)
    assert info["history_cells"] == 2
    assert slept == [120.0]
    assert scenario.calls.count("archive") == 2  # le lot en échec est refait, pas sauté


@pytest.mark.asyncio
async def test_seed_quota_wait_is_one_short_line(grid, scenario, tmp_path, caplog):
    """L'attente sur un 429 se dit en une ligne, sans l'URL de 2 000 caractères du lot."""
    from champis_api.seed import seed

    scenario.fail, scenario.fail_status, scenario.fail_once = {"archive"}, 429, True

    async def no_sleep(_):
        return None

    with caplog.at_level("WARNING", logger="champis_api.seed"):
        await seed(
            grid,
            make_client(scenario, seeded=False),
            tmp_path / "s.db",
            today=TODAY,
            sleep=no_sleep,
        )
    (msg,) = [r.getMessage() for r in caplog.records if "reprise" in r.getMessage()]
    assert "quota Open-Meteo atteint (429)" in msg and "http" not in msg


@pytest.mark.asyncio
async def test_seed_does_not_retry_a_non_quota_error(grid, scenario, tmp_path):
    from champis_api.seed import seed

    scenario.fail, scenario.fail_status = {"archive"}, 403
    with pytest.raises(httpx.HTTPStatusError):
        await seed(grid, make_client(scenario, seeded=False), tmp_path / "s.db", today=TODAY)


class _SeedBucket:
    """Bucket factice : fiche + base d'amorçage, téléchargement par copie."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.json: dict[str, object] = {}
        self.downloads: list[str] = []

    def upload(self, path, key, progress=None):
        self.objects[key] = Path(path).read_bytes()
        if progress:
            progress(len(self.objects[key]))

    def put_json(self, key, obj):
        self.json[key] = obj

    def get_json(self, key):
        return self.json.get(key)

    def download(self, key, path):
        self.downloads.append(key)
        Path(path).write_bytes(self.objects[key])

    def list(self, prefix):
        return {}

    def delete(self, keys):
        raise AssertionError

    def presign(self, key, expires_s):
        raise AssertionError


@pytest.mark.asyncio
async def test_second_seed_published_the_same_day_is_imported(grid, scenario, tmp_path):
    """Climatologie publiée le matin, historique complet le soir : même `generated` (un jour),
    mais deux publications. L'API importe la seconde au démarrage suivant au lieu de l'ignorer."""
    from champis_api.cache import WeatherCache
    from champis_api.seed import SEED_META_KEY, publish_seed, seed, sync_seed

    out = tmp_path / "weather_seed.db"
    morning, evening = datetime(2026, 11, 1, 9, tzinfo=UTC), datetime(2026, 11, 1, 23, tzinfo=UTC)
    await seed(grid, make_client(scenario, seeded=False), out, today=TODAY, climatology=True)
    bucket = _SeedBucket()
    publish_seed(bucket, out, now=morning)
    cache, work = WeatherCache(tmp_path / "cache.db"), tmp_path / "vol"
    assert sync_seed(cache, bucket, tmp_path / "absent.json", work) == 2
    assert sync_seed(cache, bucket, tmp_path / "absent.json", work) == 0  # même publication
    info = publish_seed(bucket, out, now=evening)
    assert info["generated"] == bucket.json[SEED_META_KEY]["generated"]  # même jour…
    assert sync_seed(cache, bucket, tmp_path / "absent.json", work) == 2  # …et pourtant importé
    assert sync_seed(cache, bucket, tmp_path / "absent.json", work) == 0
    # fiche d'avant `published` (bucket publié par une version antérieure) : la date fait foi
    del bucket.json[SEED_META_KEY]["published"]
    cache2 = WeatherCache(tmp_path / "cache2.db")
    cache2.meta_set("seed_generated", bucket.json[SEED_META_KEY]["generated"])
    assert sync_seed(cache2, bucket, tmp_path / "absent.json", work) == 0


@pytest.mark.asyncio
async def test_seed_published_to_bucket_is_imported_once_at_startup(grid, scenario, tmp_path):
    from champis_api.cache import WeatherCache
    from champis_api.seed import SEED_DB_KEY, SEED_META_KEY, publish_seed, seed, sync_seed

    out = tmp_path / "weather_seed.db"
    await seed(grid, make_client(scenario, seeded=False), out, today=TODAY)
    bucket = _SeedBucket()
    with pytest.raises(ValueError):  # sans climatologie, rien de servable : refusé
        publish_seed(bucket, out)
    await seed(grid, make_client(scenario, seeded=False), out, today=TODAY, climatology=True)
    info = publish_seed(bucket, out)
    assert (
        SEED_DB_KEY in bucket.objects and bucket.json[SEED_META_KEY]["cells"] == 2 == info["cells"]
    )
    cache = WeatherCache(tmp_path / "cache.db")
    work = tmp_path / "vol"
    assert sync_seed(cache, bucket, tmp_path / "absent.json", work) == 2
    assert cache.seeded_cells() == 2 and not (work / "weather_seed.db").exists()
    assert sync_seed(cache, bucket, tmp_path / "absent.json", work) == 0  # même date : rien
    assert bucket.downloads == [SEED_DB_KEY]
    # nouvel amorçage publié (date différente) → réimporté, sans écraser la prévision
    day = dict.fromkeys(("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil"), 7.0)
    cache.put_history(CELL, {(TODAY - timedelta(days=1)).isoformat(): day}, "forecast")
    bucket.json[SEED_META_KEY] |= {
        "generated": "2026-11-01",
        "published": "2026-11-01T08:00:00+00:00",
    }
    assert sync_seed(cache, bucket, tmp_path / "absent.json", work) == 2
    assert (
        cache.history(CELL, TODAY - timedelta(days=1), TODAY)[
            (TODAY - timedelta(days=1)).isoformat()
        ]["rain"]
        == 7.0
    )
    # sans bucket : le fichier local sert une fois
    cache2 = WeatherCache()
    assert sync_seed(cache2, None, out, work) == 2
    assert sync_seed(cache2, None, out, work) == 0


@pytest.mark.asyncio
async def test_short_history_makes_the_cell_unavailable_until_a_date(grid, scenario):
    """Option a (25 oct. 2026) : climatologie présente, historique encore court → la cellule est
    `unavailable` avec la date où elle sera complète ; les autres cellules sont servies."""
    from champis_api.app import State, create_app
    from champis_api.cache import WeatherCache
    from conftest import seed_from_scenario

    cache = WeatherCache()
    seed_from_scenario(cache, scenario, ["c_0935_6680"])  # complète
    months = {str(m): list(scenario.clim_quantiles) for m in range(1, 13)}
    cache.put_soil_clim(CELL, months)  # climatologie seule : l'historique viendra de past_days=92
    client = make_client(scenario, cache=cache, seeded=False)
    app = create_app(state=State(grid=grid, client=client, cache=cache))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        r = await api.get("/weather", params={"cell": CELL, "species": "boletus_edulis"})
        assert r.status_code == 503, r.text
        assert "historique de 89 j, 130 j requis" in r.json()["detail"]
        assert "complet vers le 2026-11-27" in r.json()["detail"]  # J + 41
        r = await api.get(
            "/weather/bbox",
            params={"min_lon": 6.3, "min_lat": 47.0, "max_lon": 6.4, "max_lat": 47.2},
        )
        body = r.json()
        assert r.status_code == 200 and body["unavailable"] == [CELL]
        assert [c["cell"]["id"] for c in body["cells"]] == ["c_0935_6680"]


@pytest.mark.asyncio
async def test_missing_in_both_sources_is_an_error(grid, scenario):
    raw = (await make_client(scenario).fetch_cells([_cell(grid)]))[CELL]
    k = (
        TODAY - timedelta(days=1)
    ).isoformat()  # hier : dans la prévision (past_days=1) et l'historique
    raw.forecast["rain"][raw.forecast["dates"].index(k)] = None
    raw.history[k]["rain"] = None
    with pytest.raises(SeriesGapError, match="`rain_sum` manquant sur 2026-10-16"):
        build_series(raw)


# ------------------------------------------------------------------ pré-chauffe


@pytest.mark.asyncio
async def test_warm_fills_cache_then_costs_nothing(grid, scenario):
    from champis_api.warm import warm

    client = make_client(scenario)
    report = await warm(grid, client)
    assert report.ok and report.cells == 2 and report.stale == 0
    calls_after_warm = len(scenario.calls)
    await client.fetch_cells(list(grid.cells.values()))
    assert len(scenario.calls) == calls_after_warm  # tout vient du cache


@pytest.mark.asyncio
async def test_past_days_follows_the_history_gap(grid, scenario):
    """Quota (25 oct. 2026) : la prévision ne demande que les jours absents de l'historique."""
    from champis_api.cache import WeatherCache
    from champis_api.warm import warm
    from champis_api.weather_client import call_weight
    from conftest import seed_from_scenario

    cell = _cell(grid)
    # jamais amorcée → maximum (92) ; amorcée jusqu'à hier → 1 ; trou de 5 jours → 5
    assert make_client(scenario, seeded=False).past_days_needed(CELL, TODAY) == 92
    assert make_client(scenario).past_days_needed(CELL, TODAY) == 1
    cache = WeatherCache()
    seed_from_scenario(cache, scenario, [CELL])
    cache.db.execute(
        "DELETE FROM history WHERE cell_id = ? AND day > ?",
        (CELL, (TODAY - timedelta(days=5)).isoformat()),
    )
    assert make_client(scenario, cache=cache, seeded=False).past_days_needed(CELL, TODAY) == 5
    # le lot demande le maximum de ses cellules et le poids est compté
    client = make_client(scenario)  # les deux cellules amorcées jusqu'à hier
    report = await warm(grid, client)
    assert scenario.past_days == [1]  # un seul appel groupé, past_days=1
    assert report.weight == pytest.approx(call_weight(2, 9, 9))  # 2 points, 1+8 jours, 6+3 vars
    assert report.weight / report.cells < 1
    # cellule froide dans le même lot → tout le lot à 92
    client2 = make_client(scenario, cache=cache, seeded=False)  # trou de 5 ; l'autre jamais amorcée
    await client2.fetch_cells_partial([cell, grid.get("c_0935_6680")])
    assert scenario.past_days[-1] == 92


@pytest.mark.asyncio
async def test_warm_reports_unavailable(grid, scenario):
    from champis_api.warm import warm

    scenario.fail = {"forecast"}
    report = await warm(grid, make_client(scenario))
    assert not report.ok
    assert set(report.unavailable) == set(grid.cells)


@pytest.mark.asyncio
async def test_api_key_switches_to_customer_hosts(grid, scenario):
    client = make_client(scenario, api_key="test-key")
    assert client.forecast_url.startswith("https://customer-api.")
    assert client.archive_url.startswith("https://customer-archive-api.")
    assert make_client(scenario).archive_url.startswith("https://archive-api.")  # sans clé
    raws = await client.fetch_cells([_cell(grid)])
    assert raws[CELL].stale is False  # la clé est acceptée par customer-api
    bad = make_client(scenario, api_key="mauvaise")
    with pytest.raises(WeatherUnavailableError):
        await bad.fetch_cells([_cell(grid)])


@pytest.mark.asyncio
async def test_seed_with_standard_key_falls_back_to_free_archive(grid, scenario, tmp_path):
    """Clé du plan Standard : customer-archive-api répond 403 (pas d'API historique) ; l'amorçage
    bascule sur l'archive gratuite, sans clé, au lieu d'échouer (README › amorçage)."""
    from champis_api.seed import seed

    client = make_client(scenario, seeded=False, api_key="standard-key")
    info = await seed(grid, client, tmp_path / "s.db", today=TODAY)
    assert info["history_cells"] == 2
    assert client.archive_url.startswith("https://archive-api.")  # la suite aussi
    assert client.forecast_url.startswith("https://customer-api.")  # la prévision garde la clé


@pytest.mark.asyncio
async def test_http_errors_never_carry_the_api_key(grid, scenario, tmp_path):
    """L'erreur qui remonte jusqu'à la console (et au journal) n'a plus la clé dans son message."""
    scenario.fail, scenario.fail_status = {"forecast"}, 500
    client = make_client(scenario, api_key="test-key")
    with pytest.raises(httpx.HTTPStatusError) as info:
        await client._fetch("forecast", [_cell(grid)], TODAY)
    assert "test-key" not in str(info.value) and "apikey=***" in str(info.value)
    assert info.value.response.status_code == 500


def test_error_messages_hide_the_api_key():
    import httpx
    from champis_api.weather_client import _redact

    req = httpx.Request(
        "GET", "https://customer-api.open-meteo.com/v1/forecast?lat=1&apikey=SECRET&x=1"
    )
    with pytest.raises(httpx.HTTPStatusError) as info:
        httpx.Response(403, request=req).raise_for_status()
    assert "SECRET" in str(info.value)  # httpx met l'URL complète dans le message…
    assert "SECRET" not in _redact(info.value) and "apikey=***" in _redact(info.value)


# ------------------------------------------------------------------ déploiement : web statique, planificateur


@pytest.mark.asyncio
async def test_static_web_is_served(api):
    r = await api.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/web/"
    r = await api.get("/web/")
    assert r.status_code == 200 and "<title>Champis" in r.text
    assert (await api.get("/shared/palette.json")).json()["version"] == 1
    assert (await api.get("/species/boletus_edulis.json")).json()["id"] == "boletus_edulis"


def test_next_warm_picks_next_local_hour():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from champis_api.app import next_warm

    paris = ZoneInfo("Europe/Paris")
    now = datetime(2026, 10, 17, 14, 30, tzinfo=paris)
    assert next_warm(now, (3, 15)) == datetime(2026, 10, 17, 15, tzinfo=paris)
    assert next_warm(datetime(2026, 10, 17, 15, 0, tzinfo=paris), (3, 15)) == datetime(
        2026, 10, 18, 3, tzinfo=paris
    )
    assert next_warm(datetime(2026, 10, 17, 23, 59, tzinfo=paris), (3,)) == datetime(
        2026, 10, 18, 3, tzinfo=paris
    )


class _FakeBucket:
    def __init__(self, manifest):
        self.manifest = manifest
        self.presigned: list[str] = []

    def get_json(self, key):
        return self.manifest if key == "tiles/manifest.json" else None

    def presign(self, key, expires_s):
        self.presigned.append(key)
        return f"https://bucket.test/{key}?X-Amz-Expires={expires_s}"

    def list(self, prefix):
        return {}

    def upload(self, path, key, progress=None):
        raise AssertionError

    def put_json(self, key, obj):
        raise AssertionError

    def download(self, key, path):
        raise AssertionError

    def delete(self, keys):
        raise AssertionError


_MANIFEST = {
    "version": 1,
    "generated": "2026-10-18T00:00:00+00:00",
    "depts_by_hash": {"8c1f10": ["25"]},
    "tiles": [
        {
            "kind": "T",
            "species": "boletus_edulis",
            "hash": "42a4fdc9",
            "scoring_version": "1.0.0",
            "depts_hash": "8c1f10",
            "name": "T-boletus_edulis-42a4fdc9-p1.0.0-d8c1f10.pmtiles",
            "key": "tiles/T-boletus_edulis-42a4fdc9-p1.0.0-d8c1f10.pmtiles",
            "bytes": 12345,
        }
    ],
}


@pytest.mark.asyncio
async def test_tiles_without_bucket(api):
    r = await api.get("/tiles")
    assert r.json() == {"configured": False, "mode": None, "tiles": [], "depts_by_hash": {}}
    assert (
        await api.get("/tiles/T-boletus_edulis-42a4fdc9-p1.0.0-d8c1f10.pmtiles")
    ).status_code == 404


@pytest.mark.asyncio
async def test_tiles_presign_mode(grid, scenario):
    from champis_api.app import State, create_app

    client = make_client(scenario)
    bucket = _FakeBucket(_MANIFEST)
    app = create_app(
        state=State(
            grid=grid,
            client=client,
            cache=client.cache,
            bucket=bucket,
            tiles_mode="presign",
            tiles_url_hours=24,
        )
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        body = (await c.get("/tiles")).json()
    assert body["configured"] and body["mode"] == "presign"
    assert (
        body["tiles"][0]["url"].startswith("https://bucket.test/tiles/T-boletus_edulis")
        and "86400" in body["tiles"][0]["url"]
    )
    assert body["depts_by_hash"] == {"8c1f10": ["25"]}
    assert bucket.presigned == [_MANIFEST["tiles"][0]["key"]]


@pytest.mark.asyncio
async def test_tiles_proxy_mode_relays_range(grid, scenario):
    """En mode proxy, /tiles renvoie des URL relatives et /tiles/{name} relaie Range → 206."""
    from champis_api.app import State, create_app
    from champis_api.weather_client import OpenMeteoClient

    blob = bytes(range(256)) * 4

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "bucket.test":
            rng = req.headers.get("range")
            if rng:
                a, b = (int(x) for x in rng.removeprefix("bytes=").split("-"))
                return httpx.Response(
                    206,
                    content=blob[a : b + 1],
                    headers={
                        "content-range": f"bytes {a}-{b}/{len(blob)}",
                        "etag": '"abc"',
                        "content-type": "application/octet-stream",
                    },
                )
            return httpx.Response(200, content=blob)
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenMeteoClient(http, make_client(scenario).cache, now=lambda: NOW)
    bucket = _FakeBucket(_MANIFEST)
    app = create_app(
        state=State(grid=grid, client=client, cache=client.cache, bucket=bucket, tiles_mode="proxy")
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        body = (await c.get("/tiles")).json()
        assert (
            body["mode"] == "proxy"
            and body["tiles"][0]["url"] == f"/tiles/{_MANIFEST['tiles'][0]['name']}"
        )
        r = await c.get(body["tiles"][0]["url"], headers={"Range": "bytes=10-19"})
        assert r.status_code == 206 and r.content == blob[10:20]
        assert r.headers["content-range"] == f"bytes 10-19/{len(blob)}"
        assert r.headers["accept-ranges"] == "bytes" and "immutable" in r.headers["cache-control"]
        assert (await c.get("/tiles/pas-un-nom.pmtiles")).status_code == 404


# ------------------------------------------------------------------ météo précalculée (2 nov. 2026)


@pytest.mark.asyncio
async def test_bbox_is_served_from_the_precomputed_weather(api, grid, scenario, client):
    """Après la pré-chauffe et l'indice, `/weather/bbox` sert la table `cell_weather` : aucun
    appel Open-Meteo, aucun recalcul, `computed_at` = l'horodatage du passage."""
    from champis_api.departements import compute
    from champis_api.warm import warm
    from champis_pipeline.species import list_species

    assert (await warm(grid, client)).ok
    report = await compute(grid, client, client.cache, list_species())
    assert report.stored == 2 * len(list_species())
    calls_before = list(scenario.calls)
    client.cache.put("c_0930_6680", "forecast", "poison", {"dates": []}, client.now())  # jamais lu

    r = await api.get(
        "/weather/bbox",
        params={"min_lon": 6.0, "min_lat": 47.0, "max_lon": 6.5, "max_lat": 47.3},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert scenario.calls == calls_before
    assert [c["cell"]["id"] for c in body["cells"]] == ["c_0930_6680", "c_0935_6680"]
    assert body["unavailable"] == [] and body["today"] == TODAY.isoformat()
    assert body["computed_at"] is not None and body["scoring_version"] == "1.0.0"
    rows = body["cells"][0]["species"]["boletus_edulis"]
    assert len(rows) == 11 and rows[0]["date"] == (TODAY - timedelta(days=3)).isoformat()
    assert set(rows[0]["by_alt"]["450"]) == {"M", "M_eff", "phi"}

    # Même contenu que le calcul à la demande sur une cellule
    direct = (await api.get("/weather", params={"cell": CELL, "species": "boletus_edulis"})).json()
    by_alt = {r["date"]: r["by_alt"] for r in direct["species"]["boletus_edulis"]}
    for row in rows:
        for alt, v in row["by_alt"].items():
            assert v["M_eff"] == pytest.approx(by_alt[row["date"]][alt]["M_eff"])
            assert v["phi"] == pytest.approx(by_alt[row["date"]][alt]["phi"])

    # `day` filtre toujours
    r = await api.get(
        "/weather/bbox",
        params={"min_lon": 6.0, "min_lat": 47.0, "max_lon": 6.5, "max_lat": 47.3, "day": J},
    )
    assert all(len(rows) == 1 for c in r.json()["cells"] for rows in c["species"].values())


@pytest.mark.asyncio
async def test_bbox_computes_missing_cells_on_demand_and_stores_them(api, client, monkeypatch):
    """Cellule absente du précalcul : calculée à la demande (au plus `MAX_BBOX_COMPUTE` par
    requête), rangée pour la suivante ; les autres sont `unavailable`, jamais un 422."""
    from champis_api.routers import weather as router

    assert client.cache.cell_weather_count() == 0
    monkeypatch.setattr(router, "MAX_BBOX_COMPUTE", 1)
    r = await api.get(
        "/weather/bbox",
        params={
            "min_lon": 6.0,
            "min_lat": 47.0,
            "max_lon": 6.5,
            "max_lat": 47.3,
            "species": "boletus_edulis",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["cell"]["id"] for c in body["cells"]] == ["c_0930_6680"]
    assert body["unavailable"] == ["c_0935_6680"] and body["retry_after"] is None
    assert body["computed_at"] is not None
    assert client.cache.cell_weather_count() == 1  # (c_0930_6680, boletus_edulis)

    # Deuxième requête : la première cellule vient du précalcul, la seconde est calculée
    r = await api.get(
        "/weather/bbox",
        params={
            "min_lon": 6.0,
            "min_lat": 47.0,
            "max_lon": 6.5,
            "max_lat": 47.3,
            "species": "boletus_edulis",
        },
    )
    assert [c["cell"]["id"] for c in r.json()["cells"]] == ["c_0930_6680", "c_0935_6680"]
    assert r.json()["unavailable"] == []
    assert client.cache.cell_weather_count() == 2

    # Une autre espèce n'est pas précalculée pour autant : à la demande à nouveau
    r = await api.get(
        "/weather/bbox",
        params={
            "min_lon": 6.0,
            "min_lat": 47.0,
            "max_lon": 6.5,
            "max_lat": 47.3,
            "species": "cantharellus_cibarius",
        },
    )
    assert [c["cell"]["id"] for c in r.json()["cells"]] == ["c_0930_6680"]
    assert r.json()["unavailable"] == ["c_0935_6680"]


@pytest.mark.asyncio
async def test_bbox_nothing_to_compute_when_everything_is_deferred(api, monkeypatch):
    from champis_api.routers import weather as router

    monkeypatch.setattr(router, "MAX_BBOX_COMPUTE", 0)
    r = await api.get(
        "/weather/bbox", params={"min_lon": 6.0, "min_lat": 47.0, "max_lon": 6.5, "max_lat": 47.3}
    )
    body = r.json()
    assert body["cells"] == [] and body["computed_at"] is None and body["scoring_version"] is None
    assert body["unavailable"] == ["c_0930_6680", "c_0935_6680"]


@pytest.mark.asyncio
async def test_large_json_responses_are_gzipped(api):
    r = await api.get(
        "/weather/bbox",
        params={"min_lon": 6.0, "min_lat": 47.0, "max_lon": 6.5, "max_lat": 47.3},
        headers={"Accept-Encoding": "gzip"},
    )
    assert r.status_code == 200
    assert r.headers.get("content-encoding") == "gzip"
    assert len(r.json()["cells"]) == 2  # httpx décompresse : le contenu est intact
    small = await api.get("/health", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers  # sous le seuil, rien à gagner


# ------------------------------------------------------------------ champ compact (4 nov. 2026)

BBOX_FR = {"min_lon": -6.0, "min_lat": 41.0, "max_lon": 10.0, "max_lat": 52.0}


def _eleven_rows(today: date, by_alt: dict) -> list[dict]:
    """Les onze lignes J−3 → J+7 d'une entrée `cell_weather`, comme `cell_weather()` les produit."""
    return [
        {"date": (today + timedelta(days=k)).isoformat(), "by_alt": by_alt} for k in range(-3, 8)
    ]


async def _precompute(grid, client):
    """Pré-chauffe puis passe de l'indice : `cell_weather` est rempli pour les deux cellules."""
    from champis_api.departements import compute
    from champis_api.warm import warm
    from champis_pipeline.species import list_species

    assert (await warm(grid, client)).ok
    await compute(grid, client, client.cache, list_species())


@pytest.mark.asyncio
async def test_field_is_m_eff_times_phi_rounded_from_the_precomputed_weather(
    api, grid, scenario, client
):
    """`/weather/field` : par cellule, `f[espèce][classe] = M_eff × phi` à trois décimales pour le
    seul jour demandé (spec › Points ouverts › Carte visible dès l'ouverture, 4 nov. 2026), lu
    dans `cell_field`, écrite par la passe de pré-chauffe dans la même transaction que
    `cell_weather` (spec › Points ouverts › Stockage compact par jour, 5 nov. 2026) ; les classes
    sont celles de `by_alt` de `/weather/bbox`, le contenu identique."""
    await _precompute(grid, client)
    calls_before = list(scenario.calls)
    assert client.cache.cell_field_count() == 2 * len(list_species()) * 11  # 11 jours par entrée
    assert client.cache.cell_field_status().current

    r = await api.get(
        "/weather/field",
        params={**BBOX_FR, "day": J, "species": ["boletus_edulis", "cantharellus_cibarius"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert scenario.calls == calls_before  # aucun appel Open-Meteo, aucun calcul
    assert body["today"] == J and body["day"] == J and body["scoring_version"] == "1.0.0"
    assert body["computed_at"] is not None and body["unavailable"] == 0
    assert [c["id"] for c in body["cells"]] == ["c_0930_6680", "c_0935_6680"]
    first = body["cells"][0]
    assert set(first) == {"id", "lat", "lon", "elevation_m", "alt_classes", "f"}  # pas de `stale`
    assert first["lat"] == 47.12 and first["lon"] == 6.31 and first["elevation_m"] == 650
    assert first["alt_classes"] == [450, 1350]
    assert set(first["f"]) == {"boletus_edulis", "cantharellus_cibarius"}
    assert set(first["f"]["boletus_edulis"]) == {"450", "1350"}
    assert set(body["cells"][1]["f"]["boletus_edulis"]) == {"150"}

    bbox = (await api.get("/weather/bbox", params={**BBOX_FR, "day": J})).json()
    assert bbox["computed_at"] == body["computed_at"]
    for cell, ref in zip(body["cells"], bbox["cells"], strict=True):
        assert cell["id"] == ref["cell"]["id"]
        for sid, by_alt in cell["f"].items():
            (row,) = ref["species"][sid]
            for alt, f in by_alt.items():
                v = row["by_alt"][alt]
                assert f == round(v["M_eff"] * v["phi"], 3)
                assert len(str(f).split(".")[-1]) <= 3


@pytest.mark.asyncio
async def test_field_default_species_is_all_and_unknown_is_404(api, grid, client):
    from champis_pipeline.species import list_species

    await _precompute(grid, client)
    r = await api.get("/weather/field", params={**BBOX_FR, "day": J})
    assert r.status_code == 200
    assert all(set(c["f"]) == set(list_species()) for c in r.json()["cells"])
    r = await api.get("/weather/field", params={**BBOX_FR, "day": J, "species": "amanita_x"})
    assert r.status_code == 404 and "amanita_x" in r.json()["detail"]


@pytest.mark.asyncio
async def test_field_day_is_required_and_bounded_to_the_window(api):
    r = await api.get("/weather/field", params=BBOX_FR)
    assert r.status_code == 422  # `day` obligatoire
    for off in (-4, 8):
        bad = (TODAY + timedelta(days=off)).isoformat()
        r = await api.get("/weather/field", params={**BBOX_FR, "day": bad})
        assert r.status_code == 422, off
        assert r.json()["detail"] == (
            f"day={bad} hors de la fenêtre J−3 → J+7 "
            f"({(TODAY - timedelta(days=3)).isoformat()} → {(TODAY + timedelta(days=7)).isoformat()})"
        )
    for off in (-3, 7):  # bornes incluses : rien à servir, mais pas une erreur
        r = await api.get(
            "/weather/field", params={**BBOX_FR, "day": (TODAY + timedelta(days=off)).isoformat()}
        )
        assert r.status_code == 200 and r.json()["cells"] == []


@pytest.mark.asyncio
async def test_field_never_computes_on_demand(api, scenario, client):
    """Cellules absentes du précalcul : omises et comptées, jamais calculées ni demandées à
    Open-Meteo (contrairement à `/weather/bbox`), `computed_at` nul sans cellule servie."""
    assert client.cache.cell_weather_count() == 0
    r = await api.get("/weather/field", params={**BBOX_FR, "day": J})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cells"] == [] and body["unavailable"] == 2 and body["computed_at"] is None
    assert body["scoring_version"] == "1.0.0"
    assert scenario.calls == [] and client.cache.cell_weather_count() == 0


@pytest.mark.asyncio
async def test_field_partial_precompute_stale_and_missing_day(api, grid, client):
    """Une cellule précalculée hier (lignes J−4 → J+6, sans J+7), périmée ; l'autre sans ligne
    pour une des espèces demandées : omise."""
    yesterday = TODAY - timedelta(days=1)
    rows = _eleven_rows(
        yesterday,
        {
            "450": {"M": 0.5, "M_eff": 0.6, "phi": 0.25},
            "1350": {"M": 0.1, "M_eff": 0.12345, "phi": 1.0},
        },
    )
    client.cache.put_cell_weather(
        [(CELL, "boletus_edulis", True, rows), (CELL, "cantharellus_cibarius", True, rows)],
        yesterday,
        NOW - timedelta(days=1),
    )
    client.cache.put_cell_weather(
        [
            (
                "c_0935_6680",
                "boletus_edulis",
                False,
                _eleven_rows(TODAY, {"150": {"M": 1, "M_eff": 1, "phi": 1}}),
            )
        ],
        TODAY,
        NOW,
    )

    r = await api.get(
        "/weather/field",
        params={**BBOX_FR, "day": J, "species": ["boletus_edulis", "cantharellus_cibarius"]},
    )
    body = r.json()
    assert [c["id"] for c in body["cells"]] == [CELL] and body["unavailable"] == 1
    assert body["cells"][0]["stale"] is True
    assert body["cells"][0]["f"] == {
        "boletus_edulis": {"450": 0.15, "1350": 0.123},
        "cantharellus_cibarius": {"450": 0.15, "1350": 0.123},
    }
    assert body["computed_at"] == (NOW - timedelta(days=1)).astimezone(UTC).isoformat(
        timespec="seconds"
    )

    # J+7 d'aujourd'hui manque dans les lignes d'hier : la cellule est indisponible ce jour-là,
    # l'autre, précalculée aujourd'hui, est servie
    r = await api.get(
        "/weather/field",
        params={
            **BBOX_FR,
            "day": (TODAY + timedelta(days=7)).isoformat(),
            "species": "boletus_edulis",
        },
    )
    assert [c["id"] for c in r.json()["cells"]] == ["c_0935_6680"]
    assert r.json()["unavailable"] == 1

    # Avec la seule espèce que la seconde cellule a, elle est servie, sans `stale`
    r = await api.get("/weather/field", params={**BBOX_FR, "day": J, "species": "boletus_edulis"})
    body = r.json()
    assert [c["id"] for c in body["cells"]] == [CELL, "c_0935_6680"] and body["unavailable"] == 0
    assert "stale" not in body["cells"][1] and body["cells"][1]["f"] == {
        "boletus_edulis": {"150": 1.0}
    }
    assert body["computed_at"] == NOW.astimezone(UTC).isoformat(timespec="seconds")


@pytest.mark.asyncio
async def test_field_reads_the_bbox_in_one_sql_query(api, grid, client):
    await _precompute(grid, client)
    statements: list[str] = []
    client.cache.db.set_trace_callback(statements.append)
    try:
        r = await api.get("/weather/field", params={**BBOX_FR, "day": J})
    finally:
        client.cache.db.set_trace_callback(None)
    assert r.status_code == 200 and len(r.json()["cells"]) == 2
    assert len(statements) == 1 and "cell_field" in statements[0]
    assert "cell_weather" not in statements[0]  # jamais les onze jours détaillés


@pytest.mark.asyncio
async def test_field_is_gzipped_and_unbounded(client, store, monkeypatch):
    """Pas de plafond de cellules (la grille entière tient dans une réponse) ; gzip au-delà de
    1 Kio comme les autres réponses. Grille de douze cellules, là où `/weather/bbox` refuserait."""
    from champis_api.app import State, create_app
    from champis_api.grid import Cell, WeatherGrid
    from champis_api.routers import weather as router
    from champis_pipeline.species import list_species

    cells = [
        Cell(f"c_{900 + 5 * i:04d}_6680", 47.1, 6.0 + 0.07 * i, (150.0, 450.0)) for i in range(12)
    ]
    rows = _eleven_rows(
        TODAY,
        {"150": {"M": 0.5, "M_eff": 0.6, "phi": 0.25}, "450": {"M": 0.1, "M_eff": 0.2, "phi": 1.0}},
    )
    client.cache.put_cell_weather(
        [(c.id, sid, False, rows) for c in cells for sid in list_species()], TODAY, NOW
    )
    monkeypatch.setattr(router, "MAX_BBOX_CELLS", 1)
    app = create_app(
        state=State(grid=WeatherGrid(cells), client=client, cache=client.cache, store=store)
    )
    async with app.router.lifespan_context(app):
        transport_ = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport_, base_url="http://test") as api:
            r = await api.get(
                "/weather/field", params={**BBOX_FR, "day": J}, headers={"Accept-Encoding": "gzip"}
            )
            assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
            body = r.json()
            assert len(body["cells"]) == 12 and body["unavailable"] == 0
            assert body["cells"][0]["f"]["boletus_edulis"] == {"150": 0.15, "450": 0.2}
            assert (await api.get("/weather/bbox", params=BBOX_FR)).status_code == 422


@pytest.mark.asyncio
async def test_bbox_on_demand_compute_also_fills_cell_field(api, client, monkeypatch):
    """Les cellules que `/weather/bbox` calcule à la demande (`MAX_BBOX_COMPUTE`) entrent dans
    `cell_field` comme celles de la passe : `/weather/field` les sert ensuite."""
    from champis_api.routers import weather as router

    monkeypatch.setattr(router, "MAX_BBOX_COMPUTE", 1)
    assert client.cache.cell_field_count() == 0
    r = await api.get("/weather/bbox", params={**BBOX_FR, "species": "boletus_edulis"})
    assert [c["cell"]["id"] for c in r.json()["cells"]] == [CELL]
    assert client.cache.cell_field_count() == 11
    (row,) = [x for x in r.json()["cells"][0]["species"]["boletus_edulis"] if x["date"] == J]
    f = (
        await api.get("/weather/field", params={**BBOX_FR, "day": J, "species": "boletus_edulis"})
    ).json()
    assert [c["id"] for c in f["cells"]] == [CELL] and f["unavailable"] == 1
    assert f["cells"][0]["f"]["boletus_edulis"] == {
        alt: round(v["M_eff"] * v["phi"], 3) for alt, v in row["by_alt"].items()
    }


def test_put_cell_weather_writes_cell_field_in_the_same_transaction_and_prune_keeps_the_window():
    """Remplacement par (espèce, jour, cellule) : la passe d'aujourd'hui recouvre les jours
    communs avec celle d'hier ; le jour sorti de la fenêtre (J−4) reste jusqu'à la purge de fin de
    passe, qui ne touche pas la fenêtre J−3 → J+7."""
    cache = WeatherCache()
    yesterday = TODAY - timedelta(days=1)
    old = _eleven_rows(yesterday, {"150": {"M": 0.1, "M_eff": 0.2, "phi": 0.5}})
    cache.put_cell_weather(
        [(CELL, "boletus_edulis", True, old)], yesterday, NOW - timedelta(days=1)
    )
    assert cache.cell_field_count() == 11
    new = _eleven_rows(TODAY, {"150": {"M": 0.4, "M_eff": 0.5, "phi": 0.5}})
    cache.put_cell_weather([(CELL, "boletus_edulis", False, new)], TODAY, NOW)
    assert cache.cell_weather_count() == 1 and cache.cell_field_count() == 12  # J−4 d'hier reste
    got = cache.cell_field([CELL], ["boletus_edulis"], TODAY)[CELL]
    assert got.f == {"boletus_edulis": {"150": 0.25}} and not got.stale
    assert got.computed_at == NOW.astimezone(UTC).isoformat(timespec="seconds")
    assert cache.cell_field([CELL], ["boletus_edulis"], TODAY + timedelta(days=8)) == {}
    assert cache.cell_field([], ["boletus_edulis"], TODAY) == {}
    # une cellule à qui manque une des espèces demandées n'est pas servie du tout
    assert cache.cell_field([CELL], ["boletus_edulis", "boletus_aereus"], TODAY) == {}
    assert cache.prune_cell_field(TODAY) == 1 and cache.cell_field_count() == 11
    assert cache.prune_cell_field(TODAY) == 0
    dates = sorted(r[0] for r in cache.db.execute("SELECT date FROM cell_field"))
    assert dates == [(TODAY + timedelta(days=k)).isoformat() for k in range(-3, 8)]
    # la transaction est une : une ligne invalide ne laisse rien dans aucune des deux tables
    with pytest.raises(KeyError):
        cache.put_cell_weather([("c_x", "boletus_edulis", False, [{"date": J}])], TODAY, NOW)
    cache.db.rollback()
    assert cache.cell_weather_count() == 1 and cache.cell_field_count() == 11


@pytest.mark.asyncio
async def test_rebuild_cell_field_migrates_a_cache_from_before_the_table(api, grid, client, caplog):
    """Une base d'une API déployée avant `cell_field` : `cell_weather` seule. `rebuild_cell_field`
    la dérive (idempotente, journalisée), `ensure_cell_field` ne refait rien ensuite, et
    `/weather/field` répond à l'identique."""
    import logging

    await _precompute(grid, client)
    before = (await api.get("/weather/field", params={**BBOX_FR, "day": J})).json()
    assert before["cells"]
    cache = client.cache
    reference = sorted(cache.db.execute("SELECT * FROM cell_field").fetchall())
    cache.db.execute("DELETE FROM cell_field")
    cache.db.commit()
    assert (await api.get("/weather/field", params={**BBOX_FR, "day": J})).json()["cells"] == []
    status = cache.cell_field_status()
    assert status.present and not status.current and status.field_rows == 0

    with caplog.at_level(logging.INFO, logger="champis_api.cache"):
        n = cache.ensure_cell_field()
    assert n == len(reference) and any("reconstruite" in r.message for r in caplog.records)
    assert sorted(cache.db.execute("SELECT * FROM cell_field").fetchall()) == reference
    assert cache.cell_field_status().current and cache.ensure_cell_field() is None
    assert cache.rebuild_cell_field() == n  # idempotente
    assert (await api.get("/weather/field", params={**BBOX_FR, "day": J})).json() == before

    # une entrée d'hier (J−4 → J+6) : seul J−4 est hors de la fenêtre du `today` le plus récent
    cache.put_cell_weather(
        [
            (
                "c_hier",
                "boletus_edulis",
                True,
                _eleven_rows(TODAY - timedelta(days=1), {"150": {"M": 1, "M_eff": 1, "phi": 1}}),
            )
        ],
        TODAY - timedelta(days=1),
        NOW - timedelta(days=1),
    )
    assert cache.rebuild_cell_field() == n + 10
    cache.db.execute("DELETE FROM cell_weather")  # plus rien à dériver : zéro ligne, à jour
    cache.db.commit()
    assert cache.rebuild_cell_field() == 0 and cache.cell_field_status().current


def test_cell_field_at_grid_scale():
    """Lecture par lot sur une grille de la taille d'un gros écran : une requête, toutes les
    paires, seulement le jour demandé (mesure France entière dans `test_field_france_timing`)."""
    import time

    cache = WeatherCache()
    n, species = 2000, ["boletus_edulis", "cantharellus_cibarius"]
    ids = [f"c_{i:04d}_6400" for i in range(n)]
    rows = _eleven_rows(TODAY, {"150": {"M": 0.4, "M_eff": 0.5, "phi": 0.5}})
    cache.put_cell_weather(((cid, sid, False, rows) for cid in ids for sid in species), TODAY, NOW)
    assert cache.cell_field_count() == n * len(species) * 11
    t0 = time.perf_counter()
    got = cache.cell_field(ids, species, TODAY + timedelta(days=7))
    elapsed = time.perf_counter() - t0
    assert len(got) == n
    assert got[ids[0]].f == {
        "boletus_edulis": {"150": 0.25},
        "cantharellus_cibarius": {"150": 0.25},
    }
    assert cache.cell_field(ids, species, TODAY + timedelta(days=8)) == {}
    assert elapsed < 2  # garde-fou large : ≈ 0,01 s ici, la CI est plus lente et partagée


def _france_grid(n_cells: int):
    """Grille synthétique de la taille de la France (22 672 cellules, deux classes d'altitude),
    rangée dans un cache en mémoire pour quatre espèces et onze jours."""
    from champis_api.grid import Cell, WeatherGrid

    cells = [
        Cell(
            f"c_{i:05d}", 42.0 + (i % 200) * 0.045, -4.0 + (i // 200) * 0.09, (150.0, 450.0), 222.0
        )
        for i in range(n_cells)
    ]
    return WeatherGrid(cells)


@pytest.mark.asyncio
async def test_field_france_timing(client, store, monkeypatch):
    """France entière, 22 672 cellules × 4 espèces, hors compression : objectif < 0,3 s sur un
    portable (spec › Points ouverts › Stockage compact par jour pour /weather/field, 5 nov. 2026 ;
    ≈ 3 s avant, lecture d'un jour dans les onze jours détaillés de `cell_weather`). Le seuil ici
    est large : machine de CI partagée, ≈ 2,5 × plus lente ; le chiffre mesuré est dans la PR."""
    import time

    from champis_api.app import State, create_app

    n = 22_672
    species = ["boletus_edulis", "boletus_aereus", "cantharellus_cibarius", "cantharellus_pallens"]
    grid = _france_grid(n)
    rows = _eleven_rows(
        TODAY,
        {
            "150": {"M": 0.5, "M_eff": 0.6123, "phi": 0.25},
            "450": {"M": 0.1, "M_eff": 0.2, "phi": 1.0},
        },
    )
    client.cache.put_cell_weather(
        ((c.id, sid, False, rows) for c in grid.cells.values() for sid in species), TODAY, NOW
    )
    assert client.cache.cell_field_count() == n * len(species) * 11
    app = create_app(state=State(grid=grid, client=client, cache=client.cache, store=store))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        params = {**BBOX_FR, "day": J, "species": species}
        plain = {"Accept-Encoding": "identity"}  # hors compression
        await api.get("/weather/field", params=params, headers=plain)  # échauffement
        t0 = time.perf_counter()
        r = await api.get("/weather/field", params=params, headers=plain)
        elapsed = time.perf_counter() - t0
    assert r.status_code == 200 and "content-encoding" not in r.headers
    body = r.json()
    assert len(body["cells"]) == n and body["unavailable"] == 0
    assert body["cells"][0]["f"]["boletus_edulis"] == {"150": 0.153, "450": 0.2}
    print(f"\n/weather/field France : {elapsed:.3f} s, {len(r.content) / 1e6:.2f} Mo")
    assert elapsed < 2.0  # ≈ 0,3 s visé sur un portable ; voir la mesure dans la PR
