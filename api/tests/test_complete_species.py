"""Une espèce est complète quand son fichier porte le bloc `about`, une photo et sa planche
`shared/illustrations/<id>.jpg` (décision d'Adrien des 8 et 9 nov. 2026). La planche n'est pas
un prérequis de la CI : elle conditionne l'affichage. Les clients ne voient que les espèces du
catalogue (`champis_api.catalog`) : complètes et, quand un bucket est configuré, dont les tuiles T
France sont publiées ; le pipeline et le backoffice les voient toutes.

Les vingt-sept espèces sont complètes depuis le 9 nov. 2026 : pour exercer la règle, un test retire
la planche d'une espèce témoin sans toucher au référentiel (`hidden_planche`)."""

from __future__ import annotations

import champis_pipeline.species as species_module
import httpx
import pytest
from champis_api import catalog
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


class _FakeBucket:
    """Un bucket qui n'a que son manifeste ; `fail` simule un bucket injoignable."""

    def __init__(self, manifest, fail: bool = False):
        self.manifest = manifest
        self.fail = fail
        self.reads = 0

    def get_json(self, key):
        self.reads += 1
        if self.fail:
            raise ConnectionError("bucket injoignable")
        return self.manifest if key == "tiles/manifest.json" else None

    def presign(self, key, expires_s):
        return f"https://bucket.test/{key}"


def _manifest(*species: str, dept: str | None = None) -> dict:
    tiles = [
        {
            "kind": "T",
            "species": sid,
            "hash": "0" * 8,
            "scoring_version": "1.0.0",
            "depts_hash": "abcdef",
            "name": f"T-{sid}-00000000-p1.0.0-dabcdef.pmtiles",
            "key": f"tiles/T-{sid}-00000000-p1.0.0-dabcdef.pmtiles",
            "bytes": 1,
            **({"dept": dept} if dept else {}),
        }
        for sid in species
    ]
    tiles.append(
        {"kind": "A", "class_m": 300, "depts_hash": "abcdef", "name": "A", "key": "k", "bytes": 1}
    )
    return {
        "version": 1,
        "generated": "2026-11-09T00:00:00+00:00",
        "depts_by_hash": {"abcdef": ["25"]},
        "tiles": tiles,
    }


def test_catalog_requires_published_tiles_when_a_bucket_is_configured():
    """Spec › Produit › Espèces (9 nov. 2026) : avec un bucket, seule une espèce dont un jeu T France
    est dans le manifeste est servie ; un jeu par département ne suffit pas ; sans bucket, le
    critère ne s'applique pas."""
    assert catalog.published_species() is None and catalog.served_species() == list_species()
    catalog.configure(_FakeBucket(_manifest(COMPLETE, "russula_virescens")))
    assert catalog.published_species() == {COMPLETE, "russula_virescens"}
    assert catalog.served_species() == [COMPLETE, "russula_virescens"]
    assert catalog.served_groups() == {"cepes", "russules"}
    c = catalog.completeness(INCOMPLETE)
    assert c["about"] and c["photo"] and c["illustration"] and not c["tiles"]
    assert not c["complete"] and c["missing"] == ["tiles"]
    assert catalog.completeness(COMPLETE)["complete"]
    # un jeu d'un seul département (hors ligne de l'app) ne fait pas une espèce servie
    catalog.configure(_FakeBucket(_manifest(INCOMPLETE, dept="25")))
    assert INCOMPLETE not in catalog.served_species()
    # le backoffice applique la même règle quand il a le manifeste ; sans manifeste ni bucket,
    # les trois critères du référentiel seulement
    assert admin_species.completeness(load_species(INCOMPLETE), INCOMPLETE, _manifest())[
        "missing"
    ] == ["tiles"]
    catalog.configure(None)
    assert admin_species.completeness(load_species(INCOMPLETE), INCOMPLETE)["complete"]


def test_catalog_keeps_serving_when_the_bucket_is_unreachable(monkeypatch):
    """Un bucket injoignable ne vide pas le catalogue : dernier manifeste conservé, sinon critère
    suspendu ; le manifeste est relu après le délai, pas à chaque requête."""
    bucket = _FakeBucket(_manifest(COMPLETE))
    catalog.configure(bucket)
    assert catalog.served_species() == [COMPLETE] and bucket.reads == 1
    assert catalog.served_species() == [COMPLETE] and bucket.reads == 1  # en cache
    clock = [0.0]
    monkeypatch.setattr(catalog.time, "monotonic", lambda: clock[0])
    catalog.configure(bucket)
    bucket.reads = 0
    assert catalog.served_species() == [COMPLETE] and bucket.reads == 1
    bucket.fail = True
    clock[0] += catalog.MANIFEST_TTL_S + 1
    assert catalog.served_species() == [COMPLETE] and bucket.reads == 2  # dernier manifeste gardé
    catalog.configure(_FakeBucket(_manifest(COMPLETE), fail=True))
    assert catalog.published_species() is None  # jamais lu : critère suspendu
    assert catalog.served_species() == list_species()


@pytest.mark.asyncio
async def test_api_serves_only_species_with_published_tiles(grid, scenario):
    """De bout en bout : `/species`, `/weather`, `/departements`, `/tiles` et un coin suivent le
    manifeste du bucket que `create_app` donne au catalogue."""
    from champis_api.app import State, create_app
    from conftest import make_client

    client = make_client(scenario)
    bucket = _FakeBucket(_manifest(COMPLETE))
    app = create_app(state=State(grid=grid, client=client, cache=client.cache, bucket=bucket))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c,
    ):
        assert list((await c.get("/species")).json()["species"]) == [COMPLETE]
        r = await c.get(f"/weather?lat=47.19&lon=6.37&species={INCOMPLETE}")
        assert r.status_code == 404 and INCOMPLETE in r.json()["detail"]
        assert (await c.get(f"/departements?species={INCOMPLETE}")).status_code == 404
        assert len((await c.get("/tiles")).json()["tiles"]) == 2
        coin = {
            "id": "0bd3d7a6-2f0b-4d3e-9d4e-0d4c2a9b1e11",
            "name": "test",
            "lat": 47.19,
            "lon": 6.37,
            "alt_class": 450,
            "t": {COMPLETE: 0.8},
        }
        assert CoinIn(**coin, target="cepes").target == "cepes"
        with pytest.raises(ValidationError, match="target inconnu"):
            CoinIn(**coin, target=INCOMPLETE)
