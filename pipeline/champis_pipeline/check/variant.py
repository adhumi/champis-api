"""Variante d'un fichier espèce pour un test d'entrée (spec › Backoffice › Espèces, 12 nov. 2026) :
un facteur optionnel n'entre dans le référentiel que s'il relève l'AUC GBIF d'un département de
référence (skill `species-file`). Le test calcule un second T de l'espèce **en mémoire**, avec le
bloc candidat, sans jamais écrire le fichier espèce ; le raster va dans `data/T-variants/`, jamais
dans `data/T/` ni dans les tuiles.

Première variante : la lisière (`terrain.edge_m` + `weights.edge`, palier 2). Le poids du facteur
est prélevé au prorata sur tous les poids existants, pour que la somme reste 1.
"""

from __future__ import annotations

import copy
import itertools
from pathlib import Path
from typing import Any

from ..config import VARIANTS_DIR
from ..species import species_hash

TRAPEZOID_KEYS = ("min", "opt_low", "opt_high", "max")
WEIGHT_MIN, WEIGHT_MAX = 0.01, 0.5


def parse_edge(text: str) -> dict[str, float]:
    """`min,opt_low,opt_high,max` en mètres → trapèze ; bornes croissantes, positives."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != 4:
        raise ValueError("--edge attend quatre nombres : min,opt_low,opt_high,max (mètres)")
    try:
        values = [float(p) for p in parts]
    except ValueError as e:
        raise ValueError(f"--edge : nombre attendu dans {text!r}") from e
    if values[0] < 0 or any(b < a for a, b in itertools.pairwise(values)):
        raise ValueError(
            "--edge : bornes positives et croissantes (min ≤ opt_low ≤ opt_high ≤ max)"
        )
    if values[3] <= values[0]:
        raise ValueError("--edge : max doit dépasser min")
    return dict(zip(TRAPEZOID_KEYS, values, strict=True))


def check_weight(weight: float) -> float:
    if not WEIGHT_MIN <= weight <= WEIGHT_MAX:
        raise ValueError(f"--edge-weight : entre {WEIGHT_MIN:g} et {WEIGHT_MAX:g}")
    return float(weight)


def edge_variant(
    spec: dict[str, Any], trapezoid: dict[str, float], weight: float
) -> dict[str, Any]:
    """Copie du fichier espèce avec le bloc `edge_m` et le poids `edge`, les autres poids réduits
    au prorata (somme toujours 1). Le fichier d'origine n'est pas touché."""
    weight = check_weight(weight)
    out = copy.deepcopy(spec)
    terrain = out["terrain"]
    terrain["edge_m"] = dict(trapezoid)
    old = {k: v for k, v in terrain["weights"].items() if k != "edge"}
    total = sum(old.values())
    terrain["weights"] = {k: v / total * (1.0 - weight) for k, v in old.items()}
    terrain["weights"]["edge"] = weight
    return out


def edge_tag(trapezoid: dict[str, float], weight: float) -> str:
    """Nom lisible de la variante, dans les noms de fichiers : `edge-0-0-100-400-w0.1`."""
    bounds = "-".join(f"{trapezoid[k]:g}" for k in TRAPEZOID_KEYS)
    return f"edge-{bounds}-w{weight:g}"


def variant_t_path(species_id: str, dept: str, tag: str) -> Path:
    """Le T de la variante : à côté de rien, hors de `data/T/` (que l'inventaire du worker et les
    tuiles lisent), nommé par l'empreinte de l'espèce et la variante."""
    return VARIANTS_DIR / species_id / f"{dept}-{species_hash(species_id)}-{tag}.tif"
