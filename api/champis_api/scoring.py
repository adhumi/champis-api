"""Indice météo M, fenêtre phénologique Φ, persistance, groupes, palette.

Implémentation de référence (species/SCHEMA.md, skill champis-model). Les portages JS et
Swift doivent reproduire exactement cet ordre d'opérations (skill scoring-parity).

Entrée météo : un `DailySeries` couvrant au moins [J − memory_window − rain_window, J + 7]
pour la cellule, températures NON corrigées ; la correction d'altitude est faite ici.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from champis_pipeline.scoring import trapezoid

LAPSE_RATE_C_PER_M = 0.006
PHI_FLOOR = 0.25
SOIL_WINDOW_DAYS = 7
ALT_CLASS_M = 300


def altitude_class(alt_m: float) -> float:
    """Centre de la classe de 300 m contenant alt_m : 150, 450, 750… (0–299 → 150)."""
    return math.floor(max(alt_m, 0.0) / ALT_CLASS_M) * ALT_CLASS_M + ALT_CLASS_M / 2


def round_half_up(x: float) -> int:
    """Arrondi 0,5 → vers le haut, identique à Math.round (JS) et rounded() (Swift), pas au pair."""
    return math.floor(x + 0.5)


def required_series_days(species: dict[str, Any]) -> int:
    """Longueur minimale de série avant J pour calculer M_eff sans troncature."""
    w = species["weather"]
    return int(w["memory"]["window_days"] + w["rain"]["window_days"] + w["persistence"]["days"])


# --------------------------------------------------------------------------- séries météo


@dataclass
class DailySeries:
    """Séries journalières alignées sur `dates` (croissantes, contiguës)."""

    dates: list[date]
    t_mean: list[float]
    t_min: list[float]
    t_max: list[float]
    rain: list[float]
    snow: list[float]
    et0: list[float]
    soil_moisture_pct: list[float]  # percentile 0–1 déjà calculé par rapport à la climatologie
    cell_elevation_m: float
    index: dict[date, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for a, b in zip(self.dates, self.dates[1:], strict=False):
            if (b - a).days != 1:
                raise ValueError(f"série non contiguë entre {a} et {b}")
        n = len(self.dates)
        for name in ("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil_moisture_pct"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name}: {len(getattr(self, name))} valeurs pour {n} dates")
        self.index = {d: i for i, d in enumerate(self.dates)}

    def i(self, d: date) -> int:
        if d not in self.index:
            raise KeyError(f"date hors série : {d}")
        return self.index[d]

    def window(self, values: list[float], end: date, days: int) -> list[float]:
        """Les `days` dernières valeurs se terminant à `end` inclus ; lève si la série est trop courte."""
        e = self.i(end)
        s = e - days + 1
        if s < 0:
            raise KeyError(
                f"série trop courte : {days} j demandés avant {end}, {e + 1} disponibles"
            )
        return values[s : e + 1]


def corrected(values: list[float], series: DailySeries, alt_m: float) -> list[float]:
    delta = -LAPSE_RATE_C_PER_M * (alt_m - series.cell_elevation_m)
    return [v + delta for v in values]


# --------------------------------------------------------------------------- facteurs


def factor_temp(species: dict[str, Any], s: DailySeries, day: date, alt_m: float) -> float:
    cfg = species["weather"]["temp_mean"]
    vals = corrected(s.window(s.t_mean, day, cfg["window_days"]), s, alt_m)
    return trapezoid(sum(vals) / len(vals), cfg["c"])


def factor_rain(species: dict[str, Any], s: DailySeries, day: date) -> float:
    cfg = species["weather"]["rain"]
    return trapezoid(sum(s.window(s.rain, day, cfg["window_days"])), cfg["mm"])


def rain_events(
    s: DailySeries, day: date, event_mm: float, event_days: int, lookback: int
) -> list[date]:
    """Dates de fin de chaque événement (≥ event_mm sur event_days jours consécutifs, sans neige)."""
    out: list[date] = []
    e = s.i(day)
    start = max(0, e - lookback)
    for k in range(start, e + 1):
        lo = max(0, k - event_days + 1)
        if any(sn > 0 for sn in s.snow[lo : k + 1]):
            continue
        if sum(s.rain[lo : k + 1]) >= event_mm:
            out.append(s.dates[k])
    return out


def factor_trigger(species: dict[str, Any], s: DailySeries, day: date) -> float:
    cfg = species["weather"]["trigger"]
    lag = cfg["lag_days"]
    events = rain_events(s, day, cfg["event_mm"], cfg["event_days"], int(lag["max"]))
    best = 0.0
    for d in events:
        best = max(best, trapezoid((day - d).days, lag))
    return best


def factor_soil(species: dict[str, Any], s: DailySeries, day: date) -> float:
    vals = s.window(s.soil_moisture_pct, day, SOIL_WINDOW_DAYS)
    return trapezoid(sum(vals) / len(vals), species["weather"]["soil_moisture_pct"])


def factor_frost(species: dict[str, Any], s: DailySeries, day: date, alt_m: float) -> float:
    cfg = species["weather"]["frost"]
    vals = corrected(s.window(s.t_min, day, cfg["window_days"]), s, alt_m)
    nights = sum(1 for v in vals if v <= cfg["tmin_c"])
    return cfg["factor"] if nights >= cfg["nights"] else 1.0


def factor_water_balance(species: dict[str, Any], s: DailySeries, day: date) -> float:
    cfg = species["weather"]["water_balance"]
    r = s.window(s.rain, day, cfg["window_days"])
    e = s.window(s.et0, day, cfg["window_days"])
    balance = sum(r) - sum(e)
    if balance < cfg["severe_mm"]:
        return cfg["severe_factor"]
    if balance < cfg["moderate_mm"]:
        return cfg["moderate_factor"]
    return 1.0


def factor_heat(species: dict[str, Any], s: DailySeries, day: date, alt_m: float) -> float:
    cfg = species["weather"]["heat"]
    vals = corrected(s.window(s.t_max, day, cfg["window_days"]), s, alt_m)
    hot = sum(1 for v in vals if v >= cfg["tmax_c"])
    return cfg["factor"] if hot >= cfg["days"] else 1.0


def factor_memory(species: dict[str, Any], s: DailySeries, day: date) -> float:
    cfg = species["weather"]["memory"]
    rain_window = species["weather"]["rain"]["window_days"]
    end = day - timedelta(days=rain_window)
    vals = s.window(s.rain, end, cfg["window_days"])
    tz = trapezoid(sum(vals), cfg["mm"])
    return cfg["floor"] + (1.0 - cfg["floor"]) * tz


# --------------------------------------------------------------------------- M


def weather_index_raw(
    species: dict[str, Any], s: DailySeries, day: date, alt_m: float
) -> dict[str, float]:
    w = species["weather"]["weights"]
    f = {
        "temp": factor_temp(species, s, day, alt_m),
        "rain": factor_rain(species, s, day),
        "trigger": factor_trigger(species, s, day),
        "soil_moisture": factor_soil(species, s, day),
        "frost": factor_frost(species, s, day, alt_m),
        "water_balance": factor_water_balance(species, s, day),
        "heat": factor_heat(species, s, day, alt_m),
        "memory": factor_memory(species, s, day),
    }
    base = (
        w["temp"] * f["temp"]
        + w["rain"] * f["rain"]
        + w["trigger"] * f["trigger"]
        + w["soil_moisture"] * f["soil_moisture"]
    )
    m = base * f["frost"] * f["water_balance"] * f["heat"] * f["memory"]
    f["M"] = m
    return f


def weather_index(
    species: dict[str, Any], s: DailySeries, day: date, alt_m: float
) -> dict[str, float]:
    """M, M_eff (persistance puis gel et bilan hydrique réappliqués) et détail des facteurs."""
    today = weather_index_raw(species, s, day, alt_m)
    days = int(species["weather"]["persistence"]["days"])
    m_eff = today["M"]
    for k in range(1, days + 1):
        past_day = day - timedelta(days=k)
        if past_day not in s.index:
            break
        past = weather_index_raw(species, s, past_day, alt_m)["M"]
        m_eff = max(m_eff, past * (1.0 - k / days))
    m_eff = m_eff * today["frost"] * today["water_balance"]
    today["M_eff"] = min(1.0, max(0.0, m_eff))
    return today


# --------------------------------------------------------------------------- Φ


def _md(day: date) -> tuple[int, int]:
    return (day.month, day.day)


def _parse_md(s: str) -> tuple[int, int]:
    m, d = s.split("-")
    return (int(m), int(d))


def _day_of_year(md: tuple[int, int], year: int) -> int:
    m, d = md
    if m == 2 and d == 29:
        d = 28
    return date(year, m, d).timetuple().tm_yday


def _shift_days(cfg: dict[str, Any], alt_m: float, key: str) -> int:
    per100 = cfg.get(key, 0)
    raw = per100 * (alt_m - cfg["reference_m"]) / 100.0
    mx = abs(cfg["max_days"])
    return round_half_up(max(-mx, min(mx, raw)))


def phenology_window(species: dict[str, Any], day: date, alt_m: float) -> float:
    p = species["phenology"]
    year = day.year
    doy = day.timetuple().tm_yday
    shift = p.get("altitude_shift", {"reference_m": 0, "max_days": 0})
    ds = _shift_days(shift, alt_m, "start_days_per_100m")
    de = _shift_days(shift, alt_m, "end_days_per_100m")

    s_start = _day_of_year(_parse_md(p["season"]["start"]), year) + ds
    s_end = _day_of_year(_parse_md(p["season"]["end"]), year) + de
    p_start = _day_of_year(_parse_md(p["peak"]["start"]), year) + ds
    p_end = _day_of_year(_parse_md(p["peak"]["end"]), year) + de
    ramp = int(p["ramp_days"])

    # Φ = 0 hors saison ; plancher PHI_FLOOR dans la saison ; rampes linéaires de `ramp` jours
    # entre le plancher et 1 de part et d'autre du pic, bornées par la saison.
    phi = 0.0
    if s_start <= doy <= s_end:
        phi = PHI_FLOOR
        if p_start <= doy <= p_end:
            phi = 1.0
        elif doy < p_start:
            ramp_start = max(s_start, p_start - ramp)
            if doy >= ramp_start and p_start > ramp_start:
                phi = PHI_FLOOR + (1.0 - PHI_FLOOR) * (doy - ramp_start) / (p_start - ramp_start)
        else:
            ramp_end = min(s_end, p_end + ramp)
            if doy <= ramp_end and ramp_end > p_end:
                phi = 1.0 - (1.0 - PHI_FLOOR) * (doy - p_end) / (ramp_end - p_end)

    sec = p.get("secondary_peak")
    if sec:
        a = _day_of_year(_parse_md(sec["start"]), year) + ds
        b = _day_of_year(_parse_md(sec["end"]), year) + de
        if a <= doy <= b:
            phi = max(phi, float(sec["phi"]))
    return min(1.0, max(0.0, phi))


# --------------------------------------------------------------------------- score, groupes, palette


def score(t: float, m_eff: float, phi: float) -> float:
    return t * m_eff * phi


def group_score(scores: dict[str, float]) -> dict[str, Any]:
    """Score d'un groupe = max des espèces ; `leading` = espèce qui le porte (ordre alphabétique en cas d'égalité)."""
    if not scores:
        return {"score": 0.0, "leading": None}
    leading = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    return {"score": leading[1], "leading": leading[0]}


def palette_class(value: float, palette: dict[str, Any]) -> dict[str, str]:
    for cls in palette["thresholds"]:
        if value >= cls["min"]:
            return {"label": cls["label"], "hex": cls["hex"]}
    last = palette["thresholds"][-1]
    return {"label": last["label"], "hex": last["hex"]}
