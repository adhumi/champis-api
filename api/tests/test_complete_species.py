"""Une espèce est complète quand son fichier porte le bloc `about`, une photo et sa planche
`shared/illustrations/<id>.jpg` (décision d'Adrien des 8 et 9 nov. 2026). La planche n'est pas
un prérequis de la CI : elle conditionne l'affichage. Les clients ne voient que les espèces
complètes ; le pipeline et le backoffice les voient toutes.

Les vingt-sept espèces sont complètes depuis le 9 nov. 2026 : pour exercer la règle, un test retire
la planche d'une espèce témoin sans toucher au référentiel (`hidden_planche`)."""

from __future__ import annotations

import champis_pipeline.species as species_module
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
INCOMPLETE = "craterellus_tubaeformis"  # espèce témoin, privée de sa planche par `hidden_planche`


@pytest.fixture
def hidden_planche(monkeypatch):
    """La planche de l'espèce témoin « manque » : `illustration_path` répond None pour elle seule,
    là où `completeness` et tous ses appelants la cherchent."""
    real = species_module.illustration_path
    monkeypatch.setattr(
        species_module, "illustration_path", lambda sid: None if sid == INCOMPLETE else real(sid)
    )


def test_every_species_is_complete():
    """Référentiel du 9 nov. 2026 : vingt-sept planches, vingt-sept espèces servies."""
    assert list_complete_species() == list_species() and len(list_species()) == 27
    assert completeness(INCOMPLETE)["complete"]


def test_completeness_follows_the_referential(hidden_planche):
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
    assert set(complete) < set(list_species()) and len(complete) == len(list_species()) - 1
    # le backoffice applique la même règle
    assert admin_species.completeness(load_species(INCOMPLETE), INCOMPLETE) == c


@pytest.mark.asyncio
async def test_clients_only_see_complete_species(api, hidden_planche):
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
    # un groupe est visable dès qu'une de ses espèces est complète
    assert CoinIn(**coin, target="chanterelles").target == "chanterelles"
    # l'indice par département groupe les espèces complètes : un groupe mixte ne compte que
    # ses espèces complètes, un groupe entièrement incomplet n'apparaîtrait pas
    groups = groups_of(list_complete_species())
    assert groups["chanterelles"] == ["craterellus_lutescens"]
    assert groups["grisets"] == ["tricholoma_portentosum", "tricholoma_terreum"]
