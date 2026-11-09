"""Catalogue : les espèces que l'API sert aux clients (spec › Produit › Espèces, décisions
d'Adrien des 9 nov. 2026).

Une espèce est servie si elle est **complète** dans le référentiel (bloc `about`, photo choisie,
planche `shared/illustrations/<id>.jpg` : `champis_pipeline.species.completeness`) **et** si ses
**tuiles sont publiées** : un jeu T France la couvre dans le manifeste du bucket
(`tiles/manifest.json`, entrée `kind: T` sans `dept`), quelle que soit son empreinte ; une espèce
dont le fichier a changé reste visible avec ses anciennes tuiles, le backoffice la dit « périmée ».
Sans ce second critère, une espèce nouvellement complète apparaissait dans le sélecteur du site
et de l'app avec une carte vide entre le déploiement de l'API et le rendu de ses tuiles.

Sans bucket configuré (développement, tests, glisser-déposer), le critère des tuiles ne s'applique
pas : le catalogue est celui du référentiel. Un bucket injoignable ne vide pas le catalogue non
plus : le dernier manifeste lu reste en vigueur, ou, s'il n'y en a jamais eu, le critère est
suspendu le temps d'une relecture (`RETRY_S`), avec une trace dans le journal.

Le manifeste est relu toutes les `MANIFEST_TTL_S` secondes par processus, comme `GET /tiles`.
`GET /species`, `/weather`, `/departements`, les coins, la mesure et l'indice par département
passent par `served_species()` ; le pipeline et le backoffice voient toutes les espèces.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from champis_pipeline.species import (
    completeness as referential_completeness,
)
from champis_pipeline.species import (
    list_species,
    load_species,
)
from champis_pipeline.storage import MANIFEST_KEY, BucketLike

log = logging.getLogger(__name__)

MANIFEST_TTL_S = 600
RETRY_S = 60
EMPTY_MANIFEST: dict[str, Any] = {"version": 1, "tiles": [], "depts_by_hash": {}}
ITEMS = ("about", "photo", "illustration", "tiles")

_bucket: BucketLike | None = None
_cache: tuple[float, dict[str, Any] | None] | None = (
    None  # (échéance, manifeste ; None = illisible)
)


def configure(bucket: BucketLike | None) -> None:
    """Le bucket dont le manifeste décide des tuiles publiées ; None = critère inactif."""
    global _bucket, _cache
    _bucket = bucket
    _cache = None


def manifest() -> dict[str, Any] | None:
    """Le manifeste du bucket, en cache `MANIFEST_TTL_S` ; None sans bucket, ou si le bucket est
    injoignable sans manifeste déjà lu (le critère des tuiles est alors suspendu)."""
    global _cache
    if _bucket is None:
        return None
    now = time.monotonic()
    if _cache is not None and _cache[0] > now:
        return _cache[1]
    try:
        m = _bucket.get_json(MANIFEST_KEY) or dict(EMPTY_MANIFEST)
    except Exception:
        previous = _cache[1] if _cache is not None else None
        log.exception(
            "manifeste des tuiles illisible : %s",
            "dernier manifeste conservé" if previous else "critère des tuiles suspendu",
        )
        _cache = (now + RETRY_S, previous)
        return previous
    _cache = (now + MANIFEST_TTL_S, m)
    return m


def published_species(manifest_: dict[str, Any] | None = None) -> set[str] | None:
    """Espèces couvertes par un jeu T France du manifeste ; None quand le critère ne s'applique
    pas (pas de bucket, ou manifeste illisible et jamais lu)."""
    m = manifest() if manifest_ is None else manifest_
    if m is None:
        return None
    return {
        t["species"]
        for t in m.get("tiles", [])
        if t.get("kind", "T") == "T" and t.get("species") and not t.get("dept")
    }


def completeness(
    species_id: str,
    spec: dict[str, Any] | None = None,
    manifest_: dict[str, Any] | None = None,
    published: set[str] | None = None,
) -> dict[str, Any]:
    """Ce qui manque pour qu'une espèce soit servie : les trois critères du référentiel, plus
    `tiles` quand un manifeste s'applique (`manifest_`, ou `published` déjà calculé, sinon le
    manifeste du bucket configuré)."""
    c = referential_completeness(species_id, spec)
    if published is None:
        published = published_species(manifest_)
    if published is None:
        return c
    items = {k: c[k] for k in ITEMS[:3]}
    items["tiles"] = species_id in published
    return {
        **items,
        "complete": all(items.values()),
        "missing": [k for k, v in items.items() if not v],
    }


def served_species() -> list[str]:
    """Les espèces servies aux clients, dans l'ordre de `list_species()`."""
    published = published_species()
    return [sid for sid in list_species() if completeness(sid, published=published)["complete"]]


def served_groups() -> set[str]:
    """Les groupes d'affichage d'au moins une espèce servie."""
    return {
        load_species(s)["group"]["id"] for s in served_species() if load_species(s).get("group")
    }
