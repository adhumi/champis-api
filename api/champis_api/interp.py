"""Interpolation de la météo entre cellules (spec › Scoring › Interpolation de la météo entre
cellules, 13 nov. 2026).

La valeur d'une cellule est celle de son centre ; entre les centres, `f = M_eff × Φ` est interpolé
en bilinéaire dans la grille Lambert-93 de 5 km. Les poids sont renormalisés sur les coins qui ont
une valeur, et la cellule qui contient le point doit en avoir une (sinon : pas de météo).

Référence des portages Swift (`ChampisScoring`) et JavaScript (`site.js`), validés par
`shared/vectors/interp.json`. La projection est la formule directe (GRS80), sans pyproj, pour que
les trois langages fassent le même calcul.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

CELL_M = 5000.0

# Lambert-93 (RGF93, conique conforme sécante 44° / 49°, origine 46,5° N 3° E) : constantes IGN.
_E = 0.0818191910428158  # première excentricité GRS80
_N = 0.7256077650532670
_C = 11754255.426096
_XS = 700000.0
_YS = 12655612.049876
_LON0 = math.radians(3.0)


def lambert93(lat: float, lon: float) -> tuple[float, float]:
    """(x, y) Lambert-93 en mètres d'un point WGS84 en degrés."""
    phi = math.radians(lat)
    s = math.sin(phi)
    iso = math.atanh(s) - _E * math.atanh(_E * s)  # latitude isométrique
    r = _C * math.exp(-_N * iso)
    gamma = _N * (math.radians(lon) - _LON0)
    return _XS + r * math.sin(gamma), _YS - r * math.cos(gamma)


def cell_id(i: int, j: int) -> str:
    """Identifiant de la cellule d'indices (i, j) : coin sud-ouest en km (`c_0930_6680`)."""
    return f"c_{i * 5:04d}_{j * 5:04d}"


@dataclass(frozen=True)
class Corners:
    """Les quatre cellules dont les centres encadrent un point, dans l'ordre SO, SE, NO, NE, avec
    leurs poids bilinéaires (somme 1), et la cellule qui contient le point."""

    containing: str
    ids: tuple[str, str, str, str]
    weights: tuple[float, float, float, float]


def corners(lat: float, lon: float) -> Corners:
    x, y = lambert93(lat, lon)
    u = x / CELL_M - 0.5  # en unités de cellule, 0 au centre de la cellule d'indice 0
    v = y / CELL_M - 0.5
    i0 = math.floor(u)
    j0 = math.floor(v)
    fx = u - i0
    fy = v - j0
    return Corners(
        containing=cell_id(math.floor(x / CELL_M), math.floor(y / CELL_M)),
        ids=(
            cell_id(i0, j0),
            cell_id(i0 + 1, j0),
            cell_id(i0, j0 + 1),
            cell_id(i0 + 1, j0 + 1),
        ),
        weights=((1 - fx) * (1 - fy), fx * (1 - fy), (1 - fx) * fy, fx * fy),
    )


def interpolate(c: Corners, values: Mapping[str, float | None]) -> float | None:
    """Moyenne des coins qui ont une valeur, pondérée par leurs poids renormalisés ; `None` si la
    cellule qui contient le point n'en a pas (ses pixels restent sans météo)."""
    if values.get(c.containing) is None:
        return None
    num = 0.0
    den = 0.0
    for cid, w in zip(c.ids, c.weights, strict=True):
        val = values.get(cid)
        if val is not None:
            num += w * val
            den += w
    return num / den
