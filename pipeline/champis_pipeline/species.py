"""Chargement des fichiers espèces, de la table BD Forêt → hôtes et de la palette.

Tout le reste du code reçoit des dicts déjà chargés ; personne ne lit species/ ailleurs.
"""

from __future__ import annotations

import hashlib
import json
import os
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]  # racine de champis-api : data/, .env, reports/
# Référentiel adhumi/champis (species/, mapping/, shared/) : sous-module `champis/` depuis le
# découpage des dépôts (spec › Produit › Étape C, lot C7, 6 nov. 2026) ; CHAMPIS_REFERENTIAL pour
# pointer ailleurs (une copie de travail du référentiel, par exemple).
REFERENTIAL = Path(os.environ.get("CHAMPIS_REFERENTIAL") or ROOT / "champis")
SPECIES_DIR = REFERENTIAL / "species"
MAPPING_FILE = REFERENTIAL / "mapping" / "bdforet_to_hosts.json"
PALETTE_FILE = REFERENTIAL / "shared" / "palette.json"
# Planches des espèces (spec › Produit › Décisions prises › Illustrations des espèces, 8 nov. 2026) :
# une aquarelle <id>.jpg par espèce, générée d'après PROMPTS.md ; requise pour une espèce complète.
ILLUSTRATIONS_DIR = REFERENTIAL / "shared" / "illustrations"
PROMPTS_FILE = ILLUSTRATIONS_DIR / "PROMPTS.md"

ASPECT_CLASSES = ["N", "NE", "E", "SE", "S", "SW", "W", "NW", "flat"]
COVER_CLASSES = ["fermee", "ouverte", "sans_couvert", "plantation", "non_foret"]


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def load_mapping() -> dict[str, Any]:
    return _read(MAPPING_FILE)


@lru_cache(maxsize=1)
def load_palette() -> dict[str, Any]:
    return _read(PALETTE_FILE)


@lru_cache(maxsize=1)
def host_vocabulary() -> list[str]:
    """Hôtes dans un ordre stable : l'index dans cette liste est le code du raster host.tif."""
    return list(load_mapping()["hosts"].keys())


def host_index(host: str) -> int:
    return host_vocabulary().index(host)


def topo_host_vocabulary() -> list[str]:
    """Hôtes hors forêt (BD TOPO, palier 3, 12 nov. 2026), vocabulaire à part : l'index dans cette
    liste est le code du raster host_topo.tif. Jamais de `default` : une espèce les nomme."""
    return [k for k in load_mapping().get("topo_hosts", {}) if k != "description"]


def topo_host_index(host: str) -> int:
    return topo_host_vocabulary().index(host)


@cache
def load_species(species_id: str) -> dict[str, Any]:
    return _read(SPECIES_DIR / f"{species_id}.json")


def list_species() -> list[str]:
    return sorted(p.stem for p in SPECIES_DIR.glob("*.json"))


def illustration_path(species_id: str) -> Path | None:
    """La planche `shared/illustrations/<id>.jpg` du référentiel, ou None si elle manque."""
    path = ILLUSTRATIONS_DIR / f"{species_id}.jpg"
    return path if path.is_file() else None


def completeness(species_id: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """Ce qui manque pour qu'une espèce soit complète : bloc `about`, photo choisie, planche
    (décision d'Adrien des 8 et 9 nov. 2026). Une espèce incomplète existe pour le pipeline et le
    backoffice (tuiles, contrôle GBIF), pas pour les clients : `/species`, l'indice par département,
    les coins et la carte du site ne servent que les espèces complètes. La planche n'est pas un
    prérequis de la CI du référentiel."""
    about = (load_species(species_id) if spec is None else spec).get("about") or {}
    items = {
        "about": bool(about),
        "photo": bool(about.get("photo")),
        "illustration": illustration_path(species_id) is not None,
    }
    return {
        **items,
        "complete": all(items.values()),
        "missing": [k for k, v in items.items() if not v],
    }


def list_complete_species() -> list[str]:
    """Les espèces servies aux clients : celles dont rien ne manque (`completeness`)."""
    return [sid for sid in list_species() if completeness(sid)["complete"]]


def fingerprint_bytes(raw: bytes) -> bytes:
    """Octets soumis à l'empreinte : le fichier tel quel, sans le bloc `about` (la fiche en mots,
    lot 4), que relire ne doit jamais régénérer une tuile. Sans `about`, les octets d'origine."""
    data = json.loads(raw)
    if "about" not in data:
        return raw
    data.pop("about")
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def species_hash(species_id: str) -> str:
    """Empreinte courte du modèle de l'espèce, pour nommer les tuiles et versionner les vecteurs."""
    raw = (SPECIES_DIR / f"{species_id}.json").read_bytes()
    return hashlib.sha256(fingerprint_bytes(raw)).hexdigest()[:8]


def all_species_hash() -> str:
    h = hashlib.sha256()
    for sid in list_species():
        h.update(fingerprint_bytes((SPECIES_DIR / f"{sid}.json").read_bytes()))
    return h.hexdigest()[:8]


# ---------------------------------------------------------------- groupes d'affichage (PMTiles G)

GROUP_CHANNELS = 4  # espèces par fichier G : un canal RGBA chacune


def species_groups(species_ids: list[str] | None = None) -> dict[str, list[str]]:
    """`group.id` → identifiants d'espèces du groupe, triés : c'est l'ordre des canaux des PMTiles
    G (spec › Points ouverts › PMTiles par groupe, 4 nov. 2026). Par défaut toutes les espèces."""
    groups: dict[str, list[str]] = {}
    for sid in sorted(species_ids if species_ids is not None else list_species()):
        groups.setdefault(load_species(sid)["group"]["id"], []).append(sid)
    return dict(sorted(groups.items()))


def group_sets(species_ids: list[str] | None = None) -> list[tuple[str, int, list[str]]]:
    """Fichiers G à produire : `(groupe, partie, espèces)`, au plus `GROUP_CHANNELS` espèces par
    fichier ; au-delà, une seconde partie (suffixe `-2` du nom), pour les lactaires depuis le 8 nov. 2026."""
    out = []
    for gid, ids in species_groups(species_ids).items():
        for i in range(0, len(ids), GROUP_CHANNELS):
            out.append((gid, i // GROUP_CHANNELS + 1, ids[i : i + GROUP_CHANNELS]))
    return out


def group_hash(species_ids: list[str]) -> str:
    """Empreinte courte des hashes d'espèces concaténés dans l'ordre des canaux."""
    return hashlib.sha256("".join(species_hash(s) for s in species_ids).encode()).hexdigest()[:8]
