"""Phrases en clair de `GET /weather` › `summary` (refonte UX, lot 2) et âge de la pluie
déclenchante : seuils génériques, jamais une valeur d'espèce ; une seule source pour les clients."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from champis_api.explain import ago, rain_phrase, summary
from champis_api.scoring import factor_trigger, season_phase, trigger_event
from champis_pipeline.species import load_species
from conftest import TODAY, make_series

EDULIS = load_species("boletus_edulis")


def _factors(**over):
    base = {
        "temp": 1.0,
        "rain": 1.0,
        "trigger": 1.0,
        "soil_moisture": 0.8,
        "frost": 1.0,
        "water_balance": 1.0,
        "heat": 1.0,
        "memory": 1.0,
    }
    return base | over


LAG = EDULIS["weather"]["trigger"]["lag_days"]  # lu dans le fichier d'espèce, jamais écrit ici


def test_summary_orders_rain_soil_brakes_season():
    assert summary(_factors(), 1.0, 12) == [
        "grosse pluie il y a 12 jours",
        "sol bien humide",
        "pleine saison",
    ]
    assert summary(_factors(trigger=0.0), 1.0, None) == [
        "pas de grosse pluie récente",
        "sol bien humide",
        "pleine saison",
    ]
    assert summary(_factors(trigger=0.4), 0.5, 3)[-1] == "en marge de la saison"  # sans phase
    assert summary(_factors(soil_moisture=0.5), 1.0, 0)[1] == "sol moyennement humide"
    assert summary(_factors(soil_moisture=0.1), 1.0, 1) == [
        "grosse pluie hier",
        "sol sec",
        "pleine saison",
    ]


def test_rain_phrase_says_too_early_on_time_or_fading_from_the_species_lag():
    """Étape B2 (spec › Produit, 6 nov. 2026) : le sens du délai de pousse, d'après `lag_days` de
    l'espèce ; une pluie toute fraîche dit quand la pousse est attendue."""
    low, high = round(LAG["opt_low"]), round(LAG["opt_high"])
    early = rain_phrase(low - 4, LAG)
    assert (
        early
        == f"grosse pluie il y a {low - 4} jours : trop tôt, pousse attendue dans 4 à {high - low + 4} jours"
    )
    assert rain_phrase(low, LAG).endswith(": en plein dans le délai de pousse")
    assert rain_phrase(high, LAG).endswith(": en plein dans le délai de pousse")
    assert rain_phrase(high + 1, LAG).endswith(": la pousse s'essouffle")
    assert rain_phrase(0, LAG).startswith(
        "grosse pluie aujourd'hui : trop tôt, pousse attendue dans"
    )
    assert rain_phrase(None, LAG) == "pas de grosse pluie récente"
    assert rain_phrase(5, None) == "grosse pluie il y a 5 jours"


def test_summary_says_the_season_side():
    for phase, words in [
        ("debut", "début de saison"),
        ("pleine", "pleine saison"),
        ("fin", "fin de saison"),
    ]:
        assert summary(_factors(), 0.6, 12, LAG, phase)[-1] == words


def test_season_phase_follows_the_peak_and_the_altitude():
    """Mêmes dates que Φ : avant le pic, au pic, après, hors saison (fichier du cèpe de Bordeaux)."""
    year = TODAY.year
    peak_start = date.fromisoformat(f"{year}-{EDULIS['phenology']['peak']['start']}")
    peak_end = date.fromisoformat(f"{year}-{EDULIS['phenology']['peak']['end']}")
    season_end = date.fromisoformat(f"{year}-{EDULIS['phenology']['season']['end']}")
    ref = EDULIS["phenology"]["altitude_shift"]["reference_m"]
    assert season_phase(EDULIS, peak_start - timedelta(days=5), ref) == "debut"
    assert season_phase(EDULIS, peak_start + timedelta(days=5), ref) == "pleine"
    assert season_phase(EDULIS, peak_end + timedelta(days=5), ref) == "fin"
    assert season_phase(EDULIS, season_end + timedelta(days=10), ref) == "hors"
    assert season_phase(EDULIS, date(year, 1, 15), ref) == "hors"


def test_summary_names_every_active_brake():
    phrases = summary(
        _factors(frost=0.2, heat=0.5, water_balance=0.4, memory=0.3, temp=0.1), 1.0, 10
    )
    assert phrases == [
        "grosse pluie il y a 10 jours",
        "sol bien humide",
        "gel ces dernières nuits",
        "trop chaud ces derniers jours",
        "le sol sèche, plus d'évaporation que de pluie",
        "peu de pluie ces derniers mois",
        "températures défavorables",
        "pleine saison",
    ]


def test_summary_out_of_season_says_only_that():
    assert summary(_factors(frost=0.2), 0.0, 5) == ["hors saison"]
    assert ago(0) == "aujourd'hui" and ago(1) == "hier" and ago(14) == "il y a 14 jours"


def test_trigger_event_age_is_the_best_event_most_recent_on_ties():
    """Règle 4 : le meilleur événement, pas le dernier ; à facteur égal, le plus récent. La date d'un
    événement est le dernier jour de sa fenêtre de `event_days` jours : une grosse pluie un jour
    donné compte pour `event_days` fins de fenêtre, la plus récente l'emporte."""
    cfg = EDULIS["weather"]["trigger"]
    lag = cfg["lag_days"]
    opt_low, window = int(lag["opt_low"]), int(cfg["event_days"])
    s = make_series(date(2026, 8, 1), 90, rain=0.0)
    day = date(2026, 10, 15)
    assert trigger_event(EDULIS, s, day) == (0.0, None)
    s.rain[s.i(day - timedelta(days=opt_low + window + 1))] = cfg["event_mm"] + 5  # dans l'optimum
    s.rain[s.i(day - timedelta(days=2))] = cfg["event_mm"] + 2  # trop récent : hors délai
    factor, age = trigger_event(EDULIS, s, day)
    assert factor == 1.0 and age == opt_low + 2  # fin de fenêtre la plus récente encore optimale
    assert factor_trigger(EDULIS, s, day) == 1.0
    # une seconde grosse pluie plus récente, encore dans l'optimum : c'est elle qui est retenue
    s.rain[s.i(day - timedelta(days=opt_low + 1))] = cfg["event_mm"] + 5
    assert trigger_event(EDULIS, s, day) == (1.0, opt_low)
    # la pluie trop récente seule : facteur partiel, âge 2
    s2 = make_series(date(2026, 8, 1), 90, rain=0.0)
    s2.rain[s2.i(day - timedelta(days=2))] = cfg["event_mm"] + 2
    f2, a2 = trigger_event(EDULIS, s2, day)
    assert 0.0 <= f2 < 1.0 and a2 is not None and a2 <= 2


@pytest.mark.asyncio
async def test_weather_detail_carries_summary_and_trigger_days_but_bbox_does_not(api):
    r = await api.get("/weather", params={"cell": "c_0930_6680", "species": "boletus_edulis"})
    assert r.status_code == 200, r.text
    row = r.json()["species"]["boletus_edulis"][3]  # J
    alt = row["by_alt"]["450"]
    assert isinstance(alt["summary"], list) and 2 <= len(alt["summary"]) <= 8
    assert alt["summary"][-1] in ("pleine saison", "en marge de la saison", "hors saison")
    assert alt["trigger_days"] is None or 0 <= alt["trigger_days"] <= 40
    assert (
        row["by_alt"]["450"]["trigger_days"] == row["by_alt"]["1350"]["trigger_days"]
    )  # ne dépend pas de l'altitude
    assert row["date"] == TODAY.isoformat()

    r = await api.get(
        "/weather/bbox",
        params={
            "min_lon": 6.2,
            "min_lat": 47.0,
            "max_lon": 6.5,
            "max_lat": 47.3,
            "species": "boletus_edulis",
        },
    )
    cell = r.json()["cells"][0]
    assert all(
        "summary" not in a
        for row in cell["species"]["boletus_edulis"]
        for a in row["by_alt"].values()
    )
