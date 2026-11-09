"""`GET /species` : fichiers espèces et palette tels quels, pour des clients sans valeur en dur.

Seules les espèces **complètes** sont servies (bloc `about`, photo, planche : décision d'Adrien
du 9 nov. 2026) ; une espèce dont la planche manque existe pour le pipeline et le backoffice, pas
pour le site ni l'app. `species_hash` reste l'empreinte de tous les fichiers (elle versionne les
vecteurs de parité)."""

from __future__ import annotations

from typing import Any

from champis_pipeline import SCORING_VERSION
from champis_pipeline.species import (
    all_species_hash,
    list_complete_species,
    load_palette,
    load_species,
)
from fastapi import APIRouter

router = APIRouter(tags=["species"])


@router.get("/species")
async def get_species() -> dict[str, Any]:
    return {
        "scoring_version": SCORING_VERSION,
        "species_hash": all_species_hash(),
        "palette": load_palette(),
        "species": {sid: load_species(sid) for sid in list_complete_species()},
    }
