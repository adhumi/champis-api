"""Retour terrain : `POST`/`GET`/`DELETE /observations`, identité par en-tête, idempotence, limites."""

from __future__ import annotations

import uuid

import pytest
from champis_api.store import Store
from conftest import TODAY

USER = str(uuid.uuid4())
H = {"X-Champis-User": USER}


def _obs(**over):
    base = {
        "id": str(uuid.uuid4()),
        "species": "boletus_edulis",
        "lat": 47.12,
        "lon": 6.31,
        "accuracy_m": 8.0,
        "date": TODAY.isoformat(),
        "found": True,
        "quantity": 2,
    }
    return base | over


@pytest.mark.asyncio
async def test_identity_header_is_required(api):
    r = await api.get("/observations")
    assert r.status_code == 401 and "X-Champis-User" in r.json()["detail"]
    r = await api.get("/observations", headers={"X-Champis-User": "pas-un-uuid"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_post_is_idempotent_and_private(api):
    a, b = _obs(), _obs(found=False, quantity=None)
    r = await api.post("/observations", json={"observations": [a, b]}, headers=H)
    assert r.status_code == 201, r.text
    assert r.json() == {"inserted": 2, "duplicates": 0}
    # renvoi du même lot après une coupure réseau : rien ne bouge
    r = await api.post("/observations", json={"observations": [a, b, _obs()]}, headers=H)
    assert r.json() == {"inserted": 1, "duplicates": 2}

    r = await api.get("/observations", headers=H)
    body = r.json()
    assert body["count"] == 3
    assert {o["found"] for o in body["observations"]} == {True, False}
    assert all("shared" not in o for o in body["observations"])  # réservé, jamais exposé

    # un autre appareil ne voit rien
    other = {"X-Champis-User": str(uuid.uuid4())}
    assert (await api.get("/observations", headers=other)).json()["count"] == 0


@pytest.mark.asyncio
async def test_validation_rules(api):
    async def post(o):
        return await api.post("/observations", json={"observations": [o]}, headers=H)

    assert (await post(_obs(species="amanita_phalloides"))).status_code == 422
    assert (await post(_obs(found=True, quantity=None))).status_code == 422  # quantité requise
    assert (await post(_obs(found=False, quantity=2))).status_code == 422  # quantité sans objet
    assert (await post(_obs(quantity=4))).status_code == 422
    assert (await post(_obs(lat=60.0))).status_code == 422  # hors France métropolitaine
    assert (await post(_obs(date="2027-06-01"))).status_code == 422  # dans le futur
    assert (
        await api.post("/observations", json={"observations": []}, headers=H)
    ).status_code == 422
    too_many = {"observations": [_obs() for _ in range(201)]}
    assert (await api.post("/observations", json=too_many, headers=H)).status_code == 422


@pytest.mark.asyncio
async def test_delete_erases_everything_of_the_user(api, store: Store):
    await api.post("/observations", json={"observations": [_obs(), _obs()]}, headers=H)
    coin = {
        "id": str(uuid.uuid4()),
        "name": "Combe",
        "lat": 47.12,
        "lon": 6.31,
        "target": "cepes",
        "alt_class": 450,
        "t": {"boletus_edulis": 0.9},
    }
    await api.put("/coins", json={"apns_token": "ab" * 16, "coins": [coin]}, headers=H)
    r = await api.delete("/observations", headers=H)
    assert r.json() == {"deleted": 2}
    assert (await api.get("/observations", headers=H)).json()["count"] == 0
    assert store.coins_of(USER) == [] and store.devices_with_coins() == []


@pytest.mark.asyncio
async def test_without_store_endpoints_are_503(grid, client):
    """Sans base configurée (développement), les endpoints répondent 503, le reste de l'API vit."""
    import httpx
    from champis_api.app import State, create_app

    app = create_app(state=State(grid=grid, client=client, cache=client.cache))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        assert (await c.get("/observations", headers=H)).status_code == 503
        assert (await c.get("/health")).status_code == 200


@pytest.mark.asyncio
async def test_daily_quota_per_identifier(grid, client, store: Store):
    """`CHAMPIS_OBSERVATIONS_PER_DAY` : une fois le quota atteint, 429 avec `Retry-After`, le lot
    entier est refusé (l'app le garde et le renvoie) ; le lot qui franchit le quota passe en
    entier ; un renvoi de lignes connues passe toujours, un autre appareil
    n'est pas touché, et le quota repart le lendemain (heure de Paris)."""
    from datetime import timedelta

    import httpx
    from champis_api.app import State, create_app
    from champis_api.settings import Settings
    from conftest import NOW

    clock = [NOW]
    client.now = lambda: clock[0]
    app = create_app(
        settings=Settings(observations_per_day=3),
        state=State(grid=grid, client=client, cache=client.cache, store=store),
    )
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        first = [_obs(), _obs()]
        r = await c.post("/observations", json={"observations": first}, headers=H)
        assert r.status_code == 201
        # le lot qui franchit le quota passe en entier (l'app renvoie le même lot tant qu'il est
        # refusé : un lot plus grand que le quota ne doit pas rester bloqué sans fin)
        second = [_obs(), _obs()]
        r = await c.post("/observations", json={"observations": second}, headers=H)
        assert r.status_code == 201 and r.json()["inserted"] == 2
        first = [*first, *second]
        # le quota du jour est atteint : rien de nouveau n'est écrit, pas même une ligne
        r = await c.post("/observations", json={"observations": [_obs(), _obs()]}, headers=H)
        assert r.status_code == 429 and "3 observations par jour" in r.json()["detail"]
        # NOW = 14 h 30 à Paris : minuit est dans 9 h 30
        assert 9 * 3600 < int(r.headers["retry-after"]) <= 9.5 * 3600 + 1
        assert (await c.get("/observations", headers=H)).json()["count"] == 4
        # le renvoi d'un lot déjà enregistré (coupure réseau) n'est jamais refusé
        r = await c.post("/observations", json={"observations": first}, headers=H)
        assert r.status_code == 201 and r.json() == {"inserted": 0, "duplicates": 4}
        assert (await c.post("/observations", json={"observations": [_obs()]}, headers=H)).status_code == 429  # fmt: skip
        # un autre appareil a son propre quota
        other = {"X-Champis-User": str(uuid.uuid4())}
        r = await c.post("/observations", json={"observations": [_obs()]}, headers=other)
        assert r.status_code == 201
        # le lendemain, le lot refusé passe
        clock[0] = NOW + timedelta(hours=10)
        r = await c.post("/observations", json={"observations": [_obs(), _obs()]}, headers=H)
        assert r.status_code == 201 and r.json()["inserted"] == 2
    # sans quota (0), rien n'est refusé
    assert store.add_observations(USER, [_row() for _ in range(10)], NOW, quota=0)["inserted"] == 10


def _row():
    from datetime import date

    return _obs() | {"date": date.fromisoformat(_obs()["date"])}
