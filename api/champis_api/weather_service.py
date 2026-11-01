"""Assemble les séries d'une cellule et calcule M, M_eff et Φ par espèce, jour et classe d'altitude.

Réponse `/weather` (skill open-meteo) : pour chaque jour de J−3 à J+7, par classe d'altitude de la
cellule, `M`, `M_eff`, `phi` et le détail des facteurs — la fiche « pourquoi c'est vert » en dépend.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from champis_pipeline import SCORING_VERSION
from champis_pipeline.species import all_species_hash, load_species

from .explain import summary
from .grid import Cell
from .scoring import (
    DailySeries,
    phenology_window,
    required_series_days,
    trigger_event,
    weather_index,
)
from .weather_client import CellRaw, soil_percentile

DAYS_BEFORE = 3
DAYS_AFTER = 7


class HistoryTooShortError(Exception):
    """Pas assez de jours avant J pour la fenêtre mémoire : indisponible jusqu'à `complete_on`
    (l'accumulation des prévisions comble un jour par jour), jamais une fenêtre tronquée."""

    def __init__(self, species_id: str, need: int, available: int, today: date):
        self.complete_on = today + timedelta(days=need - available)
        super().__init__(
            f"{species_id} : historique de {available} j, {need} j requis ; "
            f"complet vers le {self.complete_on.isoformat()}"
        )


class SeriesGapError(Exception):
    """Trou ou troncature dans les séries : jamais une approximation silencieuse (SCHEMA.md)."""


VARS = ("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil")
OPEN_METEO_NAMES = {
    "t_mean": "temperature_2m_mean",
    "t_min": "temperature_2m_min",
    "t_max": "temperature_2m_max",
    "rain": "rain_sum",
    "snow": "snowfall_sum",
    "et0": "et0_fao_evapotranspiration",
    "soil": "soil_moisture",
}


def build_series(raw: CellRaw) -> DailySeries:
    """Fusion par variable et par jour : prévision si présente, sinon historique accumulé.

    La prévision couvre ~J−60 → J+7 ; avant, l'historique (jours de prévision passés accumulés,
    amorçage ERA5-Land pour les plus anciens) prend le relais : mémoire estivale, bout de fenêtre
    pluie des girolles. Une valeur absente des deux sources est une erreur (SCHEMA.md : jamais une
    approximation silencieuse), qui nomme la variable et les dates.
    """
    f, h = raw.forecast, raw.history
    f_idx = {d: i for i, d in enumerate(f["dates"])}
    first = min([f["dates"][0], *h]) if h else f["dates"][0]
    dates = _date_range(date.fromisoformat(first), date.fromisoformat(f["dates"][-1]))
    merged: dict[str, list[float]] = {}
    for var in VARS:
        values, missing = [], []
        for d in dates:
            k = d.isoformat()
            v = f[var][f_idx[k]] if k in f_idx else None
            if v is None and k in h:
                v = h[k].get(var)
            if v is None:
                missing.append(k)
            values.append(v)
        if missing:
            raise SeriesGapError(f"`{OPEN_METEO_NAMES[var]}` manquant sur {_summary(missing)}")
        merged[var] = values
    soil_pct = [
        soil_percentile(v, raw.soil_clim, d) for v, d in zip(merged["soil"], dates, strict=True)
    ]
    return DailySeries(
        dates=dates,
        t_mean=[float(v) for v in merged["t_mean"]],
        t_min=[float(v) for v in merged["t_min"]],
        t_max=[float(v) for v in merged["t_max"]],
        rain=[float(v) for v in merged["rain"]],
        snow=[float(v) for v in merged["snow"]],
        et0=[float(v) for v in merged["et0"]],
        soil_moisture_pct=[float(v) for v in soil_pct],  # type: ignore[arg-type]
        cell_elevation_m=float(f["elevation"]),
    )


def _date_range(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def _summary(dates: list[str]) -> str:
    if len(dates) <= 3:
        return ", ".join(dates)
    return f"{len(dates)} jours ({dates[0]} → {dates[-1]})"


def days_around(today: date) -> list[date]:
    return [today + timedelta(days=k) for k in range(-DAYS_BEFORE, DAYS_AFTER + 1)]


def cell_weather(
    cell: Cell, raw: CellRaw, species_ids: list[str], today: date, detail: bool = True
) -> dict[str, Any]:
    s = build_series(raw)
    days = days_around(today)
    out_species: dict[str, Any] = {}
    for sid in species_ids:
        sp = load_species(sid)
        need = required_series_days(sp)
        if s.i(days[0]) < need:
            raise HistoryTooShortError(sid, need, s.i(days[0]), today)
        day_rows = []
        for d in days:
            by_alt = {}
            trigger_days = trigger_event(sp, s, d)[1] if detail else None
            for alt in cell.alt_classes:
                w = weather_index(sp, s, d, alt)
                phi = phenology_window(sp, d, alt)
                row = {
                    "M": w["M"],
                    "M_eff": w["M_eff"],
                    "phi": phi,
                }
                if detail:
                    factors = {k: v for k, v in w.items() if k not in ("M", "M_eff")}
                    row["factors"] = factors
                    # phrases en clair et âge de la pluie déclenchante (refonte UX, lot 2)
                    row["trigger_days"] = trigger_days
                    row["summary"] = summary(factors, phi, trigger_days)
                by_alt[str(int(alt))] = row
            day_rows.append({"date": d.isoformat(), "by_alt": by_alt})
        out_species[sid] = day_rows
    return {
        "cell": cell.as_dict(),
        "cell_elevation_m": s.cell_elevation_m,
        "today": today.isoformat(),
        "stale": raw.stale,
        "scoring_version": SCORING_VERSION,
        "species_hash": all_species_hash(),
        "species": out_species,
    }
