"""Une espèce est complète quand son fichier porte le bloc `about`, une photo et sa planche
`shared/illustrations/<id>.jpg` (décision d'Adrien des 8 et 9 nov. 2026). La planche n'est pas
un prérequis de la CI : elle conditionne l'affichage. Les clients ne voient que les espèces
complètes ; le pipeline et le backoffice les voient toutes."""

from __future__ import annotations

import pytest
from champis_api.admin import species as admin_species
from champis_api.departements import groups_of
from champis_api.routers.coins import CoinIn
from champis_pipeline.species import (
    completeness,
    list_complete_species,
    list_species,
    load_species,
)
from pydantic import ValidationError

COMPLETE = "boletus_edulis"
INCOMPLETE = "tricholoma_terreum"  # ajoutée le 8 nov. 2026, planche à générer


def test_completeness_follows_the_referential():
    assert completeness(COMPLETE) == {
        "about": True,
        "photo": True,
        "illustration": True,
        "complete": True,
        "missing": [],
    }
    c = completeness(INCOMPLETE)
    assert c["about"] and c["photo"] and not c["illustration"]
    assert not c["complete"] and c["missing"] == ["illustration"]
    complete = list_complete_species()
    assert COMPLETE in complete and INCOMPLETE not in complete
    assert set(complete) < set(list_species()) and len(complete) == 10
    # le backoffice applique la même règle
    assert admin_species.completeness(load_species(INCOMPLETE), INCOMPLETE) == c


@pytest.mark.asyncio
async def test_clients_only_see_complete_species(api):
    r = await api.get("/species")
    served = r.json()["species"]
    assert COMPLETE in served and INCOMPLETE not in served
    # demander explicitement une espèce incomplète : inconnue pour la météo comme pour l'indice
    r = await api.get(f"/weather?lat=47.19&lon=6.37&species={INCOMPLETE}")
    assert r.status_code in (400, 404) and INCOMPLETE in r.json()["detail"]
    r = await api.get(f"/departements?species={INCOMPLETE}")
    assert r.status_code in (400, 404)
    # un coin ne peut viser qu'une espèce ou un groupe affichable
    coin = {
        "id": "0bd3d7a6-2f0b-4d3e-9d4e-0d4c2a9b1e11",
        "name": "test",
        "lat": 47.19,
        "lon": 6.37,
        "alt_class": 450,
        "t": {COMPLETE: 0.8},  # les T lus dans les tuiles sont aussi d'espèces du catalogue
    }
    assert CoinIn(**coin, target=COMPLETE).target == COMPLETE
    with pytest.raises(ValidationError, match="target inconnu"):
        CoinIn(**coin, target=INCOMPLETE)
    with pytest.raises(ValidationError, match="target inconnu"):
        CoinIn(**coin, target="grisets")  # groupe dont aucune espèce n'est complète
    # l'indice par département groupe les espèces complètes : un groupe entièrement incomplet
    # n'apparaît pas, un groupe mixte ne compte que ses espèces complètes
    groups = groups_of(list_complete_species())
    assert "grisets" not in groups and groups["girolles"] == [
        "cantharellus_alborufescens",
        "cantharellus_cibarius",
        "cantharellus_pallens",
    ]
