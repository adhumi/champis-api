"""Les vecteurs publiés doivent correspondre au code (sinon : `uv run champis-vectors`)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from champis_api import interp
from champis_api.scoring import (
    DailySeries,
    group_score,
    palette_class,
    palette_color,
    phenology_window,
    weather_index,
)
from champis_api.vectors import OUT_DIR, build
from champis_pipeline.scoring import trapezoid
from champis_pipeline.species import load_palette, load_species

TOL = 1e-6


def _load(name: str) -> dict:
    p = OUT_DIR / name
    if not p.exists():
        pytest.skip(f"{name} absent : lancer `uv run champis-vectors`")
    return json.loads(p.read_text(encoding="utf-8"))


def test_vectors_up_to_date():
    for name, content in build().items():
        on_disk = _load(name)
        assert on_disk == json.loads(json.dumps(content)), f"{name} périmé"


def test_trapezoid_vectors():
    for c in _load("trapezoid.json")["cases"]:
        assert abs(trapezoid(c["x"], c["t"]) - c["expected"]) <= TOL


def test_phi_vectors():
    for c in _load("phi.json")["cases"]:
        got = phenology_window(
            load_species(c["species"]), date.fromisoformat(c["date"]), c["alt_m"]
        )
        assert abs(got - c["expected"]) <= TOL, c


def test_m_vectors():
    for c in _load("m.json")["cases"]:
        ser = c["series"]
        s = DailySeries(
            dates=[date.fromisoformat(d) for d in ser["dates"]],
            t_mean=ser["t_mean"],
            t_min=ser["t_min"],
            t_max=ser["t_max"],
            rain=ser["rain"],
            snow=ser["snow"],
            et0=ser["et0"],
            soil_moisture_pct=ser["soil_moisture_pct"],
            cell_elevation_m=c["cell_elevation_m"],
            soil_temp=ser.get("soil_temp"),
        )
        # `species_override` : fichier espèce embarqué dans le cas (facteur qu'aucune espèce
        # publiée n'emploie encore, 9 nov. 2026), sinon la ressource du référentiel
        sp = c.get("species_override") or load_species(c["species"])
        got = weather_index(sp, s, date.fromisoformat(c["day"]), c["alt_m"])
        for k, v in c["expected"].items():
            assert abs(got[k] - v) <= TOL, (c["species"], c["scenario"], k)


def test_group_and_palette_vectors():
    for c in _load("group.json")["cases"]:
        assert group_score(c["scores"]) == c["expected"]
    pal = load_palette()
    for c in _load("palette.json")["cases"]:
        cls = palette_class(c["score"], pal)
        assert {**cls, "rgba": list(palette_color(c["score"], pal))} == c["expected"]


def test_interp_vectors():
    for c in _load("interp.json")["cases"]:
        corners = interp.corners(c["lat"], c["lon"])
        exp = c["expected"]
        assert corners.containing == exp["containing"] and list(corners.ids) == exp["ids"], c
        assert all(abs(w - e) <= TOL for w, e in zip(corners.weights, exp["weights"], strict=True))
        got = interp.interpolate(corners, c["values"])
        if exp["value"] is None:
            assert got is None, c
        else:
            assert abs(got - exp["value"]) <= TOL, c


def test_alt_vectors():
    for c in _load("alt.json")["cases"]:
        alt = interp.elevation(c["index"], c["step_m"])
        assert alt == c["expected"]["alt_m"], c
        got = interp.between_classes(alt, {float(k): v for k, v in c["values"].items()})
        if c["expected"]["value"] is None:
            assert got is None, c
        else:
            assert abs(got - c["expected"]["value"]) <= TOL, c
