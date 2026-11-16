"""Gel du service par les routes météo (correctifs du 13 nov. 2026) : liste d'espèces sans
doublon et bornée, calcul lourd hors de la boucle d'événements, écritures du cache sérialisées
entre fils."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import timedelta

import pytest
from champis_api import coins as coins_module
from champis_api import departements as departements_module
from champis_api.cache import WeatherCache
from champis_api.catalog import served_species
from champis_api.departements import compute
from champis_api.routers import departements as departements_router
from champis_api.routers import weather as weather_router
from champis_api.warm import warm
from champis_pipeline.species import list_species
from conftest import NOW, TODAY

CELL = "c_0930_6680"
J = TODAY.isoformat()
BBOX = {"min_lon": 6.0, "min_lat": 47.0, "max_lon": 6.5, "max_lat": 47.3}
SP = "boletus_edulis"


def _spy(monkeypatch, owner, name: str, seen: list[tuple[str, str]], label: str | None = None):
    """Note, à chaque appel de `owner.name`, s'il a lieu dans la boucle d'événements ou dans un
    fil (aucune boucle ne tourne dans un fil de `offload.run` ou d'`asyncio.to_thread`)."""
    original = getattr(owner, name)

    def spy(*args, **kwargs):
        try:
            asyncio.get_running_loop()
            where = "boucle"
        except RuntimeError:
            where = "fil"
        seen.append((label or name, where))
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, spy)


# ------------------------------------------------------------------ liste d'espèces


@pytest.mark.asyncio
async def test_repeated_species_are_computed_once(api, monkeypatch):
    """`species=x` répété faisait recalculer `x` autant de fois : 500 répétitions, 7 s de gel."""
    asked: list[list[str]] = []
    original = weather_router.cell_weather

    def counting(cell, raw, species, today, detail=True):
        asked.append(list(species))
        return original(cell, raw, species, today, detail=detail)

    monkeypatch.setattr(weather_router, "cell_weather", counting)
    params = [("cell", CELL), ("species", SP), ("species", "boletus_aereus")] + [
        ("species", SP)
    ] * 9
    r = await api.get("/weather", params=params)
    assert r.status_code == 200, r.text
    assert asked == [[SP, "boletus_aereus"]]  # une fois chacune, ordre de première apparition
    assert list(r.json()["species"]) == [SP, "boletus_aereus"]


@pytest.mark.asyncio
async def test_species_list_longer_than_the_catalogue_is_refused(api, grid, client, monkeypatch):
    """Au-delà du nombre d'espèces servies : 422, sans aucun calcul ni lecture."""
    await warm(grid, client)
    await compute(grid, client, client.cache, list_species())
    seen: list[tuple[str, str]] = []
    _spy(monkeypatch, weather_router, "cell_weather", seen)
    _spy(monkeypatch, client.cache, "cell_weather", seen, "lecture")
    _spy(monkeypatch, client.cache, "cell_field", seen, "lecture")
    _spy(monkeypatch, client.cache, "dept_index", seen, "lecture")
    n = len(served_species())
    too_many = [("species", SP)] * (n + 1)
    for path, extra in (
        ("/weather", [("cell", CELL)]),
        ("/weather/bbox", list(BBOX.items())),
        ("/weather/field", [*BBOX.items(), ("day", J)]),  # `species` obligatoire, ici fournie
        ("/departements", []),
    ):
        r = await api.get(path, params=extra + too_many)
        assert r.status_code == 422 and f"{n} au plus" in r.json()["detail"], path
        # à la limite, doublons compris : servie, dédoublonnée
        r = await api.get(path, params=extra + too_many[:-1])
        assert r.status_code == 200, (path, r.text)
    assert {name for name, _ in seen} == {"cell_weather", "lecture"}  # les quatre 200 seulement
    assert (
        await api.get("/weather", params={"cell": CELL, "species": "inconnue"})
    ).status_code == 404
    r = await api.get("/departements", params=[("species", SP), ("species", SP)])
    assert list(next(iter(r.json()["departements"].values()))["species"]) == [SP]


# ------------------------------------------------------------------ hors de la boucle


@pytest.mark.asyncio
async def test_heavy_work_of_public_routes_runs_in_threads(api, grid, client, store, monkeypatch):
    """Calcul des scores, lecture du précalcul, mise en forme : jamais dans la boucle
    d'événements, où ils gelaient toutes les autres requêtes du worker."""
    seen: list[tuple[str, str]] = []
    _spy(monkeypatch, weather_router, "cell_weather", seen, "scores /weather")
    _spy(monkeypatch, weather_router, "_bbox_body", seen)
    _spy(monkeypatch, weather_router, "_field_items", seen)
    _spy(monkeypatch, coins_module, "cell_weather", seen, "scores /coins")
    _spy(monkeypatch, departements_module, "cell_weather", seen, "scores indice")
    _spy(monkeypatch, departements_module, "aggregate", seen)
    _spy(monkeypatch, departements_router, "shape_response", seen)
    cache = client.cache
    for name in ("cell_weather", "cell_field", "dept_index", "put_cell_weather", "put_dept_index"):
        _spy(monkeypatch, cache, name, seen, f"cache.{name}")
    _spy(monkeypatch, store, "coins_of", seen)

    assert (await api.get("/weather", params={"cell": CELL, "species": SP})).status_code == 200
    # emprise froide : calcul à la demande puis rangement ; puis l'indice et le précalcul
    assert (await api.get("/weather/bbox", params=BBOX | {"species": SP})).status_code == 200
    await compute(grid, client, cache, list_species())
    assert (await api.get("/weather/bbox", params=BBOX)).status_code == 200
    assert (
        await api.get("/weather/field", params=BBOX | {"day": J, "species": SP})
    ).status_code == 200
    assert (await api.get("/departements")).status_code == 200
    user = {"X-Champis-User": str(uuid.uuid4())}
    coin = {
        "id": str(uuid.uuid4()),
        "name": "Combe",
        "lat": 47.12,
        "lon": 6.31,
        "target": "cepes",
        "alt_class": 450,
        "t": {SP: 0.9},
    }
    assert (await api.put("/coins", json={"coins": [coin]}, headers=user)).status_code == 200
    r = await api.get("/coins", headers=user)
    assert r.status_code == 200 and r.json()["coins"][0]["score"] is not None

    names = {name for name, _ in seen}
    assert names >= {
        "scores /weather",
        "_bbox_body",
        "_field_items",
        "scores /coins",
        "scores indice",
        "aggregate",
        "shape_response",
        "cache.cell_weather",
        "cache.cell_field",
        "cache.dept_index",
        "cache.put_cell_weather",
        "cache.put_dept_index",
        "coins_of",
    }
    assert [(name, where) for name, where in seen if where != "fil"] == []


@pytest.mark.asyncio
async def test_event_loop_keeps_turning_during_a_slow_computation(api, monkeypatch):
    """Pendant un calcul de 0,3 s, la boucle sert d'autres requêtes : `/health` répond avant."""
    import time

    original = weather_router.cell_weather

    def slow(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)

    monkeypatch.setattr(weather_router, "cell_weather", slow)
    done: list[str] = []

    async def call(name: str, path: str, **params):
        r = await api.get(path, params=params)
        assert r.status_code == 200
        done.append(name)

    slow_call = asyncio.create_task(call("weather", "/weather", cell=CELL, species=SP))
    await asyncio.sleep(0.05)  # le calcul est parti
    t0 = time.perf_counter()
    await call("health", "/health")
    assert time.perf_counter() - t0 < 0.2
    await slow_call
    assert done == ["health", "weather"]


# ------------------------------------------------------------------ cache partagé entre fils


@pytest.mark.parametrize("shared", [True, False])
def test_cache_is_safe_from_several_threads(tmp_path, shared):
    """Plusieurs fils sur un même cache : la connexion principale ne sert qu'un fil à la fois
    (chaque écriture est une transaction entière), les grandes lectures ont leur connexion par fil
    (fichier partagé) ou passent sous le même verrou (base en mémoire). Sans cela, deux lectures
    simultanées de la même requête lisaient l'une avec les paramètres de l'autre."""
    cache = WeatherCache(tmp_path / "cache.db", shared=True) if shared else WeatherCache()
    rows = [
        {
            "date": (TODAY + timedelta(days=k)).isoformat(),
            "by_alt": {"450": {"M_eff": 0.5, "phi": 1.0}},
        }
        for k in range(-3, 8)
    ]
    errors: list[BaseException] = []

    def writer(i: int) -> None:
        try:
            for k in range(40):
                cell = f"c_{i}_{k}"
                cache.put_cell_weather([(cell, SP, False, rows)], TODAY, NOW)
                cache.put(cell, "forecast", "2026-10-17T12", {"dates": []}, NOW)
                cache.meta_set(f"k{i}", str(k))
                assert cache.cell_field([cell], [SP], TODAY)[cell].f == {SP: {"450": 0.5}}
                assert list(cache.cell_weather([cell], [SP])) == [(cell, SP)]
                assert (
                    cache.meta_get(f"k{i}") == str(k) and cache.get(cell, "forecast", "x") is None
                )
        except BaseException as e:  # remonté au fil principal
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert cache.cell_weather_count() == 240 and cache.cell_field_count() == 240 * 11
    assert cache.field_generation == 240
    assert not cache.db.in_transaction
    assert len(cache._readers) == (6 if shared else 0)  # une connexion de lecture par fil
    cache.close()


@pytest.mark.asyncio
async def test_routes_on_a_shared_file_cache_read_through_thread_connections(
    tmp_path, grid, scenario, store
):
    """En service le cache est un fichier en WAL : les fils de calcul y lisent par leur propre
    connexion et voient aussitôt ce que la connexion principale vient de valider."""
    import httpx
    from champis_api.app import State, create_app
    from conftest import make_client

    cache = WeatherCache(tmp_path / "weather_cache.db", shared=True)
    client = make_client(scenario, cache=cache)
    app = create_app(state=State(grid=grid, client=client, cache=cache, store=store))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        r = await api.get("/weather/field", params=BBOX | {"day": J, "species": SP})
        assert r.json()["cells"] == [] and r.json()["unavailable"] == 2
        # calcul à la demande, rangé par la connexion principale…
        r = await api.get("/weather/bbox", params=BBOX | {"species": SP})
        assert len(r.json()["cells"]) == 2 and r.json()["unavailable"] == []
        # … et relu par les connexions des fils : précalcul servi, champ complet
        r = await api.get("/weather/bbox", params=BBOX | {"species": SP, "day": J})
        assert [len(c["species"][SP]) for c in r.json()["cells"]] == [1, 1]
        r = await api.get("/weather/field", params=BBOX | {"day": J, "species": SP})
        assert len(r.json()["cells"]) == 2 and r.json()["unavailable"] == 0
        await compute(grid, client, cache, [SP])
        r = await api.get("/departements", params={"species": SP})
        assert set(r.json()["departements"]) == {"25", "39"}
    assert 1 <= len(cache._readers) <= 4  # au plus une par fil de calcul
    cache.close()
