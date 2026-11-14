"""Cache en mémoire du champ de `/weather/field` (`champis_api.field_cache`, 13 nov. 2026) :
mêmes réponses qu'une lecture directe, plus de relecture tant que le précalcul n'a pas changé."""

from __future__ import annotations

import threading
import time

import pytest
from champis_api.departements import compute
from champis_api.field_cache import ENTRY_OVERHEAD, FieldCache, FieldEntry
from champis_api.warm import warm
from champis_pipeline.species import list_species
from conftest import NOW, TODAY

J = TODAY.isoformat()
BBOX_FR = {"min_lon": -6.0, "min_lat": 41.0, "max_lon": 10.0, "max_lat": 52.0}
ONE_CELL = {"min_lon": 6.30, "min_lat": 47.0, "max_lon": 6.32, "max_lat": 47.3}
NOWHERE = {"min_lon": 0.0, "min_lat": 43.0, "max_lon": 0.5, "max_lat": 43.5}
SP = "boletus_edulis"


def _entry(n: int = 1, frag: bytes = b"{}") -> FieldEntry:
    return FieldEntry.of({f"c{i}": (frag, "2026-10-17T03:00:00+00:00") for i in range(n)})


def test_entry_is_built_on_second_demand_or_at_once_when_eager():
    cache = FieldCache()
    built: list[str] = []

    def build() -> FieldEntry:
        built.append("x")
        return _entry()

    assert cache.get("v1", "cepes", build) is None  # première demande : à servir directement
    assert built == [] and len(cache) == 0
    first = cache.get("v1", "cepes", build)  # deuxième : construite et gardée
    assert first is not None and built == ["x"]
    assert cache.get("v1", "cepes", build) is first and built == ["x"]
    # une emprise qui couvre la grille construit tout de suite
    assert cache.get("v1", "girolles", build, eager=True) is not None and built == ["x", "x"]
    assert len(cache) == 2 and cache.builds == 2


def test_version_change_drops_everything():
    cache = FieldCache()
    assert cache.get("v1", "k", _entry, eager=True) is not None
    assert cache.get("v1", "autre", _entry) is None  # vue une fois sous v1
    assert cache.get("v2", "k", _entry) is None and len(cache) == 0 and cache.bytes == 0
    assert cache.get("v2", "autre", _entry) is None  # le souvenir de v1 est parti aussi
    assert cache.get("v2", "k", _entry) is not None


def test_size_is_bounded_least_recently_served_goes_first():
    size = _entry(10, b"x" * 100).size
    assert size == 10 * (100 + ENTRY_OVERHEAD)
    cache = FieldCache(max_bytes=2 * size)
    for key in ("a", "b"):
        cache.get("v", key, lambda: _entry(10, b"x" * 100), eager=True)
    cache.get("v", "a", _entry)  # « a » resservie : « b » est la moins récente
    cache.get("v", "c", lambda: _entry(10, b"x" * 100), eager=True)
    assert cache.bytes == 2 * size and len(cache) == 2
    built: list[str] = []
    cache.get("v", "a", lambda: built.append("a") or _entry(), eager=True)
    assert built == []  # toujours là
    cache.get("v", "b", lambda: built.append("b") or _entry(), eager=True)
    assert built == ["b"]  # partie, reconstruite
    # une entrée plus grosse que le plafond est servie mais pas gardée ; plafond 0 : pas de cache
    big = FieldCache(max_bytes=10)
    assert big.get("v", "k", lambda: _entry(10, b"x" * 100), eager=True) is not None
    assert len(big) == 0 and big.bytes == 0
    assert FieldCache(max_bytes=0).get("v", "k", _entry, eager=True) is None


def test_concurrent_demands_build_once():
    cache = FieldCache()
    built: list[int] = []

    def build() -> FieldEntry:
        built.append(1)
        time.sleep(0.05)
        return _entry()

    got: list[FieldEntry | None] = []
    threads = [
        threading.Thread(target=lambda: got.append(cache.get("v", "k", build, eager=True)))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(built) == 1 and len({id(e) for e in got}) == 1 and got[0] is not None


async def _precompute(grid, client) -> None:
    assert (await warm(grid, client)).ok
    await compute(grid, client, client.cache, list_species())


@pytest.mark.asyncio
async def test_field_answers_are_the_same_with_and_without_the_cache(api, app, grid, client):
    """Corps identiques à l'octet près, que le champ vienne du cache en mémoire ou d'une lecture
    de l'emprise : le contrat de `/weather/field` ne change pas."""
    await _precompute(grid, client)
    fields = app.state.champis.field_cache
    queries = [
        BBOX_FR | {"day": J},
        BBOX_FR | {"day": J, "species": SP},
        [*ONE_CELL.items(), ("day", J), ("species", "cantharellus_cibarius"), ("species", SP)],
        NOWHERE | {"day": J, "species": SP},
    ]
    cached = [(await api.get("/weather/field", params=q)).content for q in queries]
    cached_again = [(await api.get("/weather/field", params=q)).content for q in queries]
    assert fields.builds >= 3 and len(fields) >= 3
    fields.max_bytes = 0  # CHAMPIS_FIELD_CACHE_MB=0 : lecture directe à chaque requête
    direct = [(await api.get("/weather/field", params=q)).content for q in queries]
    assert cached == direct == cached_again
    body = (await api.get("/weather/field", params=queries[2])).json()
    assert [c["id"] for c in body["cells"]] == ["c_0930_6680"] and body["unavailable"] == 0
    assert set(body["cells"][0]["f"]) == {SP, "cantharellus_cibarius"}
    assert set(body) == {"today", "computed_at", "day", "scoring_version", "cells", "unavailable"}


@pytest.mark.asyncio
async def test_field_is_read_once_until_the_precomputation_changes(api, app, grid, client):
    await _precompute(grid, client)
    cache, fields = client.cache, app.state.champis.field_cache
    statements: list[str] = []
    cache.db.set_trace_callback(statements.append)

    async def reads(params) -> int:
        statements.clear()
        r = await api.get("/weather/field", params=params)
        assert r.status_code == 200 and r.json()["cells"]
        return len([q for q in statements if "cell_field" in q])

    try:
        france = BBOX_FR | {"day": J, "species": SP}
        assert await reads(france) == 1  # couvre la grille : construit tout de suite
        assert await reads(france) == 0
        assert await reads(ONE_CELL | {"day": J, "species": SP}) == 0  # autre emprise, même entrée
        assert fields.builds == 1
        # le précalcul change (calcul à la demande rangé, pré-chauffe, instantané importé) : relu
        rows = cache.cell_weather(["c_0930_6680"], [SP])["c_0930_6680", SP].rows
        cache.put_cell_weather([("c_0930_6680", SP, True, rows)], TODAY, NOW)
        statements.clear()
        r = await api.get("/weather/field", params=france)
        assert len([q for q in statements if "FROM cell_field" in q]) == 1
        assert next(c for c in r.json()["cells"] if c["id"] == "c_0930_6680")["stale"] is True
        cache.meta_set("cache_published", "2026-10-17T13:00:00+00:00")
        assert await reads(france) == 1
        assert await reads(france) == 0
    finally:
        cache.db.set_trace_callback(None)
