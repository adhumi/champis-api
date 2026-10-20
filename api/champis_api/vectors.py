"""Génère shared/vectors/*.json — le contrat de parité JS/Swift (skill scoring-parity).

uv run champis-vectors            # écrit dans shared/vectors/
uv run champis-vectors --check    # vérifie que les fichiers sont à jour (CI)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from champis_pipeline import SCORING_VERSION
from champis_pipeline.scoring import trapezoid
from champis_pipeline.species import (
    ROOT,
    all_species_hash,
    list_species,
    load_palette,
    load_species,
)

from .scoring import (
    DailySeries,
    group_score,
    palette_class,
    phenology_window,
    required_series_days,
    weather_index,
)

OUT_DIR = ROOT / "shared" / "vectors"
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
    return s


def m_vectors() -> list[dict[str, Any]]:
    out = []
    cases = [
        ("ideal", date(2026, 10, 13), 700.0),
        ("best_event_not_last", date(2026, 10, 15), 700.0),
        ("snow", date(2026, 10, 15), 700.0),
        ("frost_altitude", date(2026, 10, 20), 700.0),
        ("frost_altitude", date(2026, 10, 20), 1450.0),
        ("drought", date(2026, 10, 11), 700.0),
        ("heat", date(2026, 9, 10), 700.0),
        ("crash", date(2026, 10, 20), 700.0),
        ("crash", date(2026, 11, 5), 700.0),
    ]
    for sid in list_species():
        sp = load_species(sid)
        for kind, day, alt in cases:
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
            )
            r = weather_index(sp, s, day, alt)
            lo, hi = 0, len(s.dates)
            out.append(
                {
                    "species": sid,
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
    pal = load_palette()
    xs = [0.0, 0.1499, 0.15, 0.3499, 0.35, 0.5499, 0.55, 0.7499, 0.75, 1.0]
    return [{"score": x, "expected": palette_class(x, pal)} for x in xs]


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
            print(f"✓ {path} ({len(content['cases'])} cas)")
    if stale:
        print(
            "vecteurs périmés : " + ", ".join(stale) + " — relancer `uv run champis-vectors`",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
