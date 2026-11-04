"""Règles de la spec et du schéma que la suite ne couvrait pas encore.

Chaque test cite sa règle : skill `champis-model` (numéros), `species/SCHEMA.md`, ou api/README
(contrats des endpoints). Les données viennent des fichiers espèces, jamais du test.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import pytest
from champis_api.scoring import (
    factor_heat,
    factor_memory,
    required_series_days,
    weather_index,
    weather_index_raw,
)
from champis_pipeline.species import (
    ROOT,
    all_species_hash,
    list_species,
    load_palette,
    load_species,
)
from conftest import TODAY, make_series

EDULIS = load_species("boletus_edulis")
USER = str(uuid.uuid4())
H = {"X-Champis-User": USER}


# ------------------------------------------------------------------ Modèle (skill champis-model)


def test_heat_factor_applies_only_with_enough_hot_days_and_follows_altitude_correction():
    """SCHEMA › `heat` : ≥ `days` jours ≥ `tmax_c` dans `window_days` → × `factor` ; règle 5 : les
    températures sont corrigées de −0,6 °C/100 m, donc la chaleur de plaine disparaît en altitude."""
    cfg = EDULIS["weather"]["heat"]
    day = date(2026, 8, 20)
    s = make_series(date(2026, 3, 1), 200, t_max=cfg["tmax_c"] + 1.0, elevation=300.0)
    assert factor_heat(EDULIS, s, day, 300.0) == cfg["factor"]
    # 600 m au-dessus de la cellule : −3,6 °C, plus aucun jour chaud
    assert factor_heat(EDULIS, s, day, 900.0) == 1.0
    # un jour de moins que `days` : pas de pénalité
    s = make_series(date(2026, 3, 1), 200, t_max=cfg["tmax_c"] - 5.0, elevation=300.0)
    for k in range(cfg["days"] - 1):
        s.t_max[s.i(day - timedelta(days=k))] = cfg["tmax_c"] + 2.0
    assert factor_heat(EDULIS, s, day, 300.0) == 1.0
    s.t_max[s.i(day - timedelta(days=cfg["days"] - 1))] = cfg["tmax_c"] + 2.0
    assert factor_heat(EDULIS, s, day, 300.0) == cfg["factor"]


def test_memory_modulates_between_floor_and_one_never_zero():
    """SCHEMA › `memory` : × (floor + (1 − floor) × trapèze) ; un été sec module, n'annule jamais."""
    cfg = EDULIS["weather"]["memory"]
    day = date(2026, 10, 15)
    dry = make_series(date(2026, 3, 1), 260, rain=0.0)
    assert factor_memory(EDULIS, dry, day) == pytest.approx(cfg["floor"])
    wet = make_series(date(2026, 3, 1), 260, rain=cfg["mm"]["opt_low"] / cfg["window_days"] * 1.5)
    assert factor_memory(EDULIS, wet, day) == pytest.approx(1.0)
    assert 0 < cfg["floor"] < 1


def test_m_eff_persistence_then_frost_and_water_balance_reapplied():
    """Règle 3 : M_eff(J) = max(M(J), max_k M(J−k) × (1 − k/days)), puis gel et bilan hydrique
    réappliqués sur M_eff (les sporophores existants gèlent)."""
    frost = EDULIS["weather"]["frost"]
    days = EDULIS["weather"]["persistence"]["days"]
    day = date(2026, 10, 15)
    s = make_series(date(2026, 3, 1), 240, rain=0.0, t_min=8.0, elevation=300.0)
    # grosse pluie il y a 12 j : M(J−k) élevé pour les k récents, M(J) modéré
    s.rain[s.i(day - timedelta(days=12))] = 40.0
    raw_today = weather_index_raw(EDULIS, s, day, 300.0)["M"]
    expected = raw_today
    for k in range(1, days + 1):
        expected = max(
            expected,
            weather_index_raw(EDULIS, s, day - timedelta(days=k), 300.0)["M"] * (1 - k / days),
        )
    calm = weather_index(EDULIS, s, day, 300.0)
    assert calm["M_eff"] == pytest.approx(expected) and calm["frost"] == 1.0
    assert calm["M_eff"] >= calm["M"]

    # gel les `nights` dernières nuits : M_eff est multiplié par le facteur gel, même si M(J−k) était bon
    for k in range(frost["nights"]):
        s.t_min[s.i(day - timedelta(days=k))] = frost["tmin_c"] - 1.0
    frozen = weather_index(EDULIS, s, day, 300.0)
    assert frozen["frost"] == frost["factor"]
    assert frozen["M_eff"] == pytest.approx(min(1.0, expected_after_frost(s, day, days)))


def expected_after_frost(s, day, days):
    """Persistance recalculée sur la série gelée, puis × gel × bilan hydrique du jour."""
    today = weather_index_raw(EDULIS, s, day, 300.0)
    m_eff = today["M"]
    for k in range(1, days + 1):
        m_eff = max(
            m_eff,
            weather_index_raw(EDULIS, s, day - timedelta(days=k), 300.0)["M"] * (1 - k / days),
        )
    return m_eff * today["frost"] * today["water_balance"]


def test_required_series_days_covers_memory_rain_and_persistence():
    """SCHEMA : la série doit couvrir memory.window + rain.window + persistence.days avant J."""
    w = EDULIS["weather"]
    assert (
        required_series_days(EDULIS)
        >= w["memory"]["window_days"] + w["rain"]["window_days"] + w["persistence"]["days"]
    )
    for sid in list_species():
        assert required_series_days(load_species(sid)) <= 200  # l'amorçage J−200 suffit à toutes


# ------------------------------------------------------------------ API › contrats (api/README)


@pytest.mark.asyncio
async def test_health_and_species_contract(api):
    """`GET /health` : status, cells, seeded, dept_index_at ; `GET /species` : scoring_version,
    species_hash, palette et chaque fichier espèce tel quel (les clients n'embarquent rien)."""
    h = (await api.get("/health")).json()
    assert set(h) == {"status", "cells", "seeded", "dept_index_at", "cache_published"}
    assert (
        h["status"] == "ok" and h["cells"] == 2 and h["seeded"] == 2 and h["dept_index_at"] is None
    )

    body = (await api.get("/species")).json()
    assert set(body) == {"scoring_version", "species_hash", "palette", "species"}
    assert body["species_hash"] == all_species_hash()
    assert body["palette"] == load_palette()
    assert sorted(body["species"]) == list_species()
    for sid in list_species():
        on_disk = json.loads((ROOT / "species" / f"{sid}.json").read_text(encoding="utf-8"))
        assert body["species"][sid] == on_disk, sid


@pytest.mark.asyncio
async def test_weather_covers_eleven_days_per_altitude_class_with_eight_factors(api):
    """`GET /weather` : J−3 → J+7 pour chaque classe d'altitude de la cellule, M, M_eff, phi et les
    huit facteurs ; `/weather/bbox` : mêmes lignes sans `factors`."""
    r = await api.get("/weather", params={"cell": "c_0930_6680", "species": "boletus_edulis"})
    assert r.status_code == 200, r.text
    rows = r.json()["species"]["boletus_edulis"]
    dates = [row["date"] for row in rows]
    assert (
        dates[0] == (TODAY - timedelta(days=3)).isoformat()
        and dates[-1] == (TODAY + timedelta(days=7)).isoformat()
    )
    assert len(dates) == 11
    for row in rows:
        assert set(row["by_alt"]) == {"450", "1350"}
        for alt in row["by_alt"].values():
            assert set(alt) >= {"M", "M_eff", "phi", "factors", "summary", "trigger_days"}
            assert isinstance(alt["summary"], list) and all(
                isinstance(p, str) for p in alt["summary"]
            )
            assert alt["trigger_days"] is None or isinstance(alt["trigger_days"], int)
            assert set(alt["factors"]) == {
                "temp",
                "rain",
                "trigger",
                "soil_moisture",
                "frost",
                "water_balance",
                "heat",
                "memory",
            }
            assert 0 <= alt["M_eff"] <= 1 and 0 <= alt["phi"] <= 1

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
    assert r.status_code == 200, r.text
    cells = r.json()["cells"]
    assert len(cells) == 2
    for cell in cells:
        assert set(cell["cell"]) >= {"id", "lat", "lon", "alt_classes", "elevation_m"}
        for row in cell["species"]["boletus_edulis"]:
            for alt in row["by_alt"].values():
                assert "factors" not in alt and "summary" not in alt and "trigger_days" not in alt
                assert set(alt) >= {"M", "M_eff", "phi"}


@pytest.mark.asyncio
async def test_observations_batch_limit_and_bounds(api):
    """api/README : lots ≤ 200, France métropolitaine, quantité 1–3 ; au-delà, 422 sans rien écrire."""

    def obs(**over):
        return {
            "id": str(uuid.uuid4()),
            "species": "boletus_edulis",
            "lat": 47.12,
            "lon": 6.31,
            "date": TODAY.isoformat(),
            "found": True,
            "quantity": 2,
        } | over

    r = await api.post(
        "/observations", json={"observations": [obs() for _ in range(201)]}, headers=H
    )
    assert r.status_code == 422
    r = await api.post("/observations", json={"observations": []}, headers=H)
    assert r.status_code == 422
    for bad in (obs(lat=52.0), obs(lon=-6.0), obs(quantity=4), obs(accuracy_m=-1)):
        assert (
            await api.post("/observations", json={"observations": [bad]}, headers=H)
        ).status_code == 422
    assert (await api.get("/observations", headers=H)).json()["count"] == 0
    r = await api.post(
        "/observations", json={"observations": [obs() for _ in range(200)]}, headers=H
    )
    assert r.status_code == 201 and r.json()["inserted"] == 200


@pytest.mark.asyncio
async def test_coins_limit_target_and_full_replacement(api):
    """api/README › `PUT /coins` : ≤ 50, `target` = espèce ou groupe connu, `t` ⊂ espèces dans [0, 1] ;
    la liste complète remplace la précédente (le serveur ne fusionne pas)."""

    def coin(**over):
        return {
            "id": str(uuid.uuid4()),
            "name": "Combe",
            "lat": 47.12,
            "lon": 6.31,
            "target": "cepes",
            "alt_class": 450,
            "t": {"boletus_edulis": 0.8},
        } | over

    assert (
        await api.put("/coins", json={"coins": [coin() for _ in range(51)]}, headers=H)
    ).status_code == 422
    for bad in (
        coin(target="truffes"),
        coin(t={"amanita": 0.5}),
        coin(t={"boletus_edulis": 1.5}),
        coin(t={}),
        coin(name=""),
        coin(alt_class=-1),
    ):
        assert (await api.put("/coins", json={"coins": [bad]}, headers=H)).status_code == 422, bad
    assert (
        await api.put("/coins", json={"apns_token": "zz", "coins": []}, headers=H)
    ).status_code == 422

    first = [
        coin(name="A"),
        coin(name="B", target="boletus_edulis"),
    ]  # espèce comme cible : accepté
    r = await api.put("/coins", json={"coins": first}, headers=H)
    assert r.status_code == 200 and r.json()["coins"] == 2 and r.json()["notifications"] is False
    r = await api.put("/coins", json={"coins": [coin(name="C")]}, headers=H)
    assert r.json()["coins"] == 1
    names = [c["name"] for c in (await api.get("/coins", headers=H)).json()["coins"]]
    assert names == ["C"]  # remplacée, pas fusionnée
    other = {"X-Champis-User": str(uuid.uuid4())}
    assert (await api.get("/coins", headers=other)).json()["coins"] == []


@pytest.mark.asyncio
async def test_departements_day_filter_and_group_leading(api, grid, client):
    """api/README › `GET /departements` : 503 avant la première pré-chauffe ; `day` filtre un jour ;
    groupe = max des espèces avec `leading` (règle 9) ; `coverage` ∈ [0, 1]."""
    from champis_api.departements import compute

    assert (await api.get("/departements")).status_code == 503
    await compute(grid, client, client.cache, list_species())
    r = await api.get("/departements", params={"day": TODAY.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["today"] == TODAY.isoformat() and set(body["departements"]) == {"25", "39"}
    doubs = body["departements"]["25"]
    assert [d["date"] for d in doubs["species"]["boletus_edulis"]] == [TODAY.isoformat()]
    cepes = doubs["groups"]["cepes"][0]
    members = [
        doubs["species"][s][0]["value"]
        for s in doubs["species"]
        if load_species(s)["group"]["id"] == "cepes"
    ]
    assert cepes["value"] == pytest.approx(max(v for v in members if v is not None))
    assert load_species(cepes["leading"])["group"]["id"] == "cepes"
    assert 0 <= cepes["coverage"] <= 1
    full = (await api.get("/departements", params={"species": "boletus_edulis"})).json()
    assert len(full["departements"]["25"]["species"]["boletus_edulis"]) == 11  # J−3 → J+7
    assert set(full["departements"]["25"]["species"]) == {"boletus_edulis"}
