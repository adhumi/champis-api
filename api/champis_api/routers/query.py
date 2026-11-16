"""Paramètre `species` commun à `/weather*` et `/departements` (obligatoire sur `/weather/field`)."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Query

from ..catalog import served_species


def species_query(species: Annotated[list[str] | None, Query()] = None) -> list[str]:
    """Espèces demandées (`species=` répétable), défaut : toutes celles du catalogue
    (`/weather`, `/weather/bbox`, `/departements`).

    Sans doublon et jamais plus longue que le catalogue (correctif du 13 nov. 2026) : chaque
    espèce coûte un calcul par cellule, et `species=x` répété cinq cents fois gelait le service
    plusieurs secondes. Une liste plus longue que le catalogue, qui porte forcément des doublons
    ou des inconnues, est refusée en 422 avant tout travail ; les doublons d'une liste plus
    courte sont retirés, l'ordre de la première apparition gardé.
    """
    known = served_species()  # catalogue : complètes et tuiles publiées (9 nov. 2026)
    if not species:
        return known
    return _checked(species, known)


def required_species_query(species: Annotated[list[str] | None, Query()] = None) -> list[str]:
    """Espèces demandées, **obligatoires** : `GET /weather/field` (décision du 16 nov. 2026).

    Sans `species` (ou avec des valeurs vides seulement), 422 au lieu du champ de tout le
    catalogue : la carte demande toujours les espèces de sa couche, et le champ de toutes les
    espèces sur la France entière est la réponse la plus lourde du service. Mêmes contrôles
    ensuite que `species_query`.
    """
    species = [s for s in species or [] if s.strip()]
    if not species:
        raise HTTPException(
            422,
            "species obligatoire : préciser au moins une espèce "
            "(species=<id>, répétable ; la liste est dans /species)",
        )
    return _checked(species, served_species())


def _checked(species: list[str], known: list[str]) -> list[str]:
    """La liste demandée, bornée au catalogue, sans doublon, sans inconnue."""
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
RequiredSpecies = Annotated[list[str], Depends(required_species_query)]
