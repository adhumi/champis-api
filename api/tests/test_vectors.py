"""Les vecteurs publiés doivent correspondre au code (sinon : `uv run champis-vectors`)."""

from __future__ import annotations

import json
from datetime import date

import pytest
from champis_api.scoring import (
    DailySeries,
    group_score,
    palette_class,
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
        )
        got = weather_index(load_species(c["species"]), s, date.fromisoformat(c["day"]), c["alt_m"])
        for k, v in c["expected"].items():
            assert abs(got[k] - v) <= TOL, (c["species"], c["scenario"], k)


def test_group_and_palette_vectors():
    for c in _load("group.json")["cases"]:
        assert group_score(c["scores"]) == c["expected"]
    pal = load_palette()
    for c in _load("palette.json")["cases"]:
        assert palette_class(c["score"], pal) == c["expected"]
