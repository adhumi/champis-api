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
    """Longueur minimale de série avant J pour calculer M_eff sans troncature (la fenêtre de la
    température du sol, facteur optionnel, est comptée si elle dépasse celle de la pluie)."""
    w = species["weather"]
    window = max(w["rain"]["window_days"], w.get("soil_temp", {}).get("window_days", 0))
    return int(w["memory"]["window_days"] + window + w["persistence"]["days"])


class SeriesGapError(Exception):
    """Trou ou troncature dans les séries : jamais une approximation silencieuse (SCHEMA.md)."""


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
    # température du sol 0–7 cm (°C), facteur optionnel du 9 nov. 2026 : `None` quand la source
    # ne la fournit pas, `None` par jour quand l'historique amorcé avant sa mise en service l'ignore ;
    # seule une espèce qui l'emploie en a besoin, et alors un trou est une erreur
    soil_temp: list[float | None] | None = None
    index: dict[date, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for a, b in zip(self.dates, self.dates[1:], strict=False):
            if (b - a).days != 1:
                raise ValueError(f"série non contiguë entre {a} et {b}")
        n = len(self.dates)
        for name in ("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil_moisture_pct"):
            if len(getattr(self, name)) != n:
                raise ValueError(f"{name}: {len(getattr(self, name))} valeurs pour {n} dates")
        if self.soil_temp is not None and len(self.soil_temp) != n:
            raise ValueError(f"soil_temp: {len(self.soil_temp)} valeurs pour {n} dates")
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


def trigger_event(species: dict[str, Any], s: DailySeries, day: date) -> tuple[float, int | None]:
    """(facteur, âge en jours de l'événement retenu) : le meilleur événement de la fenêtre (règle 4),
    le plus récent à égalité ; `None` sans événement. L'âge sert aux phrases en clair (lot 2)."""
    cfg = species["weather"]["trigger"]
    lag = cfg["lag_days"]
    events = rain_events(s, day, cfg["event_mm"], cfg["event_days"], int(lag["max"]))
    best, age = 0.0, None
    for d in events:  # chronologique : à facteur égal, le plus récent remplace
        f = trapezoid((day - d).days, lag)
        if f >= best and (f > 0 or age is None):
            best, age = f, (day - d).days
    return best, age


def factor_trigger(species: dict[str, Any], s: DailySeries, day: date) -> float:
    return trigger_event(species, s, day)[0]


def factor_soil_temp(species: dict[str, Any], s: DailySeries, day: date, alt_m: float) -> float:
    """Température du sol 0–7 cm, moyenne sur `window_days`, corrigée à l'altitude du pixel comme
    l'air (spec › Scoring › Facteurs de sol et de paysage, 9 nov. 2026). Facteur optionnel : appelé
    seulement si l'espèce a un bloc `soil_temp`. Une valeur absente dans la fenêtre est une erreur."""
    cfg = species["weather"]["soil_temp"]
    if s.soil_temp is None:
        raise SeriesGapError("`soil_temperature_0_to_7cm_mean` absent de la série")
    vals = s.window(s.soil_temp, day, cfg["window_days"])
    e = s.i(day)
    window_dates = s.dates[e - len(vals) + 1 : e + 1]
    missing = [d.isoformat() for d, v in zip(window_dates, vals, strict=True) if v is None]
    if missing:
        raise SeriesGapError(
            f"`soil_temperature_0_to_7cm_mean` manquant sur {len(missing)} jour(s) "
            f"({missing[0]} → {missing[-1]}) : collecte trop récente pour ce facteur"
        )
    corrected_vals = corrected([float(v) for v in vals], s, alt_m)
    return trapezoid(sum(corrected_vals) / len(corrected_vals), cfg["c"])


def factor_soil(species: dict[str, Any], s: DailySeries, day: date) -> float:
    vals = s.window(s.soil_moisture_pct, day, SOIL_WINDOW_DAYS)
    return trapezoid(sum(vals) / len(vals), species["weather"]["soil_moisture_pct"])


def frost_severity(tmin_c: float, cfg: dict[str, Any]) -> float:
    """Sévérité d'une nuit, 0–1 : `1 − trapèze(tmin, min = tmin_c, opt_low = tmin_c + ramp_c,
    opt_high = +∞, max = +∞)`, soit 1 à `tmin_c` et en dessous, 0 dès `tmin_c + ramp_c`, linéaire
    entre. Le trapèze unique du modèle (règle 1), avec des bornes hautes infinies : `trapezoid`
    accepte `inf` (x ≤ +∞ est toujours vrai, et une rampe descendante de +∞ à +∞ n'existe pas)."""
    ramp = {
        "min": cfg["tmin_c"],
        "opt_low": cfg["tmin_c"] + cfg["ramp_c"],
        "opt_high": math.inf,
        "max": math.inf,
    }
    return 1.0 - trapezoid(tmin_c, ramp)


def factor_frost(species: dict[str, Any], s: DailySeries, day: date, alt_m: float) -> float:
    """Gel en rampe (spec › Scoring › gel en rampe, 4 nov. 2026) :

        S = Σ sévérité(tmin corrigée) sur les `window_days` dernières nuits
        facteur = 1 − (1 − factor) × min(1, S / nights)

    `nights` nuits à `tmin_c` ou en dessous donnent × `factor`, comme l'ancienne marche ; une seule
    nuit à mi-rampe donne `1 − (1 − factor) × 0,5 / nights`. Continu en température et en nombre
    de nuits : deux cellules météo voisines ne basculent plus pour un dixième de degré."""
    cfg = species["weather"]["frost"]
    vals = corrected(s.window(s.t_min, day, cfg["window_days"]), s, alt_m)
    severity = sum(frost_severity(v, cfg) for v in vals)
    return 1.0 - (1.0 - cfg["factor"]) * min(1.0, severity / cfg["nights"])


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
    if "soil_temp" in species["weather"]:  # facteur optionnel (9 nov. 2026) : absent = poids 0
        f["soil_temp"] = factor_soil_temp(species, s, day, alt_m)
        base += w.get("soil_temp", 0.0) * f["soil_temp"]
    m = base * f["frost"] * f["water_balance"] * f["heat"] * f["memory"]
    f["M"] = m
    return f


def weather_index(
    species: dict[str, Any],
    s: DailySeries,
    day: date,
    alt_m: float,
    memo: dict[date, dict[str, float]] | None = None,
) -> dict[str, float]:
    """M, M_eff (persistance puis gel et bilan hydrique réappliqués) et détail des facteurs.

    `memo` : facteurs bruts déjà calculés par jour, pour **cette** espèce, cette série et cette
    altitude. La persistance relit les `days` jours d'avant : sur onze jours consécutifs, les
    mêmes jours reviennent presque à chaque fois (253 calculs pour 33 jours distincts) ; l'appelant
    qui enchaîne les jours passe le même dictionnaire, le résultat est identique (21 nov. 2026)."""

    def raw(d: date) -> dict[str, float]:
        if memo is None:
            return weather_index_raw(species, s, d, alt_m)
        if d not in memo:
            memo[d] = weather_index_raw(species, s, d, alt_m)
        return memo[d]

    today = dict(raw(day))
    days = int(species["weather"]["persistence"]["days"])
    m_eff = today["M"]
    for k in range(1, days + 1):
        past_day = day - timedelta(days=k)
        if past_day not in s.index:
            break
        past = raw(past_day)["M"]
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


def _date_md(year: int, md: tuple[int, int]) -> date:
    m, d = md
    if m == 2 and d == 29:
        d = 28
    return date(year, m, d)


def season_wraps(p: dict[str, Any]) -> bool:
    """Une saison dont `season.end` précède `season.start` passe le 1er janvier (décision
    d'Adrien du 9 nov. 2026 : pied bleu, petit-gris, lactaire sanguin, bolet bai en janvier)."""
    return _parse_md(p["season"]["end"]) < _parse_md(p["season"]["start"])


def _season_axis(p: dict[str, Any], day: date, ds: int, de: int) -> tuple[int, int, int, int, int]:
    """`(d, s_start, s_end, p_start, p_end)` sur un même axe entier, décalages compris.

    Saison dans l'année civile : jours de l'année de `day`, comme avant le 9 nov. 2026 (une fin
    décalée au-delà du 31 décembre ne déborde pas sur janvier : comportement inchangé). Saison à
    cheval sur le 1er janvier : ordinaux de dates ; la saison qui contient `day` est celle qui
    commence l'année de `day` ou l'année d'avant, le pic et la fin tombant l'année suivante quand
    leur mois-jour précède le début."""
    season, peak = p["season"], p["peak"]
    s0, e0 = _parse_md(season["start"]), _parse_md(season["end"])
    ps0, pe0 = _parse_md(peak["start"]), _parse_md(peak["end"])
    if not season_wraps(p):
        year = day.year
        return (
            day.timetuple().tm_yday,
            _day_of_year(s0, year) + ds,
            _day_of_year(e0, year) + de,
            _day_of_year(ps0, year) + ds,
            _day_of_year(pe0, year) + de,
        )
    d = day.toordinal()
    candidates = []
    for year in (day.year - 1, day.year):
        s_start = _date_md(year, s0).toordinal() + ds
        s_end = _date_md(year + 1, e0).toordinal() + de
        p_start = _date_md(year + (1 if ps0 < s0 else 0), ps0).toordinal() + ds
        p_end = _date_md(year + (1 if pe0 < s0 else 0), pe0).toordinal() + de
        candidates.append((d, s_start, s_end, p_start, p_end))
    for c in candidates:
        if c[1] <= d <= c[2]:
            return c
    return candidates[-1]


def phenology_window(species: dict[str, Any], day: date, alt_m: float) -> float:
    p = species["phenology"]
    year = day.year
    shift = p.get("altitude_shift", {"reference_m": 0, "max_days": 0})
    ds = _shift_days(shift, alt_m, "start_days_per_100m")
    de = _shift_days(shift, alt_m, "end_days_per_100m")

    doy, s_start, s_end, p_start, p_end = _season_axis(p, day, ds, de)
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
    if sec:  # pic secondaire : toujours dans l'année civile de `day`
        day_of_year = day.timetuple().tm_yday
        a = _day_of_year(_parse_md(sec["start"]), year) + ds
        b = _day_of_year(_parse_md(sec["end"]), year) + de
        if a <= day_of_year <= b:
            phi = max(phi, float(sec["phi"]))
    return min(1.0, max(0.0, phi))


def season_phase(species: dict[str, Any], day: date, alt_m: float) -> str:
    """Position du jour dans la saison, pour les phrases (étape B2, 6 nov. 2026) : « hors » hors de
    la saison, « debut » avant le pic, « pleine » au pic (ou quand Φ atteint 1 par un pic
    secondaire), « fin » après. Mêmes dates et même décalage d'altitude que `phenology_window`."""
    p = species["phenology"]
    shift = p.get("altitude_shift", {"reference_m": 0, "max_days": 0})
    ds = _shift_days(shift, alt_m, "start_days_per_100m")
    de = _shift_days(shift, alt_m, "end_days_per_100m")
    doy, s_start, s_end, p_start, p_end = _season_axis(p, day, ds, de)
    if not s_start <= doy <= s_end:
        return "hors"
    if p_start <= doy <= p_end or phenology_window(species, day, alt_m) >= 1.0:
        return "pleine"
    return "debut" if doy < p_start else "fin"


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
    """Mot d'un score (règle 10) : premier seuil dont `min ≤ value`, sinon le dernier. Le `hex`
    est la couleur de pastille de la classe, jamais celle d'un pixel (voir `palette_color`)."""
    for cls in palette["thresholds"]:
        if value >= cls["min"]:
            return {"label": cls["label"], "hex": cls["hex"]}
    last = palette["thresholds"][-1]
    return {"label": last["label"], "hex": last["hex"]}


def palette_color(value: float, palette: dict[str, Any]) -> tuple[int, int, int, int]:
    """Couleur RGBA d'un pixel de score `value` (échelle linéaire, décision du 6 nov. 2026) :
    `rgb` = `scale.hex`, alpha = `floor(255 × (alpha_min + (alpha_max − alpha_min) × s) + 0.5)`
    avec `s` borné à [0, 1]. `floor(x + 0.5)` et non `round` : la règle au demi de `round`
    diffère entre Python, JavaScript et Swift, celle-ci est la même partout (skill scoring-parity).
    """
    scale = palette["scale"]
    h = scale["hex"]
    r, g, b = (int(h[i : i + 2], 16) for i in (1, 3, 5))
    s = min(max(float(value), 0.0), 1.0)
    alpha = scale["alpha_min"] + (scale["alpha_max"] - scale["alpha_min"]) * s
    return (r, g, b, math.floor(255.0 * alpha + 0.5))
