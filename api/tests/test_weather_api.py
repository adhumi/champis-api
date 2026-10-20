"""Service HTTP et client Open-Meteo, sur fixtures synthétiques (skill open-meteo : scénarios)."""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest
from champis_api.cache import WeatherCache
from champis_api.scoring import phenology_window
from champis_api.weather_client import WeatherUnavailableError, soil_percentile
from champis_api.weather_service import SeriesGapError, build_series, cell_weather
from champis_pipeline.scoring import trapezoid
from champis_pipeline.species import load_species
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
    assert r.json() == {"status": "ok", "cells": 2, "seeded": 2}
    r = await api.get("/species")
    body = r.json()
    assert body["scoring_version"] == "1.0.0"
    assert body["palette"]["thresholds"][0]["label"] == "excellent"
    assert body["species"]["boletus_edulis"]["group"]["id"] == "cepes"
    assert len(body["species"]) == 7


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
    assert set(today) == {"M", "M_eff", "phi"} | {"factors"}
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
    assert high["factors"]["frost"] == EDULIS["weather"]["frost"]["factor"]
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
    assert client.cache.history_days(CELL) == 60  # mais le passé de la prévision est rangé
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
async def test_seed_command_writes_file_and_imports(grid, scenario, tmp_path):
    """`champis-api seed` : archive gratuite → fichier versionné → import SQLite au démarrage."""
    from champis_api.cache import WeatherCache
    from champis_api.seed import import_seed, load_seed, seed

    client = make_client(
        scenario, seeded=False, api_key="test-key"
    )  # la clé ne sert qu'à la prévision
    out = tmp_path / "weather_seed.json"
    data = await seed(grid, client, out, today=TODAY)
    assert set(data["cells"]) == set(grid.cells)
    assert sorted(scenario.calls) == ["archive", "archive", "climatology", "climatology"]
    assert len(load_seed(out)["cells"][CELL]["history"]) == 200
    # reprise : rien à refaire
    await seed(grid, client, out, today=TODAY)
    assert len(scenario.calls) == 4
    cache = WeatherCache()
    assert import_seed(cache, out) == 2
    assert cache.history_days(CELL) == 200 and cache.soil_clim(CELL) is not None


@pytest.mark.asyncio
async def test_seed_waits_out_a_quota_429_and_resumes_the_same_cell(grid, scenario, tmp_path):
    """Un 429 de l'archive gratuite ne fait pas planter l'amorçage : attente puis même cellule."""
    from champis_api.seed import seed

    scenario.fail, scenario.fail_status, scenario.fail_once = {"archive"}, 429, True
    scenario.retry_after = "120"
    slept: list[float] = []

    async def fake_sleep(s: float) -> None:
        slept.append(s)

    client = make_client(scenario, seeded=False)
    data = await seed(grid, client, tmp_path / "seed.json", today=TODAY, sleep=fake_sleep)
    assert set(data["cells"]) == set(grid.cells)
    assert slept == [120.0]
    assert scenario.calls.count("archive") == 3  # la cellule en échec est refaite, pas sautée


@pytest.mark.asyncio
async def test_seed_does_not_retry_a_non_quota_error(grid, scenario, tmp_path):
    from champis_api.seed import seed

    scenario.fail, scenario.fail_status = {"archive"}, 403
    with pytest.raises(httpx.HTTPStatusError):
        await seed(grid, make_client(scenario, seeded=False), tmp_path / "s.json", today=TODAY)


@pytest.mark.asyncio
async def test_missing_in_both_sources_is_an_error(grid, scenario):
    raw = (await make_client(scenario).fetch_cells([_cell(grid)]))[CELL]
    k = (TODAY - timedelta(days=30)).isoformat()
    raw.forecast["rain"][raw.forecast["dates"].index(k)] = None
    raw.history[k]["rain"] = None
    with pytest.raises(SeriesGapError, match="`rain_sum` manquant sur 2026-09-17"):
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
    assert client.archive_url.startswith("https://archive-api.")  # l'archive reste gratuite
    raws = await client.fetch_cells([_cell(grid)])
    assert raws[CELL].stale is False  # la clé est acceptée par customer-api
    bad = make_client(scenario, api_key="mauvaise")
    with pytest.raises(WeatherUnavailableError):
        await bad.fetch_cells([_cell(grid)])


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

    def upload(self, path, key):
        raise AssertionError

    def put_json(self, key, obj):
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
