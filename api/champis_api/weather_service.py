"""Assemble les séries d'une cellule et calcule M, M_eff et Φ par espèce, jour et classe d'altitude.

Réponse `/weather` (skill open-meteo) : pour chaque jour de J−3 à J+7, par classe d'altitude de la
cellule, `M`, `M_eff`, `phi` et le détail des facteurs — la fiche « pourquoi c'est vert » en dépend.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from champis_pipeline import SCORING_VERSION
from champis_pipeline.species import all_species_hash, load_species

from .explain import sentence, summary
from .grid import Cell
from .scoring import (
    DailySeries,
    SeriesGapError,  # levée ici (trou) comme dans le scoring (facteur optionnel soil_temp)
    phenology_window,
    required_series_days,
    season_phase,
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


VARS = ("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil", "soil_t")
# Variables optionnelles : un trou n'est une erreur que si une espèce les emploie (facteur optionnel
# `soil_temp`, 9 nov. 2026) ; l'historique amorcé avant leur mise en service ne les a pas
OPTIONAL_VARS = frozenset({"soil_t"})
OPEN_METEO_NAMES = {
    "t_mean": "temperature_2m_mean",
    "t_min": "temperature_2m_min",
    "t_max": "temperature_2m_max",
    "rain": "rain_sum",
    "snow": "snowfall_sum",
    "et0": "et0_fao_evapotranspiration",
    "soil": "soil_moisture",
    "soil_t": "soil_temperature_0_to_7cm_mean",
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
    merged: dict[str, list[float | None]] = {}
    for var in VARS:
        values, missing = [], []
        fvals = f.get(var)
        for d in dates:
            k = d.isoformat()
            v = fvals[f_idx[k]] if fvals is not None and k in f_idx else None
            if v is None and k in h:
                v = h[k].get(var)
            if v is None:
                missing.append(k)
            values.append(v)
        if missing and var not in OPTIONAL_VARS:
            raise SeriesGapError(f"`{OPEN_METEO_NAMES[var]}` manquant sur {_summary(missing)}")
        merged[var] = values
    soil_pct = [soil_percentile(v, raw.soil_clim) for v in merged["soil"]]
    soil_t = merged["soil_t"]
    return DailySeries(
        dates=dates,
        t_mean=[float(v) for v in merged["t_mean"]],  # type: ignore[arg-type]
        t_min=[float(v) for v in merged["t_min"]],  # type: ignore[arg-type]
        t_max=[float(v) for v in merged["t_max"]],  # type: ignore[arg-type]
        rain=[float(v) for v in merged["rain"]],  # type: ignore[arg-type]
        snow=[float(v) for v in merged["snow"]],  # type: ignore[arg-type]
        et0=[float(v) for v in merged["et0"]],  # type: ignore[arg-type]
        soil_moisture_pct=[float(v) for v in soil_pct],  # type: ignore[arg-type]
        cell_elevation_m=float(f["elevation"]),
        soil_temp=[None if v is None else float(v) for v in soil_t],
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
                    lag = sp["weather"]["trigger"].get("lag_days")
                    phase = season_phase(sp, d, alt)
                    row["summary"] = summary(factors, phi, trigger_days, lag=lag, phase=phase)
                    # la même chose en une phrase (fiche d'un point, 17 nov. 2026)
                    row["sentence"] = sentence(factors, phi, trigger_days, lag=lag, phase=phase)
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
