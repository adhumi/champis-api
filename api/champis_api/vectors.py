"""Génère shared/vectors/*.json — le contrat de parité JS/Swift (skill scoring-parity).

uv run champis-vectors            # écrit dans shared/vectors/
uv run champis-vectors --check    # vérifie que les fichiers sont à jour (CI)
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from champis_pipeline import SCORING_VERSION, console
from champis_pipeline.scoring import trapezoid
from champis_pipeline.species import (
    REFERENTIAL,
    all_species_hash,
    list_species,
    load_palette,
    load_species,
)

from .scoring import (
    DailySeries,
    group_score,
    palette_class,
    palette_color,
    phenology_window,
    required_series_days,
    weather_index,
)

OUT_DIR = (
    REFERENTIAL / "shared" / "vectors"
)  # dans le sous-module : à committer dans adhumi/champis
EPS = 1e-9


def _meta() -> dict[str, str]:
    return {"scoring_version": SCORING_VERSION, "species_hash": all_species_hash()}


def _r(x: float) -> float:
    return round(float(x), 9)


def trapezoid_vectors() -> list[dict[str, Any]]:
    t = {"min": 2.0, "opt_low": 4.0, "opt_high": 6.0, "max": 8.0}
    step = {"min": 0.0, "opt_low": 0.0, "opt_high": 20.0, "max": 40.0}
    xs = [1.0, 2.0 - EPS, 2.0, 2.0 + EPS, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 8.0 + EPS, 9.0]
    out = [{"x": x, "t": t, "expected": _r(trapezoid(x, t))} for x in xs]
    out += [
        {"x": x, "t": step, "expected": _r(trapezoid(x, step))}
        for x in (0.0, 20.0, 30.0, 40.0, 41.0)
    ]
    cliff = {"min": 2.0, "opt_low": 4.0, "opt_high": 8.0, "max": 8.0}
    out += [
        {"x": x, "t": cliff, "expected": _r(trapezoid(x, cliff))} for x in (7.9, 8.0, 8.0 + EPS)
    ]
    return out


def phi_vectors() -> list[dict[str, Any]]:
    out = []
    for sid in list_species():
        sp = load_species(sid)
        for alt in (100, 700, 1400):
            d = date(2026, 1, 1)
            while d.year == 2026:
                out.append(
                    {
                        "species": sid,
                        "date": d.isoformat(),
                        "alt_m": alt,
                        "expected": _r(phenology_window(sp, d, alt)),
                    }
                )
                d += timedelta(days=7)
        out.append(
            {
                "species": sid,
                "date": "2028-02-29",
                "alt_m": 500,
                "expected": _r(phenology_window(sp, date(2028, 2, 29), 500)),
            }
        )
    return out


def _scenario_series(kind: str) -> DailySeries:
    start = date(2026, 3, 1)
    n = 260
    dates = [start + timedelta(days=i) for i in range(n)]
    s = DailySeries(
        dates=dates,
        t_mean=[13.0] * n,
        t_min=[8.0] * n,
        t_max=[18.0] * n,
        rain=[2.5] * n,
        snow=[0.0] * n,
        et0=[1.5] * n,
        soil_moisture_pct=[0.6] * n,
        cell_elevation_m=700.0,
        soil_temp=[11.0]
        * n,  # facteur optionnel (9 nov. 2026) : fourni à tous, lu par qui l'emploie
    )
    i = s.i
    if kind == "ideal":
        s.rain[i(date(2026, 10, 1))] = 30.0
    elif kind == "best_event_not_last":
        for k in range(len(s.rain)):
            s.rain[k] = 0.0
        s.rain[i(date(2026, 10, 1))] = 25.0
        s.rain[i(date(2026, 10, 13))] = 22.0
    elif kind == "snow":
        for k in range(len(s.rain)):
            s.rain[k] = 0.0
        s.rain[i(date(2026, 10, 1))] = 25.0
        s.snow[i(date(2026, 10, 1))] = 3.0
    elif kind == "frost_altitude":
        for k in range(len(s.t_min)):
            s.t_min[k] = 2.0
    # gel en rampe (4 nov. 2026) : les températures sont absolues, chaque espèce les lit avec son
    # propre seuil (tmin_c −1, −2 ou −3 °C, ramp_c 2 °C, nights 1 ou 2), d'où des sévérités variées
    elif kind == "frost_one_night":
        # une seule nuit à −1 °C : mi-rampe pour un seuil à −2 °C, nuit pleine à −1 °C, rien à −3 °C
        s.t_min[i(date(2026, 10, 20))] = -1.0
    elif kind == "frost_partial_nights":
        # quatre nuits partielles qui se cumulent : S = Σ s, plafonné à `nights`
        for d, v in ((20, -1.0), (19, -0.5), (18, -1.5), (17, 0.0)):
            s.t_min[i(date(2026, 10, d))] = v
    elif kind == "frost_ramp_edges":
        # une nuit exactement à tmin_c + ramp_c (s = 0) et une juste en dessous (s ≈ 0) pour −2 °C
        s.t_min[i(date(2026, 10, 20))] = 0.0
        s.t_min[i(date(2026, 10, 19))] = 0.0 - EPS
    elif kind == "frost_window_edge":
        # deux nuits pleines : J−7 est hors des 7 derniers jours, J−6 dedans → une seule compte
        s.t_min[i(date(2026, 10, 13))] = -10.0
        s.t_min[i(date(2026, 10, 14))] = -10.0
    elif kind == "drought":
        for k in range(len(s.rain)):
            s.rain[k] = 0.0
            s.et0[k] = 4.0
            s.soil_moisture_pct[k] = 0.05
    elif kind == "heat":
        for k in range(len(s.t_max)):
            s.t_max[k] = 31.0
    elif kind == "crash":
        s.rain[i(date(2026, 10, 1))] = 30.0
        for k in range(i(date(2026, 10, 16)), n):
            s.t_mean[k] = s.rain[k] = s.soil_moisture_pct[k] = 0.0
    # température du sol (facteur optionnel, 9 nov. 2026) : sol froid, puis sol qui se réchauffe
    # sur la fenêtre (la moyenne tombe entre min et opt_low), air inchangé pour isoler le facteur
    elif kind == "soil_temp_cold":
        s.rain[i(date(2026, 10, 1))] = 30.0
        for k in range(len(s.soil_temp)):
            s.soil_temp[k] = 3.0
    elif kind == "soil_temp_warming":
        s.rain[i(date(2026, 10, 1))] = 30.0
        for k, d in enumerate(s.dates):
            s.soil_temp[k] = 3.0 if d < date(2026, 10, 9) else 9.0
    return s


# Espèce fictive à fichier embarqué (`species_override`) : le cèpe de Bordeaux avec le facteur
# optionnel `soil_temp`, qu'aucune espèce publiée n'emploie encore. Le client rejoue le cas avec
# ce fichier plutôt qu'avec sa ressource `boletus_edulis.json`.
SOIL_TEMP_OVERRIDE = {
    "soil_temp": {
        "window_days": 10,
        "c": {"min": 4.0, "opt_low": 8.0, "opt_high": 15.0, "max": 20.0},
    },
    "weights": {"temp": 0.15, "rain": 0.3, "trigger": 0.25, "soil_moisture": 0.1, "soil_temp": 0.2},
}


def _soil_temp_species() -> dict[str, Any]:
    sp = copy.deepcopy(load_species("boletus_edulis"))
    sp.pop("about", None)
    sp["id"] = "boletus_edulis+soil_temp"
    sp["weather"] = {**sp["weather"], **copy.deepcopy(SOIL_TEMP_OVERRIDE)}
    return sp


def m_vectors() -> list[dict[str, Any]]:
    out = []
    cases = [
        ("ideal", date(2026, 10, 13), 700.0),
        ("best_event_not_last", date(2026, 10, 15), 700.0),
        ("snow", date(2026, 10, 15), 700.0),
        ("frost_altitude", date(2026, 10, 20), 700.0),
        ("frost_altitude", date(2026, 10, 20), 1450.0),
        ("frost_one_night", date(2026, 10, 20), 700.0),
        ("frost_one_night", date(2026, 10, 20), 850.0),
        ("frost_partial_nights", date(2026, 10, 20), 700.0),
        ("frost_ramp_edges", date(2026, 10, 20), 700.0),
        ("frost_window_edge", date(2026, 10, 20), 700.0),
        ("drought", date(2026, 10, 11), 700.0),
        ("heat", date(2026, 9, 10), 700.0),
        ("crash", date(2026, 10, 20), 700.0),
        ("crash", date(2026, 11, 5), 700.0),
    ]
    soil_cases = [
        ("ideal", date(2026, 10, 13), 700.0),  # 11 °C dans l'optimum → facteur 1
        ("ideal", date(2026, 10, 13), 1450.0),  # 11 − 4,5 °C = 6,5 : sur la rampe → 0,625
        ("soil_temp_cold", date(2026, 10, 13), 700.0),  # 3 °C < min → 0
        ("soil_temp_warming", date(2026, 10, 13), 700.0),  # moyenne 10 j = 6 °C → 0,5
    ]
    runs = [(sid, load_species(sid), cases, None) for sid in list_species()]
    runs.append(("boletus_edulis", _soil_temp_species(), soil_cases, _soil_temp_species()))
    for sid, sp, sp_cases, override in runs:
        for kind, day, alt in sp_cases:
            full = _scenario_series(kind)
            a = full.i(day - timedelta(days=required_series_days(sp) + 1))
            b = full.i(day) + 1
            s = DailySeries(
                dates=full.dates[a:b],
                t_mean=full.t_mean[a:b],
                t_min=full.t_min[a:b],
                t_max=full.t_max[a:b],
                rain=full.rain[a:b],
                snow=full.snow[a:b],
                et0=full.et0[a:b],
                soil_moisture_pct=full.soil_moisture_pct[a:b],
                cell_elevation_m=full.cell_elevation_m,
                soil_temp=full.soil_temp[a:b],  # type: ignore[index]
            )
            r = weather_index(sp, s, day, alt)
            lo, hi = 0, len(s.dates)
            out.append(
                {
                    "species": sid,
                    **({"species_override": override} if override else {}),
                    "scenario": kind,
                    "day": day.isoformat(),
                    "alt_m": alt,
                    "cell_elevation_m": s.cell_elevation_m,
                    "series": {
                        "dates": [d.isoformat() for d in s.dates[lo:hi]],
                        "t_mean": s.t_mean[lo:hi],
                        "t_min": s.t_min[lo:hi],
                        "t_max": s.t_max[lo:hi],
                        "rain": s.rain[lo:hi],
                        "snow": s.snow[lo:hi],
                        "et0": s.et0[lo:hi],
                        "soil_moisture_pct": s.soil_moisture_pct[lo:hi],
                        "soil_temp": s.soil_temp[lo:hi],  # type: ignore[index]
                    },
                    "expected": {k: _r(v) for k, v in r.items()},
                }
            )
    return out


def group_vectors() -> list[dict[str, Any]]:
    cases = [
        {"boletus_edulis": 0.6, "boletus_aereus": 0.6, "boletus_pinophilus": 0.2},
        {"boletus_edulis": 0.1, "boletus_reticulatus": 0.45},
        {
            "cantharellus_cibarius": 0.0,
            "cantharellus_pallens": 0.0,
            "cantharellus_alborufescens": 0.0,
        },
        {},
    ]
    return [{"scores": c, "expected": group_score(c)} for c in cases]


def palette_vectors() -> list[dict[str, Any]]:
    """Mots aux seuils exacts et ±ε, et couleur du pixel (échelle linéaire) aux bornes, aux
    seuils et sur des demi-niveaux d'arrondi de l'alpha (0,5/255, 1,5/255…), où `round` et
    `floor(x + 0.5)` divergent ; hors de [0, 1] pour vérifier la borne."""
    pal = load_palette()
    xs = [0.0, 0.1499, 0.15, 0.3499, 0.35, 0.5499, 0.55, 0.7499, 0.75, 1.0]
    xs += [0.5 / 255, 1.5 / 255, 0.1, 0.25, 0.45, 0.5, 0.65, 0.875, 0.999, -0.1, 1.2]
    return [
        {"score": x, "expected": {**palette_class(x, pal), "rgba": list(palette_color(x, pal))}}
        for x in xs
    ]


def build() -> dict[str, dict[str, Any]]:
    meta = _meta()
    return {
        "trapezoid.json": {"generated_by": meta, "cases": trapezoid_vectors()},
        "phi.json": {"generated_by": meta, "cases": phi_vectors()},
        "m.json": {"generated_by": meta, "cases": m_vectors()},
        "group.json": {"generated_by": meta, "cases": group_vectors()},
        "palette.json": {"generated_by": meta, "cases": palette_vectors()},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    ap.add_argument("--check", action="store_true", help="échoue si les fichiers diffèrent")
    args = ap.parse_args(argv)
    c = console.get()
    files = build()
    args.out.mkdir(parents=True, exist_ok=True)
    stale = []
    for name, content in files.items():
        text = json.dumps(content, ensure_ascii=False, indent=1) + "\n"
        path = args.out / name
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            path.write_text(text, encoding="utf-8")
            c.write(f"ok  {path} ({len(content['cases'])} cas)")
    if stale:
        c.error("vecteurs périmés : " + ", ".join(stale), hint="relancer `uv run champis-vectors`")
        return 1
    if args.check:
        c.write(f"ok  {len(files)} fichiers de vecteurs à jour dans {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
