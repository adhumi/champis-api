"""Potentiel terrain T — implémentation de référence (voir species/SCHEMA.md, skill champis-model).

Deux entrées : `trapezoid` (scalaire ou numpy) et `terrain_potential` (vectorisé sur des blocs
de rasters). Aucune valeur d'espèce ici : tout vient du JSON.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

from .species import ASPECT_CLASSES, COVER_CLASSES, host_vocabulary

PH_FLOOR = 0.15
MISSING_FACTOR = 0.5
NODATA_U8 = 255
NODATA_U16 = 65535


def trapezoid(x: Any, t: dict[str, float]) -> Any:
    """Score trapézoïdal : 0 hors [min, max], 1 dans [opt_low, opt_high], linéaire entre.

    Fonctionne sur un float ou un ndarray. Une rampe de largeur nulle (min == opt_low)
    est une marche : x == min vaut 1.
    """
    lo, ol, oh, hi = t["min"], t["opt_low"], t["opt_high"], t["max"]
    x = np.asarray(x, dtype=np.float64)
    out = np.zeros_like(x)
    inside = (x >= ol) & (x <= oh)
    out[inside] = 1.0
    if ol > lo:
        rising = (x >= lo) & (x < ol)
        out[rising] = (x[rising] - lo) / (ol - lo)
    if hi > oh:
        falling = (x > oh) & (x <= hi)
        out[falling] = (hi - x[falling]) / (hi - oh)
    return float(out) if out.ndim == 0 else out


def _lookup(table: dict[str, float], keys: list[str], default: float) -> NDArray[np.float64]:
    return np.array([float(table.get(k, default)) for k in keys], dtype=np.float64)


def terrain_factors(
    species: dict[str, Any],
    host: NDArray[np.uint8],
    cover: NDArray[np.uint8],
    ph10: NDArray[np.uint8],
    alt_m: NDArray[np.uint16],
    slope_deg: NDArray[np.uint8],
    aspect: NDArray[np.uint8],
) -> dict[str, NDArray[np.float64]]:
    """Chaque facteur de T séparément (host, cover, ph, altitude, slope, aspect), même codage que
    `terrain_potential`. Sert au calcul de T et au diagnostic (`champis-pipeline explain`)."""
    t = species["terrain"]
    if "default" not in t["hosts"]:
        raise ValueError(f"{species['id']}: hosts.default manquant")
    host_w = _lookup(t["hosts"], host_vocabulary(), t["hosts"]["default"])
    cover_w = _lookup(t["cover"], COVER_CLASSES, MISSING_FACTOR)
    aspect_w = _lookup(t["aspect"], ASPECT_CLASSES, MISSING_FACTOR)

    valid_host = host < len(host_w)
    f_host = np.where(valid_host, host_w[np.minimum(host, len(host_w) - 1)], 0.0)
    valid_cover = cover < len(cover_w)
    f_cover = np.where(valid_cover, cover_w[np.minimum(cover, len(cover_w) - 1)], MISSING_FACTOR)
    valid_ph = ph10 != NODATA_U8
    f_ph = np.where(valid_ph, np.maximum(trapezoid(ph10 / 10.0, t["ph"]), PH_FLOOR), MISSING_FACTOR)
    valid_alt = alt_m != NODATA_U16
    f_alt = np.where(
        valid_alt, trapezoid(alt_m.astype(np.float64), t["altitude_m"]), MISSING_FACTOR
    )
    valid_slope = slope_deg != NODATA_U8
    f_slope = np.where(
        valid_slope, trapezoid(slope_deg.astype(np.float64), t["slope_deg"]), MISSING_FACTOR
    )
    valid_aspect = aspect < len(aspect_w)
    f_aspect = np.where(
        valid_aspect, aspect_w[np.minimum(aspect, len(aspect_w) - 1)], MISSING_FACTOR
    )
    return {
        "host": f_host,
        "cover": f_cover,
        "ph": f_ph,
        "altitude": f_alt,
        "slope": f_slope,
        "aspect": f_aspect,
    }


def terrain_potential(
    species: dict[str, Any],
    host: NDArray[np.uint8],
    cover: NDArray[np.uint8],
    ph10: NDArray[np.uint8],
    alt_m: NDArray[np.uint16],
    slope_deg: NDArray[np.uint8],
    aspect: NDArray[np.uint8],
) -> NDArray[np.float32]:
    """T pour un bloc de pixels. Entrées = codes des rasters du pipeline (voir skill geo-pipeline).

    - host : index dans host_vocabulary(), 255 = hors forêt / nodata → T = 0
    - cover : index dans COVER_CLASSES, 255 = inconnu → facteur 0,5
    - ph10 : pH × 10, 255 = inconnu → facteur 0,5
    - alt_m : mètres, 65535 = inconnu → facteur 0,5 (mais pas de règle dure)
    - slope_deg : degrés, 255 = inconnu → 0,5
    - aspect : index dans ASPECT_CLASSES (8 = flat), 255 = inconnu → 0,5
    """
    t = species["terrain"]
    f = terrain_factors(species, host, cover, ph10, alt_m, slope_deg, aspect)
    result = f["host"] * weighted_sum(species, f)
    # règle dure : altitude connue et strictement hors [min, max] → 0
    alt_f = alt_m.astype(np.float64)
    valid_alt = alt_m != NODATA_U16
    hard_alt = valid_alt & ((alt_f < t["altitude_m"]["min"]) | (alt_f > t["altitude_m"]["max"]))
    result = np.where(hard_alt, 0.0, result)
    return np.clip(result, 0.0, 1.0).astype(np.float32)


def weighted_sum(species: dict[str, Any], f: dict[str, NDArray[np.float64]]) -> NDArray[np.float64]:
    """Σ(poids × facteur) sur cover, ph, altitude, aspect, slope (l'hôte est multiplicatif)."""
    w = species["terrain"]["weights"]
    return (
        w["cover"] * f["cover"]
        + w["ph"] * f["ph"]
        + w["altitude"] * f["altitude"]
        + w["aspect"] * f["aspect"]
        + w["slope"] * f["slope"]
    )


def encode_u8(
    t: NDArray[np.float32], nodata_mask: NDArray[np.bool_] | None = None
) -> NDArray[np.uint8]:
    """T ∈ [0, 1] → uint8 0–254 ; 255 = nodata."""
    out = np.rint(t * 254.0).astype(np.uint8)
    if nodata_mask is not None:
        out = np.where(nodata_mask, NODATA_U8, out).astype(np.uint8)
    return out


def decode_u8(v: NDArray[np.uint8]) -> NDArray[np.float32]:
    return np.where(v == NODATA_U8, np.nan, v / 254.0).astype(np.float32)
