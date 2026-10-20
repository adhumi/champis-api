"""`GET /species` : fichiers espèces et palette tels quels, pour des clients sans valeur en dur."""

from __future__ import annotations

from typing import Any

from champis_pipeline import SCORING_VERSION
from champis_pipeline.species import all_species_hash, list_species, load_palette, load_species
from fastapi import APIRouter

router = APIRouter(tags=["species"])


@router.get("/species")
async def get_species() -> dict[str, Any]:
    return {
        "scoring_version": SCORING_VERSION,
        "species_hash": all_species_hash(),
        "palette": load_palette(),
        "species": {sid: load_species(sid) for sid in list_species()},
    }
