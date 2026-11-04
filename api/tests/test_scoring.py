from __future__ import annotations

from datetime import date, timedelta

import pytest
from champis_api.scoring import (
    PHI_FLOOR,
    DailySeries,
    factor_frost,
    factor_trigger,
    factor_water_balance,
    frost_severity,
    group_score,
    palette_class,
    phenology_window,
    weather_index,
    weather_index_raw,
)
from champis_pipeline.scoring import trapezoid
from champis_pipeline.species import list_species, load_palette, load_species
from conftest import make_series

EDULIS = load_species("boletus_edulis")
CIBARIUS = load_species("cantharellus_cibarius")
PINOPHILUS = load_species("boletus_pinophilus")


# ------------------------------------------------------------------ trapèze


@pytest.mark.parametrize(
    ("x", "expected"),
    [(0, 0.0), (2, 0.0), (3, 0.5), (4, 1.0), (6, 1.0), (7, 0.5), (8, 0.0), (9, 0.0)],
)
def test_trapezoid_shape(x, expected):
    t = {"min": 2, "opt_low": 4, "opt_high": 6, "max": 8}
    assert trapezoid(x, t) == pytest.approx(expected)


def test_trapezoid_step_edges():
    t = {"min": 0, "opt_low": 0, "opt_high": 20, "max": 40}
    assert trapezoid(0, t) == 1.0
    assert trapezoid(30, t) == pytest.approx(0.5)


# ------------------------------------------------------------------ trigger


def test_trigger_best_event_not_last():
    """Grosse pluie il y a 14 j puis petite pluie il y a 2 j : le max gagne."""
    s = make_series(date(2026, 8, 1), 90, rain=0.0)
    day = date(2026, 10, 15)
    s.rain[s.i(date(2026, 10, 1))] = 25.0  # il y a 14 j → optimum (10–20)
    s.rain[s.i(date(2026, 10, 13))] = 22.0  # il y a 2 j → sous lag.min
    assert factor_trigger(EDULIS, s, day) == 1.0


def test_trigger_snow_does_not_count():
    s = make_series(date(2026, 8, 1), 90, rain=0.0)
    day = date(2026, 10, 15)
    i = s.i(date(2026, 10, 1))
    s.rain[i] = 25.0
    s.snow[i] = 3.0
    assert factor_trigger(EDULIS, s, day) == 0.0


def test_trigger_none_in_window():
    s = make_series(date(2026, 8, 1), 90, rain=0.0)
    assert factor_trigger(EDULIS, s, date(2026, 10, 15)) == 0.0


# ------------------------------------------------------------------ gel et altitude


def test_frost_only_with_altitude_correction():
    s = make_series(date(2026, 10, 1), 30, t_min=2.0, elevation=700.0)
    day = date(2026, 10, 20)
    assert factor_frost(EDULIS, s, day, 700.0) == 1.0
    # à 1 450 m : 2 − 0,006 × 750 = −2,5 °C, sous tmin_c toutes les nuits → × factor
    assert factor_frost(EDULIS, s, day, 1450.0) == pytest.approx(
        EDULIS["weather"]["frost"]["factor"]
    )


# gel en rampe (spec › Scoring › gel en rampe, 4 nov. 2026)


def frost_series(nights: dict[int, float]) -> tuple[DailySeries, date]:
    """Nuits douces (8 °C) sauf `nights` : {jours avant J: tmin}, cellule et pixel à 700 m."""
    day = date(2026, 10, 20)
    s = make_series(date(2026, 10, 1), 30, t_min=8.0, elevation=700.0)
    for k, v in nights.items():
        s.t_min[s.i(day - timedelta(days=k))] = v
    return s, day


@pytest.mark.parametrize(
    ("offset_c", "expected"),
    [
        (-5.0, 1.0),
        (-1e-9, 1.0),
        (0.0, 1.0),
        (0.5, 0.75),
        (1.0, 0.5),
        (1.5, 0.25),
        (2.0, 0.0),
        (2.0 + 1e-9, 0.0),
        (7.0, 0.0),
    ],
)
def test_frost_severity_is_one_minus_trapezoid(offset_c, expected):
    """s = 1 − trapèze(tmin, min = tmin_c, opt_low = tmin_c + ramp_c, +∞, +∞)."""
    cfg = EDULIS["weather"]["frost"]
    assert frost_severity(cfg["tmin_c"] + offset_c, cfg) == pytest.approx(expected)


def test_frost_ramp_exact_values():
    """Cèpe : tmin_c −2, ramp_c 2, nights 2, factor 0,2."""
    cfg = EDULIS["weather"]["frost"]
    assert (cfg["tmin_c"], cfg["ramp_c"], cfg["nights"], cfg["factor"]) == (-2, 2, 2, 0.2)
    # aucune nuit froide
    s, day = frost_series({})
    assert factor_frost(EDULIS, s, day, 700.0) == 1.0
    # une nuit à mi-rampe (−1 °C, s = 0,5) : 1 − 0,8 × 0,25 = 0,8
    s, day = frost_series({0: -1.0})
    assert factor_frost(EDULIS, s, day, 700.0) == pytest.approx(0.8)
    # une nuit pleine : 1 − 0,8 × 0,5 = 0,6
    s, day = frost_series({3: -2.0})
    assert factor_frost(EDULIS, s, day, 700.0) == pytest.approx(0.6)
    # deux nuits pleines : × factor, comme l'ancienne marche
    s, day = frost_series({0: -2.0, 1: -2.0})
    assert factor_frost(EDULIS, s, day, 700.0) == pytest.approx(0.2)
    # quatre nuits partielles qui se cumulent : S = 0,5 + 0,25 + 0,75 + 0 = 1,5 → 1 − 0,8 × 0,75
    s, day = frost_series({0: -1.0, 1: -0.5, 2: -1.5, 3: 0.0})
    assert factor_frost(EDULIS, s, day, 700.0) == pytest.approx(0.4)
    # S plafonné à nights : sept nuits polaires ne font pas moins que factor
    s, day = frost_series({k: -15.0 for k in range(7)})
    assert factor_frost(EDULIS, s, day, 700.0) == pytest.approx(0.2)


def test_frost_ramp_is_continuous():
    """Une nuit juste sous tmin_c + ramp_c donne un facteur proche de 1 ; une nuit juste sous
    tmin_c, le même facteur qu'à tmin_c : plus de marche, en température comme en nombre de nuits."""
    cfg = EDULIS["weather"]["frost"]
    top = cfg["tmin_c"] + cfg["ramp_c"]
    s, day = frost_series({0: top - 1e-6})
    assert factor_frost(EDULIS, s, day, 700.0) == pytest.approx(1.0, abs=1e-6)
    s, day = frost_series({0: top})
    assert factor_frost(EDULIS, s, day, 700.0) == 1.0
    s, day = frost_series({0: cfg["tmin_c"] - 1e-6})
    s_at, _ = frost_series({0: cfg["tmin_c"]})
    assert factor_frost(EDULIS, s, day, 700.0) == pytest.approx(
        factor_frost(EDULIS, s_at, day, 700.0)
    )
    # monotone décroissant en température
    values = [
        factor_frost(EDULIS, frost_series({0: v})[0], day, 700.0)
        for v in (1.0, 0.0, -0.5, -1.0, -1.5, -2.0, -3.0)
    ]
    assert values == sorted(values, reverse=True)


def test_frost_window_edge():
    """J−window_days est hors fenêtre, J−window_days+1 dedans."""
    cfg = EDULIS["weather"]["frost"]
    w = cfg["window_days"]
    s, day = frost_series({w: -10.0, w - 1: -10.0})
    assert factor_frost(EDULIS, s, day, 700.0) == pytest.approx(
        1.0 - (1.0 - cfg["factor"]) / cfg["nights"]
    )


def test_frost_ramp_reapplied_on_m_eff(calm_autumn):
    """Le facteur du jour, en rampe, multiplie aussi M_eff (règle 3)."""
    s = calm_autumn
    s.rain[s.i(date(2026, 10, 1))] = 30.0
    day = date(2026, 10, 13)
    mild = weather_index(EDULIS, s, day, 700.0)
    s.t_min[s.i(day)] = -1.0  # mi-rampe → 0,8, seulement la nuit de J
    r = weather_index(EDULIS, s, day, 700.0)
    assert r["frost"] == pytest.approx(0.8)
    assert r["M"] == pytest.approx(mild["M"] * 0.8)
    days = EDULIS["weather"]["persistence"]["days"]
    best_past = max(
        weather_index_raw(EDULIS, s, day - timedelta(days=k), 700.0)["M"] * (1 - k / days)
        for k in range(1, days + 1)
    )
    assert r["M_eff"] == pytest.approx(min(1.0, max(r["M"], best_past) * 0.8))
    assert r["M_eff"] < mild["M_eff"]


# ------------------------------------------------------------------ bilan hydrique


def test_water_balance_fair_weather_after_rain_is_not_penalised():
    """10 j de beau temps après la pluie : bilan 14 j encore positif."""
    s = make_series(date(2026, 9, 1), 60, rain=0.0, et0=2.0)
    s.rain[s.i(date(2026, 10, 1))] = 40.0
    assert factor_water_balance(EDULIS, s, date(2026, 10, 11)) == 1.0


def test_water_balance_severe():
    s = make_series(date(2026, 9, 1), 60, rain=0.0, et0=4.0)
    assert (
        factor_water_balance(EDULIS, s, date(2026, 10, 11))
        == EDULIS["weather"]["water_balance"]["severe_factor"]
    )


# ------------------------------------------------------------------ M complet


def test_ideal_autumn_scores_high(calm_autumn):
    s = calm_autumn
    s.rain[s.i(date(2026, 10, 1))] = 30.0
    r = weather_index(EDULIS, s, date(2026, 10, 13), 700.0)
    assert r["M"] > 0.85
    assert r["M_eff"] >= r["M"]
    assert set(r) >= {
        "temp",
        "rain",
        "trigger",
        "soil_moisture",
        "frost",
        "water_balance",
        "heat",
        "memory",
        "M",
        "M_eff",
    }


def test_persistence_keeps_girolles_longer_than_cepes(calm_autumn):
    """Conditions idéales puis effondrement brutal : M_eff décroît plus lentement pour la girolle."""
    s = calm_autumn
    s.rain[s.i(date(2026, 10, 1))] = 30.0
    crash = s.i(date(2026, 10, 16))
    for i in range(crash, len(s.dates)):
        s.t_mean[i], s.rain[i], s.soil_moisture_pct[i] = 0.0, 0.0, 0.0
    day = date(2026, 10, 20)
    r_c = weather_index(CIBARIUS, s, day, 700.0)
    r_e = weather_index(EDULIS, s, day, 700.0)
    assert r_c["M_eff"] > r_c["M"]
    assert r_e["M_eff"] > r_e["M"]
    assert r_c["M_eff"] > r_e["M_eff"]  # 30 j de persistance contre 12


# ------------------------------------------------------------------ Φ


def test_phi_peak_floor_and_offseason():
    assert phenology_window(EDULIS, date(2026, 10, 1), 300) == 1.0
    assert phenology_window(EDULIS, date(2026, 7, 20), 300) == PHI_FLOOR
    assert phenology_window(EDULIS, date(2026, 3, 1), 300) == 0.0


def test_phi_ramp_midpoint():
    # pic le 1er sept., rampe 30 j → le 17 août ≈ milieu
    v = phenology_window(EDULIS, date(2026, 8, 17), 300)
    assert PHI_FLOOR < v < 1.0


def test_phi_altitude_shift_asymmetric_girolle():
    # +4 j/100 m au début : à 1 400 m (+1 000 m) le début passe du 1er juin au 30 juin (borné 30)
    assert phenology_window(CIBARIUS, date(2026, 6, 10), 400) == PHI_FLOOR
    assert phenology_window(CIBARIUS, date(2026, 6, 10), 1400) == 0.0


def test_phi_secondary_peak_pinophilus():
    assert phenology_window(PINOPHILUS, date(2026, 6, 1), 600) == pytest.approx(0.6)


def test_phi_leap_day_does_not_crash():
    for sid in list_species():
        phenology_window(load_species(sid), date(2028, 2, 29), 500)


# ------------------------------------------------------------------ groupes, palette


def test_group_max_and_tie_break():
    g = group_score({"boletus_edulis": 0.6, "boletus_aereus": 0.6, "boletus_pinophilus": 0.2})
    assert g == {"score": 0.6, "leading": "boletus_aereus"}


@pytest.mark.parametrize(
    ("v", "label"),
    [
        (0.75, "excellent"),
        (0.7499, "bon"),
        (0.55, "bon"),
        (0.35, "moyen"),
        (0.15, "faible"),
        (0.1499, "tres_faible"),
        (0.0, "tres_faible"),
    ],
)
def test_palette_thresholds(v, label):
    assert palette_class(v, load_palette())["label"] == label


# ------------------------------------------------------------------ robustesse des séries


def test_short_series_raises():
    s = make_series(date(2026, 9, 1), 40)
    with pytest.raises(KeyError):
        weather_index(EDULIS, s, date(2026, 10, 10), 700.0)


def test_non_contiguous_series_rejected():
    s = make_series(date(2026, 9, 1), 10)
    s.dates[5] = date(2026, 9, 20)
    with pytest.raises(ValueError, match="contigu"):
        DailySeries(
            **{
                k: getattr(s, k)
                for k in (
                    "dates",
                    "t_mean",
                    "t_min",
                    "t_max",
                    "rain",
                    "snow",
                    "et0",
                    "soil_moisture_pct",
                    "cell_elevation_m",
                )
            }
        )


def test_forecast_last_day(calm_autumn):
    s = calm_autumn
    r = weather_index(EDULIS, s, s.dates[-1], 700.0)
    assert 0.0 <= r["M_eff"] <= 1.0


def test_memory_window_excludes_recent_rain():
    """Pluie nulle J−117..J−28 mais forte dans les 28 derniers jours : mémoire au plancher."""
    s = make_series(date(2026, 3, 1), 260, rain=0.0)
    for k in range(s.i(date(2026, 9, 18)), s.i(date(2026, 10, 15)) + 1):
        s.rain[k] = 5.0
    from champis_api.scoring import factor_memory

    assert factor_memory(EDULIS, s, date(2026, 10, 15)) == pytest.approx(
        EDULIS["weather"]["memory"]["floor"]
    )


def test_altitude_class_and_rounding():
    from champis_api.scoring import altitude_class, round_half_up

    assert altitude_class(0) == 150 and altitude_class(299) == 150 and altitude_class(300) == 450
    assert round_half_up(0.5) == 1 and round_half_up(1.5) == 2 and round_half_up(-0.5) == 0


def test_phi_exact_edges():
    year = 2026
    p = EDULIS["phenology"]
    from datetime import timedelta

    def d(md):
        m, dd = md.split("-")
        return date(year, int(m), int(dd))

    ref = p["altitude_shift"]["reference_m"]
    assert phenology_window(EDULIS, d(p["season"]["start"]) - timedelta(days=1), ref) == 0.0
    assert phenology_window(EDULIS, d(p["season"]["start"]), ref) == PHI_FLOOR
    assert (
        phenology_window(EDULIS, d(p["peak"]["start"]) - timedelta(days=p["ramp_days"]), ref)
        == PHI_FLOOR
    )
    assert phenology_window(EDULIS, d(p["peak"]["start"]), ref) == 1.0
    assert (
        phenology_window(EDULIS, d(p["peak"]["end"]) + timedelta(days=p["ramp_days"]), ref)
        == PHI_FLOOR
    )
    assert phenology_window(EDULIS, d(p["season"]["end"]), ref) == PHI_FLOOR
    assert phenology_window(EDULIS, d(p["season"]["end"]) + timedelta(days=1), ref) == 0.0
