"""Indice par département (vue France) : `I = Σ T × M_eff × Φ / Σ T` sur les pixels forestiers.

Spec › Scoring › indice par département (30 oct. 2026). Les poids Σ T viennent de la grille météo
version 2 (par cellule, département touché et classe d'altitude) ; `M_eff` et `Φ` de la météo de
chaque cellule, par classe d'altitude. Calculé pour chaque espèce et les 11 jours J−3 → J+7 à la
fin de chaque pré-chauffe, stocké dans le SQLite du volume, servi par `GET /departements`.

Le même passage range dans la table `cell_weather` les lignes `M`, `M_eff`, `Φ` de chaque
cellule et de chaque espèce : `GET /weather/bbox` les sert telles quelles, sans recalcul, ce qui
permet de peindre une emprise de plusieurs centaines de cellules (décision du 2 nov. 2026). Dans
la même transaction, `cell_field` reçoit le champ compact `M_eff × Φ` par jour que sert
`GET /weather/field` ; la passe purge ensuite les jours sortis de la fenêtre (5 nov. 2026).

Une cellule sans météo (indisponible, historique trop court) sort du numérateur et du
dénominateur ; `coverage` dit quelle part de Σ T a été couverte. Jamais de valeur inventée : un
département sans aucune cellule couverte a une valeur `None`.
Groupe (règle 9) : maximum des indices des espèces du groupe, `leading` = l'espèce qui le porte.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from champis_pipeline.species import load_species

from .cache import WeatherCache
from .grid import WeatherGrid
from .scoring import group_score
from .weather_client import OpenMeteoClient
from .weather_service import HistoryTooShortError, SeriesGapError, cell_weather, days_around

log = logging.getLogger(__name__)

# cellule → espèce → jour ISO → classe d'altitude (« 450 ») → {"M_eff", "phi"}
CellWeather = dict[str, dict[str, dict[str, dict[str, dict[str, float]]]]]
IndexRow = tuple[str, str, str, float | None, float]  # dept, species, day, value, coverage


@dataclass
class DeptIndexReport:
    departements: int
    cells: int
    cells_without_weather: int
    rows: int
    stored: int = 0  # entrées (cellule, espèce) rangées pour `GET /weather/bbox`
    field_rows: int = 0  # lignes `cell_field` après purge, pour `GET /weather/field`


def aggregate(
    grid: WeatherGrid, weather: CellWeather, species_ids: list[str], days: list[date]
) -> list[IndexRow]:
    """Agrège les poids Σ T de la grille et la météo par cellule en un indice par
    (département, espèce, jour). Pur et testable : aucune E/S."""
    num: dict[tuple[str, str, str], float] = {}
    den: dict[tuple[str, str, str], float] = {}
    total: dict[tuple[str, str], float] = {}
    day_keys = [d.isoformat() for d in days]
    for cell in grid.cells.values():
        w = weather.get(cell.id)
        for dept, by_alt in cell.t_sum.items():
            for alt, by_sp in by_alt.items():
                for sid, t_sum in by_sp.items():
                    if sid not in species_ids or t_sum <= 0:
                        continue
                    total[dept, sid] = total.get((dept, sid), 0.0) + t_sum
                    if w is None:
                        continue
                    for dk in day_keys:
                        row = w.get(sid, {}).get(dk, {}).get(alt)
                        if row is None:
                            continue
                        k = (dept, sid, dk)
                        num[k] = num.get(k, 0.0) + t_sum * row["M_eff"] * row["phi"]
                        den[k] = den.get(k, 0.0) + t_sum
    rows: list[IndexRow] = []
    for (dept, sid), tot in sorted(total.items()):
        for dk in day_keys:
            d = den.get((dept, sid, dk), 0.0)
            value = num[dept, sid, dk] / d if d > 0 else None
            rows.append((dept, sid, dk, value, d / tot if tot > 0 else 0.0))
    return rows


async def compute(
    grid: WeatherGrid, client: OpenMeteoClient, cache: WeatherCache, species_ids: list[str]
) -> DeptIndexReport:
    """Recalcule l'indice pour toute la grille à partir du cache (après une pré-chauffe, aucune
    requête Open-Meteo n'est nécessaire) et l'enregistre."""
    cells = [c for c in grid.cells.values() if c.t_sum]
    if not cells:
        log.warning("indice par département : grille sans poids Σ T (version 1) ; rien à calculer")
        return DeptIndexReport(0, 0, 0, 0)
    today = client.today()
    days = days_around(today)
    res = await client.fetch_cells_partial(cells)
    now = client.now()
    weather: CellWeather = {}
    missing = len(res.unavailable)
    stored = 0
    for c in cells:
        raw = res.raws.get(c.id)
        if raw is None:
            continue
        try:
            out = cell_weather(c, raw, species_ids, today, detail=False)
        except (HistoryTooShortError, SeriesGapError) as e:
            log.info("indice : cellule %s écartée (%s)", c.id, e)
            missing += 1
            continue
        weather[c.id] = {
            sid: {row["date"]: row["by_alt"] for row in rows}
            for sid, rows in out["species"].items()
        }
        stored += cache.put_cell_weather(
            ((c.id, sid, raw.stale, rows) for sid, rows in out["species"].items()), today, now
        )
    rows = aggregate(grid, weather, species_ids, days)
    cache.put_dept_index(rows, today, now)
    pruned = cache.prune_cell_field(today)
    report = DeptIndexReport(
        len(grid.departements()), len(cells), missing, len(rows), stored, cache.cell_field_count()
    )
    log.info(
        "indice par département : %d départements, %d cellules (%d sans météo), %d lignes ; "
        "météo par cellule rangée pour /weather/bbox : %d entrées ; champ compact pour "
        "/weather/field : %d lignes (%d purgées hors fenêtre)",
        report.departements,
        report.cells,
        report.cells_without_weather,
        report.rows,
        report.stored,
        report.field_rows,
        pruned,
    )
    return report


def groups_of(species_ids: list[str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for sid in species_ids:
        g = load_species(sid).get("group")
        if g:
            groups.setdefault(g["id"], []).append(sid)
    return groups


def shape_response(
    rows: list[IndexRow], species_ids: list[str]
) -> dict[str, dict[str, dict[str, list[dict]]]]:
    """`{dept: {"species": {sid: [{date, value, coverage}]}, "groups": {gid: [{date, value, leading, coverage}]}}}`."""
    out: dict[str, dict[str, dict[str, list[dict]]]] = {}
    by_dept_day: dict[str, dict[str, dict[str, tuple[float | None, float]]]] = {}
    for dept, sid, day, value, coverage in rows:
        out.setdefault(dept, {"species": {}, "groups": {}})["species"].setdefault(sid, []).append(
            {"date": day, "value": value, "coverage": coverage}
        )
        by_dept_day.setdefault(dept, {}).setdefault(day, {})[sid] = (value, coverage)
    for gid, members in groups_of(species_ids).items():
        for dept, days in by_dept_day.items():
            series = []
            for day in sorted(days):
                scores = {s: v for s, (v, _) in days[day].items() if s in members and v is not None}
                if not scores:
                    series.append({"date": day, "value": None, "leading": None, "coverage": 0.0})
                    continue
                g = group_score(scores)
                series.append(
                    {
                        "date": day,
                        "value": g["score"],
                        "leading": g["leading"],
                        "coverage": days[day][g["leading"]][1],
                    }
                )
            out[dept]["groups"][gid] = series
    return out
