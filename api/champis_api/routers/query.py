"""Paramètre `species` commun à `/weather*` et `/departements`."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Query

from ..catalog import served_species


def species_query(species: Annotated[list[str] | None, Query()] = None) -> list[str]:
    """Espèces demandées (`species=` répétable), défaut : toutes celles du catalogue.

    Sans doublon et jamais plus longue que le catalogue (correctif du 13 nov. 2026) : chaque
    espèce coûte un calcul par cellule, et `species=x` répété cinq cents fois gelait le service
    plusieurs secondes. Une liste plus longue que le catalogue, qui porte forcément des doublons
    ou des inconnues, est refusée en 422 avant tout travail ; les doublons d'une liste plus
    courte sont retirés, l'ordre de la première apparition gardé.
    """
    known = served_species()  # catalogue : complètes et tuiles publiées (9 nov. 2026)
    if not species:
        return known
    if len(species) > len(known):
        raise HTTPException(
            422, f"{len(species)} espèces demandées, {len(known)} au plus (celles de /species)"
        )
    species = list(dict.fromkeys(species))
    unknown = [s for s in species if s not in known]
    if unknown:
        raise HTTPException(404, f"espèce(s) inconnue(s) : {unknown}")
    return species


Species = Annotated[list[str], Depends(species_query)]
