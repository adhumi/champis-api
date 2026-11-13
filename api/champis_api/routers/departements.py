"""`GET /departements` : indice par département pour la vue France (spec › Scoring).

Lit ce que la dernière pré-chauffe a calculé (`champis_api.departements.compute`) ; ne déclenche
aucun calcul ni appel Open-Meteo. 503 tant qu'aucun indice n'a été calculé.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from champis_pipeline import SCORING_VERSION
from champis_pipeline.species import all_species_hash
from fastapi import APIRouter, Depends, HTTPException, Request

from .. import offload
from ..cache import WeatherCache
from ..departements import shape_response
from .query import Species

router = APIRouter(prefix="/departements", tags=["departements"])


def _cache(request: Request) -> WeatherCache:
    return request.app.state.champis.cache


@router.get("")
async def get_departements(
    cache: Annotated[WeatherCache, Depends(_cache)],
    species: Species,
    day: date | None = None,
) -> dict[str, Any]:
    """Par département : indice `I = Σ T × M_eff × Φ / Σ T` par espèce et par jour (J−3 → J+7, ou
    `day` seul), avec `coverage` (part de Σ T couverte par la météo) ; par groupe, le maximum des
    espèces et l'espèce `leading` qui le porte (règle 9). Même palette que la carte."""
    meta = cache.dept_index_meta()
    if meta is None:
        raise HTTPException(503, "indice par département pas encore calculé : pré-chauffe requise")
    computed_at, today = meta
    # lecture et mise en forme (jusqu'à ≈ 30 000 lignes : départements × espèces × onze jours)
    # dans un fil, hors de la boucle d'événements (13 nov. 2026)
    departements = await offload.run(_shaped, cache, species, day)
    return {
        "today": today,
        "computed_at": computed_at,
        "scoring_version": SCORING_VERSION,
        "species_hash": all_species_hash(),
        "departements": departements,
    }


def _shaped(cache: WeatherCache, species: list[str], day: date | None) -> dict[str, Any]:
    return shape_response(cache.dept_index(species, day), species)
