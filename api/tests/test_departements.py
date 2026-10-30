"""Indice par département (spec › Scoring) : agrégation pure, calcul après pré-chauffe, `GET /departements`."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from champis_api.cache import WeatherCache
from champis_api.departements import aggregate, compute, shape_response
from champis_api.grid import WeatherGrid
from champis_api.warm import warm
from conftest import GRID, TODAY
from fixtures.open_meteo import Scenario

EDULIS = "boletus_edulis"
CIBARIUS = "cantharellus_cibarius"
DAYS = [TODAY + timedelta(days=k) for k in range(-3, 8)]


def _weather(cells: dict[str, dict[str, float]]) -> dict:
    """Météo factice : cellule → classe d'altitude → M_eff × Φ (constant sur les 11 jours)."""
    out = {}
    for cid, by_alt in cells.items():
        rows = {
            d.isoformat(): {alt: {"M_eff": f, "phi": 1.0} for alt, f in by_alt.items()}
            for d in DAYS
        }
        out[cid] = {EDULIS: rows, CIBARIUS: rows}
    return out


def test_aggregate_weights_by_t_sum(grid: WeatherGrid):
    """I = Σ T × M_eff × Φ / Σ T : la hêtraie à 450 m (Σ T 300) pèse trois fois la crête à 1 350 m."""
    weather = _weather({"c_0930_6680": {"450": 0.9, "1350": 0.3}, "c_0935_6680": {"150": 0.6}})
    rows = aggregate(grid, weather, [EDULIS, CIBARIUS], DAYS)
    by = {(d, s, day): (v, c) for d, s, day, v, c in rows}
    j = TODAY.isoformat()
    # Doubs, cèpe : (300 × 0,9 + 100 × 0,3 + 50 × 0,6) / 450
    assert by["25", EDULIS, j][0] == pytest.approx((270 + 30 + 30) / 450)
    assert by["25", EDULIS, j][1] == pytest.approx(1.0)
    # Jura : seule la seconde cellule compte, à 150 m
    assert by["39", EDULIS, j][0] == pytest.approx(0.6)
    assert by["39", CIBARIUS, j][0] == pytest.approx(0.6)
    # 11 jours × (25: 2 espèces, 39: 2 espèces)
    assert len(rows) == 11 * 4


def test_aggregate_excludes_cells_without_weather(grid: WeatherGrid):
    """Une cellule sans météo sort du numérateur et du dénominateur ; `coverage` le dit."""
    weather = _weather({"c_0930_6680": {"450": 0.8, "1350": 0.8}})  # c_0935_6680 indisponible
    rows = aggregate(grid, weather, [EDULIS], DAYS)
    by = {(d, s, day): (v, c) for d, s, day, v, c in rows}
    j = TODAY.isoformat()
    assert by["25", EDULIS, j][0] == pytest.approx(0.8)
    assert by["25", EDULIS, j][1] == pytest.approx(400 / 450)
    assert by["39", EDULIS, j] == (
        None,
        0.0,
    )  # aucune cellule couverte : jamais une valeur inventée


def test_shape_response_groups_take_the_max_species():
    rows = [
        ("25", EDULIS, "2026-10-17", 0.4, 1.0),
        ("25", "boletus_aereus", "2026-10-17", 0.7, 0.9),
        ("25", CIBARIUS, "2026-10-17", 0.2, 1.0),
        ("39", EDULIS, "2026-10-17", None, 0.0),
    ]
    out = shape_response(rows, [EDULIS, "boletus_aereus", CIBARIUS])
    assert out["25"]["species"][EDULIS] == [{"date": "2026-10-17", "value": 0.4, "coverage": 1.0}]
    cepes = out["25"]["groups"]["cepes"][0]
    assert (
        cepes["value"] == 0.7 and cepes["leading"] == "boletus_aereus" and cepes["coverage"] == 0.9
    )
    assert out["25"]["groups"]["girolles"][0]["leading"] == CIBARIUS
    assert out["39"]["groups"]["cepes"][0] == {
        "date": "2026-10-17",
        "value": None,
        "leading": None,
        "coverage": 0.0,
    }


@pytest.mark.asyncio
async def test_compute_after_warm_matches_weather_endpoint(api, grid: WeatherGrid, client):
    """L'indice calculé après la pré-chauffe est la moyenne pondérée par Σ T des M_eff × Φ que
    `/weather` renvoie pour les mêmes cellules, le même jour et les mêmes classes d'altitude."""
    assert (await api.get("/departements")).status_code == 503  # rien calculé avant la pré-chauffe
    report = await warm(grid, client)
    assert report.ok
    idx = await compute(grid, client, client.cache, [EDULIS, CIBARIUS])
    assert idx.departements == 2 and idx.cells == 2 and idx.cells_without_weather == 0

    r = await api.get("/departements", params={"species": EDULIS, "day": TODAY.isoformat()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["today"] == TODAY.isoformat() and body["scoring_version"] == "1.0.0"
    rows = body["departements"]["25"]["species"][EDULIS]
    assert len(rows) == 1 and rows[0]["date"] == TODAY.isoformat()

    num = den = 0.0
    for cid, by_alt in ((c["id"], c["t_sum"]["25"]) for c in GRID["cells"]):
        w = (await api.get("/weather", params={"cell": cid, "species": EDULIS})).json()
        today_row = next(r for r in w["species"][EDULIS] if r["date"] == TODAY.isoformat())
        for alt, by_sp in by_alt.items():
            if EDULIS not in by_sp:
                continue
            v = today_row["by_alt"][alt]
            num += by_sp[EDULIS] * v["M_eff"] * v["phi"]
            den += by_sp[EDULIS]
    assert rows[0]["value"] == pytest.approx(num / den, abs=1e-9)
    assert rows[0]["coverage"] == pytest.approx(1.0)

    r = await api.get("/departements")
    assert set(r.json()["departements"]) == {"25", "39"}
    assert len(r.json()["departements"]["39"]["species"][EDULIS]) == 11
    assert "cepes" in r.json()["departements"]["25"]["groups"]
    assert (await api.get("/departements", params={"species": "amanita"})).status_code == 404
    health = (await api.get("/health")).json()
    assert health["dept_index_at"] is not None


@pytest.mark.asyncio
async def test_compute_without_weights_does_nothing(tmp_path, client):
    """Grille version 1 (sans Σ T) : pas d'indice, pas d'erreur, et /departements reste en 503."""
    import json

    v1 = {"version": 1, "cell_m": 5000, "cells": [dict(c, t_sum=None) for c in GRID["cells"]]}
    for c in v1["cells"]:
        del c["t_sum"]
    p = tmp_path / "g.json"
    p.write_text(json.dumps(v1))
    grid = WeatherGrid.load(p)
    assert not grid.has_weights
    idx = await compute(grid, client, client.cache, [EDULIS])
    assert idx.rows == 0 and client.cache.dept_index_meta() is None


def test_cache_dept_index_roundtrip():
    cache = WeatherCache()
    from datetime import datetime

    rows = [("25", EDULIS, "2026-10-17", 0.5, 1.0), ("25", EDULIS, "2026-10-18", None, 0.0)]
    cache.put_dept_index(rows, date(2026, 10, 17), datetime(2026, 10, 17, 3, 20))
    assert cache.dept_index([EDULIS], date(2026, 10, 18)) == [rows[1]]
    assert cache.dept_index() == rows
    at, today = cache.dept_index_meta()
    assert today == "2026-10-17" and at.startswith("2026-10-17")
    cache.put_dept_index([], date(2026, 10, 18), datetime(2026, 10, 18, 3, 20))
    assert cache.dept_index() == []  # remplacement complet, pas d'accumulation


def test_scenario_fixture_is_available():
    assert Scenario(elevation=600.0).elevation == 600.0
