"""Lot BO2 (spec › Backoffice › Usage, tranché par Adrien le 8 nov. 2026) : appareils actifs par
empreinte HMAC et par jour, jamais l'identifiant ; part de chaque espèce et groupe sur la carte,
app et site séparés ; le tout exposé par `GET /admin/api/usage`."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from champis_api import counts
from champis_api.app import State, create_app
from champis_api.settings import Settings
from champis_api.store import Store
from champis_pipeline.species import list_complete_species, species_groups

D = counts.today()
USER = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
KEY = "une-cle-de-test-suffisamment-longue-0123456789"
SECRET = "un-secret-de-mesure"


def test_device_hash_is_keyed_short_and_changes_every_day():
    h = counts.device_hash(SECRET, USER, D)
    assert len(h) == 32 and h == counts.device_hash(SECRET, USER.upper(), D)  # même identifiant
    assert h != counts.device_hash(SECRET, OTHER, D)
    assert h != counts.device_hash("autre secret", USER, D)
    assert USER not in h
    # salée par le jour : le même appareil n'a pas la même empreinte le lendemain
    assert h != counts.device_hash(SECRET, USER, D + timedelta(days=1))
    # l'ancienne empreinte, la même tous les jours, se reconnaît : 32 chiffres hexadécimaux
    old = counts.legacy_device_hash(SECRET, USER)
    assert len(old) == 32 and int(old, 16) >= 0 and h.startswith("j") and h != old
    prints = counts.device_prints(SECRET, USER, D)
    assert prints[0] == old and prints[1] == h and len(set(prints)) == 402  # 13 mois + l'ancienne
    assert counts.device_hash(SECRET, USER, D - timedelta(days=400)) in prints
    assert counts.valid_user_id(USER) == USER and counts.valid_user_id("x") is None
    assert counts.valid_user_id(None) is None


def test_tracker_writes_each_device_once_a_day():
    t = counts.DeviceDayTracker()
    assert t.first_today(D, "a") and not t.first_today(D, "a") and t.first_today(D, "b")
    assert t.first_today(D + timedelta(days=1), "a")  # nouveau jour : on recompte
    assert not t.first_today(D + timedelta(days=1), "a")


def test_tracker_is_bounded():
    t = counts.DeviceDayTracker(max_entries=2)
    assert t.first_today(D, "a") and t.first_today(D, "b") and t.first_today(D, "c")
    assert list(t.seen) == ["b", "c"]  # la plus ancienne est partie
    assert t.first_today(D, "a") and not t.first_today(D, "c")


def test_layer_targets_group_species_or_all():
    groups = {"cepes": ["boletus_aereus", "boletus_edulis"], "girolles": ["cantharellus_cibarius"]}
    assert counts.layer_targets(["boletus_edulis", "boletus_aereus"], groups) == ["cepes"]
    assert counts.layer_targets(["cantharellus_cibarius"], groups) == ["girolles"]
    assert counts.layer_targets(["boletus_edulis"], groups) == ["boletus_edulis"]
    assert counts.layer_targets(["boletus_edulis", "cantharellus_cibarius"], groups) == [
        "boletus_edulis",
        "cantharellus_cibarius",
    ]
    assert counts.layer_targets([], groups) == ["toutes"]
    assert counts.layer_counter("app", "cepes") == "layer_app:cepes"


def test_layer_shares_sum_over_a_window():
    data = {
        D: {"layer_app:cepes": 3, "layer_web:cepes": 1, "web_map_opens": 9},
        D - timedelta(days=2): {"layer_app:girolles": 2},
        D - timedelta(days=10): {"layer_app:cepes": 100},
    }
    assert counts.layer_shares(data, D, 7) == {
        "app": {"cepes": 3, "girolles": 2},
        "web": {"cepes": 1},
    }
    assert counts.layer_shares(data, D, 30)["app"]["cepes"] == 103


def test_store_counts_devices_per_day_only():
    store = Store("sqlite://")
    assert store.mark_device_day(D, "ja") and not store.mark_device_day(D, "ja")
    store.mark_device_day(D, "jb")
    store.mark_device_day(D - timedelta(days=3), "jc")
    store.mark_device_day(D - timedelta(days=20), "0f")  # ancienne empreinte
    store.mark_device_day(D - timedelta(days=500), "0f")
    assert store.active_devices(D) == 2
    assert store.active_devices(D - timedelta(days=3)) == 1
    assert store.active_devices(D - timedelta(days=1)) == 0
    assert store.daily_active_devices(D - timedelta(days=3)) == {D - timedelta(days=3): 1, D: 2}
    assert not store.has_legacy_device_rows(D)
    assert store.has_legacy_device_rows(D - timedelta(days=20))
    assert store.table_counts()["device_days"] == 5
    assert store.purge_device_days(D - timedelta(days=400)) == 1
    assert store.table_counts()["device_days"] == 4
    store.close()


def test_tracker_asks_once_a_day_whether_the_day_is_legacy():
    t = counts.DeviceDayTracker()
    asked = []

    def probe(answer):
        return lambda: asked.append(answer) or answer

    assert t.legacy(D, probe(True)) and t.legacy(D, probe(False))  # une seule lecture par jour
    assert not t.legacy(D + timedelta(days=1), probe(False))
    assert asked == [True, False]
    t.first_today(D, "ja")
    t.forget(["ja", "absente"])
    assert t.first_today(D, "ja")


def test_morning_notification_records_yesterdays_actives_and_purges():
    store = Store("sqlite://")
    yesterday = D - timedelta(days=1)
    store.mark_device_day(yesterday, "a")
    store.mark_device_day(yesterday, "b")
    store.mark_device_day(D - timedelta(days=500), "z")
    counts.record_notify(store, sent=1, failed=0, day=D)
    assert store.counts_since(yesterday)[yesterday] == {"active_devices": 2}
    assert store.table_counts()["device_days"] == 2  # la vieille ligne est partie
    assert "2 appareils actifs" in counts.summary(store.counts_since(yesterday), yesterday)
    store.close()


def _app(grid, client, store, secret=SECRET):
    s = Settings(admin_key=KEY, s3=None, apns_key="", measure_secret=secret)
    return create_app(
        settings=s, state=State(grid=grid, client=client, cache=client.cache, store=store)
    )


@pytest.mark.asyncio
async def test_requests_mark_devices_and_layers_without_keeping_identifiers(grid, client, store):
    app = _app(grid, client, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        app_h = {"X-Champis-User": USER}
        # l'app : /species au lancement compte l'appareil, les requêtes météo comptent la couche
        assert (await c.get("/species", headers=app_h)).status_code == 200
        cepes = species_groups()["cepes"]
        q = "&".join(f"species={s}" for s in cepes)
        bbox = "min_lon=6.2&min_lat=47.0&max_lon=6.5&max_lat=47.3"
        for _ in range(2):
            r = await c.get(f"/weather/bbox?{bbox}&{q}", headers=app_h)
            assert r.status_code == 200
        r = await c.get("/weather?cell=c_0930_6680&species=boletus_edulis", headers=app_h)
        assert r.status_code == 200
        # le site : Origin de champis.app, pas d'identifiant
        web_h = {"Origin": "https://champis.app"}
        r = await c.get(f"/weather/field?day={D.isoformat()}&{bbox}&{q}", headers=web_h)
        assert r.status_code in (200, 422), r.text  # 422 si `day` sort de la fenêtre du scénario
        assert (await c.get(f"/weather/bbox?{bbox}&{q}", headers=web_h)).status_code == 200
        # un autre appareil, puis le même deux fois : deux lignes et pas trois
        assert (await c.get("/species", headers={"X-Champis-User": OTHER})).status_code == 200
        assert (await c.get("/species", headers=app_h)).status_code == 200
        # curl sans Origin ni identifiant : rien
        assert (await c.get(f"/weather/bbox?{bbox}&{q}")).status_code == 200
        # un identifiant mal formé : ignoré, la requête passe
        assert (await c.get("/species", headers={"X-Champis-User": "nope"})).status_code == 200
    assert store.active_devices(D) == 2
    assert store.table_counts()["device_days"] == 2
    assert not store.has_legacy_device_rows(D)  # empreintes du jour, pas l'ancienne
    today = store.counts_since(D)[D]
    assert today["layer_app:cepes"] == 2 and today["layer_app:boletus_edulis"] == 1
    assert today["layer_web:cepes"] >= 1
    assert "layer_other:cepes" not in today and USER not in str(today)


@pytest.mark.asyncio
async def test_without_a_secret_devices_are_not_counted_but_layers_are(grid, client, store):
    app = _app(grid, client, store, secret="")
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        h = {"X-Champis-User": USER}
        assert (await c.get("/species", headers=h)).status_code == 200
        # les girolles servies : la girolle améthyste, sans planche, n'est pas dans le catalogue
        q = "&".join(f"species={s}" for s in species_groups(list_complete_species())["girolles"])
        assert (await c.get(f"/weather?cell=c_0930_6680&{q}", headers=h)).status_code == 200
    assert store.table_counts()["device_days"] == 0
    assert store.counts_since(D)[D] == {"layer_app:girolles": 1}


@pytest.mark.asyncio
async def test_usage_page_exposes_devices_and_layer_shares(grid, client, store):
    yesterday = D - timedelta(days=1)
    for k in range(3):
        store.mark_device_day(D, f"h{k}")
    store.mark_device_day(yesterday, "h0")
    store.mark_device_day(D - timedelta(days=20), "h9")
    store.bump(D, "layer_app:cepes", 6)
    store.bump(D, "layer_app:girolles", 2)
    store.bump(D - timedelta(days=9), "layer_web:cepes", 5)
    coin = {
        "id": "c1",
        "name": "Combe",
        "lat": 47.1,
        "lon": 6.3,
        "target": "cepes",
        "alt_class": 450,
        "t": {},
    }
    store.put_coins(USER, [coin], None, False, datetime.now(UTC))
    app = _app(grid, client, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        r = await c.get("/admin/api/usage?days=90", headers={"Authorization": f"Bearer {KEY}"})
        assert r.status_code == 200
        u = r.json()
    d = u["devices"]
    assert d["enabled"] and d["today"] == 3 and d["yesterday"] == 1
    # l'empreinte change chaque jour : plus de compte sur 7 jours, 30 jours ou 13 mois
    assert d["with_coins"] == 1 and not {"week", "month", "unique"} & set(d)
    assert d["daily"][D.isoformat()] == 3 and d["daily"][yesterday.isoformat()] == 1
    assert u["layers"]["7d"] == {"app": {"cepes": 6, "girolles": 2}}
    assert u["layers"]["30d"]["web"] == {"cepes": 5}
    assert "layer_app:cepes" in u["counters"]  # brut, la page les regroupe


@pytest.mark.asyncio
async def test_only_served_requests_on_limited_paths_are_measured(grid, client, store):
    """Une réponse en erreur ou un chemin hors limiteur (relais de tuiles) n'écrit rien : un
    identifiant au hasard par requête ne remplit plus `device_days`."""
    app = _app(grid, client, store)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        h = {"X-Champis-User": USER}
        assert (await c.get("/tiles/T-x.pmtiles", headers=h)).status_code == 404  # hors limiteur
        assert (await c.get("/nulle-part", headers=h)).status_code == 404
        assert (await c.get("/weather?cell=inconnue", headers=h)).status_code == 404
        r = await c.post("/observations", json={"observations": []}, headers=h)
        assert r.status_code == 422
        assert store.table_counts()["device_days"] == 0
        assert store.counts_since(D) == {}
        assert (await c.get("/species", headers=h)).status_code == 200
    assert store.table_counts()["device_days"] == 1


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_deploy_day_keeps_the_legacy_hash_and_the_next_day_switches(
    grid, client, store, monkeypatch
):
    """Les lignes d'avant gardent l'ancienne empreinte et expirent seules ; un jour ne mélange pas
    les deux : le jour du déploiement finit à l'ancienne, le lendemain passe à la nouvelle."""
    old = counts.legacy_device_hash(SECRET, USER)
    store.mark_device_day(D - timedelta(days=1), old)
    store.mark_device_day(D, old)  # vu ce matin, avant le déploiement
    app = _app(grid, client, store)
    async with app.router.lifespan_context(app), await _client(app) as c:
        assert (await c.get("/species", headers={"X-Champis-User": USER})).status_code == 200
        assert (await c.get("/species", headers={"X-Champis-User": OTHER})).status_code == 200
        assert store.active_devices(D) == 2  # le même appareil n'est pas compté deux fois
        tomorrow = D + timedelta(days=1)
        monkeypatch.setattr(counts, "today", lambda now=None: tomorrow)
        assert (await c.get("/species", headers={"X-Champis-User": USER})).status_code == 200
    assert store.active_devices(tomorrow) == 1
    assert not store.has_legacy_device_rows(tomorrow)
    assert store.has_legacy_device_rows(D) and store.active_devices(D - timedelta(days=1)) == 1


@pytest.mark.asyncio
async def test_erasure_also_removes_the_device_prints(grid, client, store):
    """`DELETE /observations` : les empreintes de l'appareil partent aussi, celle de chaque jour
    conservé et l'ancienne ; celles des autres restent, et l'effacement n'en réécrit pas une."""
    from conftest import NOW

    today = counts.today(NOW)  # l'horloge de l'app de test
    mine = [
        (today, counts.device_hash(SECRET, USER, today)),
        (today - timedelta(days=7), counts.device_hash(SECRET, USER, today - timedelta(days=7))),
        (
            today - timedelta(days=399),
            counts.device_hash(SECRET, USER, today - timedelta(days=399)),
        ),
        (today - timedelta(days=30), counts.legacy_device_hash(SECRET, USER)),
        (today - timedelta(days=31), counts.legacy_device_hash(SECRET, USER)),
    ]
    theirs = [
        (today, counts.device_hash(SECRET, OTHER, today)),
        (today - timedelta(days=30), counts.legacy_device_hash(SECRET, OTHER)),
    ]
    for day, h in mine + theirs:
        store.mark_device_day(day, h)
    app = _app(grid, client, store)
    async with app.router.lifespan_context(app), await _client(app) as c:
        r = await c.delete("/observations", headers={"X-Champis-User": USER})
        assert r.status_code == 200 and r.json() == {"deleted": 0}
    assert store.table_counts()["device_days"] == len(theirs)
    assert (
        store.active_devices(today) == 1 and store.active_devices(today - timedelta(days=30)) == 1
    )
    # sans secret, il n'y a pas d'empreinte à effacer ni à recalculer
    app = _app(grid, client, store, secret="")
    async with app.router.lifespan_context(app), await _client(app) as c:
        assert (
            await c.delete("/observations", headers={"X-Champis-User": USER})
        ).status_code == 200
    assert store.table_counts()["device_days"] == len(theirs)
