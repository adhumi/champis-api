"""Température du sol 0–7 cm : facteur optionnel `weather.soil_temp` (spec › Scoring › Facteurs de
sol et de paysage, 9 nov. 2026). Absent du fichier = poids 0 et rien ne change ; présent, un trou
dans la fenêtre est une erreur ; l'historique d'avant la variable se lit toujours."""

from __future__ import annotations

import copy
from datetime import date, timedelta

import pytest
from champis_api.cache import HISTORY_VARS, WeatherCache
from champis_api.explain import summary
from champis_api.scoring import (
    SeriesGapError,
    factor_soil_temp,
    required_series_days,
    weather_index,
    weather_index_raw,
)
from champis_api.vectors import SOIL_TEMP_OVERRIDE
from champis_api.weather_client import SERIES_VARS, SOIL_TEMP_DAILY, CellRaw, _normalize_series
from champis_api.weather_service import build_series, cell_weather
from champis_pipeline.species import load_species
from conftest import TODAY, make_client, make_series, seed_from_scenario
from fixtures.open_meteo import Scenario

DAY = date(2026, 10, 13)


def _species_with_soil_temp() -> dict:
    sp = copy.deepcopy(load_species("boletus_edulis"))
    sp["weather"] = {**sp["weather"], **copy.deepcopy(SOIL_TEMP_OVERRIDE)}  # jamais le dict partagé
    return sp


def _series(n: int = 140, soil_temp: float | None = 11.0):
    return make_series(DAY - timedelta(days=n - 1), n, soil_temp=soil_temp)


def test_factor_follows_the_trapezoid_and_the_altitude_correction():
    sp = _species_with_soil_temp()
    assert factor_soil_temp(sp, _series(), DAY, 700.0) == 1.0  # 11 °C dans [8, 15]
    # 1 450 m : −0,6 °C/100 m × 750 m = −4,5 °C → 6,5 °C, sur la rampe 4 → 8
    assert factor_soil_temp(sp, _series(), DAY, 1450.0) == pytest.approx(0.625)
    assert factor_soil_temp(sp, _series(soil_temp=3.0), DAY, 700.0) == 0.0
    # un jour manquant dans la fenêtre : erreur nommée, jamais une moyenne sur moins de jours
    s = _series()
    s.soil_temp[s.i(DAY - timedelta(days=3))] = None
    with pytest.raises(SeriesGapError, match="soil_temperature_0_to_7cm_mean"):
        factor_soil_temp(sp, s, DAY, 700.0)
    with pytest.raises(SeriesGapError):
        factor_soil_temp(sp, _series(soil_temp=None), DAY, 700.0)


def test_species_without_the_block_ignore_the_variable():
    sp = load_species("boletus_edulis")
    without = weather_index_raw(sp, _series(soil_temp=None), DAY, 700.0)
    with_values = weather_index_raw(sp, _series(soil_temp=3.0), DAY, 700.0)
    assert "soil_temp" not in without and without == with_values
    assert required_series_days(sp) == 130


def test_species_with_the_block_weigh_it_in_the_sum():
    sp = _species_with_soil_temp()
    f = weather_index(sp, _series(soil_temp=3.0), DAY, 700.0)
    assert f["soil_temp"] == 0.0
    ideal = weather_index(sp, _series(), DAY, 700.0)
    assert ideal["soil_temp"] == 1.0 and ideal["M"] > f["M"]
    # la fenêtre du sol (10 j) est plus courte que celle de la pluie (28 j) : série requise inchangée
    assert required_series_days(sp) == 130
    sp["weather"]["soil_temp"]["window_days"] = 40
    assert required_series_days(sp) == 142
    assert "sol trop froid ou trop chaud" in summary(f, 1.0, None)
    assert "sol trop froid ou trop chaud" not in summary(ideal, 1.0, None)


def test_history_rows_written_before_the_variable_still_read():
    cache = WeatherCache(":memory:")
    legacy = {"t_mean": 1, "t_min": 0, "t_max": 2, "rain": 5.0, "snow": 0, "et0": 1, "soil": 0.3}
    cache.put_history("c", {"2026-09-01": legacy}, "seed")
    assert cache.put_history("c", {"2026-09-02": {**legacy, "soil_t": None}}, "forecast") == 1
    assert cache.put_history("c", {"2026-09-03": {**legacy, "soil_t": 9.5}}, "forecast") == 1
    # une ligne à sept valeurs, comme avant le 9 nov. 2026
    cache.db.execute(
        "INSERT INTO history VALUES (?, ?, ?, ?)",
        ("c", "2026-08-31", "seed", "[1.0, 0.0, 2.0, 5.0, 0.0, 1.0, 0.3]"),
    )
    rows = cache.history("c", date(2026, 8, 31), date(2026, 9, 3))
    assert set(rows["2026-08-31"]) == set(HISTORY_VARS)
    assert rows["2026-08-31"]["soil_t"] is None and rows["2026-09-01"]["soil_t"] is None
    assert rows["2026-09-02"]["soil_t"] is None and rows["2026-09-03"]["soil_t"] == 9.5
    assert cache.history_days_with("c", "soil_t") == 1
    # une variable requise absente : le jour n'est toujours pas rangé
    assert cache.put_history("c", {"2026-09-04": {**legacy, "rain": None}}, "forecast") == 0


def test_normalize_tolerates_a_response_without_the_variable():
    assert SOIL_TEMP_DAILY in SERIES_VARS
    body = {
        "elevation": 600.0,
        "daily": {
            "time": ["2026-10-01", "2026-10-02"],
            "temperature_2m_mean": [10, 11],
            "temperature_2m_min": [5, 6],
            "temperature_2m_max": [15, 16],
            "rain_sum": [0, 1],
            "snowfall_sum": [0, 0],
            "et0_fao_evapotranspiration": [1, 1],
            "soil_moisture_0_to_7cm_mean": [0.3, 0.3],
        },
    }
    assert _normalize_series(body)["soil_t"] == [None, None]
    body["daily"][SOIL_TEMP_DAILY] = [8.0, None]
    assert _normalize_series(body)["soil_t"] == [8.0, None]


@pytest.mark.asyncio
async def test_series_keep_gaps_for_the_optional_variable_only(grid):
    """Amorçage d'avant la variable, prévision avec : la série porte des `None` au début, les
    espèces sans `soil_temp` calculent, une espèce avec refuse tant que la collecte est trop
    récente."""
    from test_weather_api import CELL, _cell

    sc = Scenario(elevation=600.0)
    client = make_client(sc)
    seed_from_scenario(client.cache, sc, [CELL])  # l'amorçage n'a pas `soil_t`
    raw = (await client.fetch_cells([_cell(grid)]))[CELL]
    s = build_series(raw)
    # `past_days` ne demande que les jours absents de l'historique : avec un amorçage complet, la
    # variable n'arrive qu'un jour par jour (hier, aujourd'hui, la prévision), d'où la règle
    # « activer après window_days + persistence.days jours de collecte »
    assert s.soil_temp is not None and s.soil_temp[0] is None
    assert s.soil_temp[s.i(TODAY)] == 11.0 and s.soil_temp[s.i(TODAY + timedelta(days=7))] == 11.0
    assert s.soil_temp[s.i(TODAY - timedelta(days=91))] is None
    resp = cell_weather(_cell(grid), raw, ["boletus_edulis"], TODAY)
    assert resp["species"]["boletus_edulis"]
    # une espèce qui emploie le facteur : la fenêtre de 10 jours tombe dans la collecte trop récente
    sp = _species_with_soil_temp()
    with pytest.raises(SeriesGapError, match="collecte trop récente"):
        weather_index(sp, s, TODAY, 600.0)
    # une fois la collecte faite (ici en remplissant la série), le facteur se calcule
    s.soil_temp = [11.0 if v is None else v for v in s.soil_temp]
    assert weather_index(sp, s, TODAY, 600.0)["soil_temp"] == 1.0


@pytest.mark.asyncio
async def test_a_forecast_without_the_variable_still_serves(grid):
    """Une réponse rejouée d'avant le 9 nov. 2026 (cache) n'a pas la clé : `None` partout."""
    from test_weather_api import CELL, _cell

    client = make_client(Scenario(elevation=600.0, without_soil_t=True))
    raw: CellRaw = (await client.fetch_cells([_cell(grid)]))[CELL]
    s = build_series(raw)
    assert all(v is None for v in s.soil_temp)
    assert cell_weather(_cell(grid), raw, ["boletus_edulis"], TODAY)["species"]["boletus_edulis"]
