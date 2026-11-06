"""« Mes coins » : scores côté backend, notification récapitulative unique, APNs, jetons invalides."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

import httpx
import jwt
import pytest
from champis_api.apns import ApnsClient
from champis_api.coins import compose, day_words, good_threshold, notify_all, score_coins
from champis_api.store import Coin, Store
from champis_api.warm import warm
from conftest import TODAY
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

USER = str(uuid.uuid4())
H = {"X-Champis-User": USER}
TOKEN = "ab" * 16


def _coin(**over):
    base = {
        "id": str(uuid.uuid4()),
        "name": "Combe",
        "lat": 47.12,
        "lon": 6.31,
        "target": "cepes",
        "alt_class": 450,
        "t": {"boletus_edulis": 0.9, "boletus_aereus": 0.3},
    }
    return base | over


def _row(v, days=11):
    """Réponse factice de cell_weather : M_eff × Φ constant = v sur toutes les classes."""
    rows = []
    for k in range(-3, 8):
        d = (TODAY + timedelta(days=k)).isoformat()
        rows.append(
            {
                "date": d,
                "by_alt": {"450": {"M_eff": v, "phi": 1.0}, "1350": {"M_eff": v / 2, "phi": 1.0}},
            }
        )
    return rows


class FakeApns:
    """Enregistre les envois ; un jeton particulier est « désinscrit »."""

    def __init__(self):
        self.sent = []

    async def send(self, token, title, body, sandbox=False, extra=None, **_):
        from champis_api.apns import ApnsResult

        self.sent.append((token, title, body, sandbox))
        self.extras = [*getattr(self, "extras", []), extra]
        if token == "dead" * 8:
            return ApnsResult(False, 410, "Unregistered")
        return ApnsResult(True, 200)


def test_score_coins_uses_pixel_t_and_cell_weather(grid):
    coins = [
        Coin(
            "1",
            USER,
            "Combe",
            47.12,
            6.31,
            "cepes",
            450,
            {"boletus_edulis": 0.9, "boletus_aereus": 0.3},
        ),
        Coin("2", USER, "Crête", 47.12, 6.31, "cepes", 1350, {"boletus_edulis": 0.9}),
        Coin(
            "3", USER, "Loin", 47.12, 6.38, "cepes", 150, {"boletus_edulis": 0.9}
        ),  # cellule sans météo
    ]
    weather = {
        "c_0930_6680": {"species": {"boletus_edulis": _row(0.8), "boletus_aereus": _row(0.8)}}
    }
    scores = score_coins(coins, grid, weather, TODAY)
    assert scores[0].today == pytest.approx(0.9 * 0.8) and scores[0].leading == "boletus_edulis"
    assert scores[1].today == pytest.approx(0.9 * 0.4)  # classe 1350 : M_eff moitié
    assert scores[2].today is None and scores[2].leading is None
    assert scores[0].best_day is None  # météo constante : pas de meilleur jour à venir
    # les onze jours J−3 → J+7 (lot 1) : toujours onze entrées, valeur None sans météo
    assert [d.date for d in scores[0].days] == [TODAY + timedelta(days=k) for k in range(-3, 8)]
    assert all(
        d.value == pytest.approx(0.72) and d.leading == "boletus_edulis" for d in scores[0].days
    )
    assert len(scores[2].days) == 11 and all(d.value is None for d in scores[2].days)


def test_score_coins_reports_a_better_coming_day(grid):
    coin = Coin("1", USER, "Combe", 47.12, 6.31, "cepes", 450, {"boletus_edulis": 1.0})
    rows = _row(0.3)
    peak = (TODAY + timedelta(days=3)).isoformat()
    for r in rows:
        if r["date"] == peak:
            r["by_alt"]["450"]["M_eff"] = 0.9
    s = score_coins([coin], grid, {"c_0930_6680": {"species": {"boletus_edulis": rows}}}, TODAY)[0]
    assert (
        s.today == pytest.approx(0.3)
        and s.best_day == TODAY + timedelta(days=3)
        and s.best == pytest.approx(0.9)
    )


def test_compose_only_when_a_coin_is_good():
    c1 = Coin("1", USER, "Combe", 0, 0, "cepes", 450, {"boletus_edulis": 1})
    c2 = Coin("2", USER, "Tourbière", 0, 0, "cepes", 450, {"boletus_edulis": 1})
    from champis_api.coins import CoinScore

    assert good_threshold() == 0.55  # lu dans la palette, pas écrit ici
    quiet = [
        CoinScore(c1, 0.4, None, None, "boletus_edulis"),
        CoinScore(c2, None, None, None, None),
    ]
    assert compose(quiet, TODAY) is None
    ready = [
        CoinScore(c1, 0.82, TODAY + timedelta(days=2), 0.9, "boletus_edulis"),
        CoinScore(c2, 0.4, None, None, "boletus_edulis"),
    ]
    title, body, coin_id = compose(ready, TODAY)
    assert coin_id == "1"  # le coin le mieux noté : l'app ouvre sa fiche
    # en mots, sans score ni point décimal (spec › Produit › étape A, 6 nov. 2026)
    assert title == "Champis — Combe : excellent aujourd'hui"
    peak = TODAY + timedelta(days=2)
    assert body == (
        "Combe : excellent (cèpe de Bordeaux). Tourbière reste sous « bon ». "
        f"Meilleur jour à venir : {day_words(peak)} pour Combe."
    )
    assert not any(ch.isdigit() for ch in body.replace(str(peak.day), ""))


def test_compose_names_several_coins_and_their_labels():
    from champis_api.coins import CoinScore

    c1 = Coin("1", USER, "Combe", 0, 0, "cepes", 450, {"boletus_edulis": 1})
    c2 = Coin("2", USER, "Hêtraie", 0, 0, "girolles", 450, {"cantharellus_cibarius": 1})
    c3 = Coin("3", USER, "Sapinière", 0, 0, "cepes", 450, {"boletus_edulis": 1})
    c4 = Coin("4", USER, "Lisière", 0, 0, "cepes", 450, {"boletus_edulis": 1})
    scores = [
        CoinScore(c2, 0.6, None, None, "cantharellus_cibarius"),
        CoinScore(c1, 0.8, None, None, "boletus_edulis"),
        CoinScore(c3, 0.2, None, None, "boletus_edulis"),
        CoinScore(c4, 0.1, None, None, "boletus_edulis"),
    ]
    title, body, coin_id = compose(scores, TODAY)
    assert coin_id == "1"
    assert title == "Champis — 2 coins excellents ou bons aujourd'hui"
    assert body == (
        "Combe : excellent (cèpe de Bordeaux). Hêtraie : bon (girolle). "
        "Les 2 autres restent sous « bon »."
    )
    same = [CoinScore(c1, 0.6, None, None, None), CoinScore(c3, 0.7, None, None, None)]
    title, body, _ = compose(same, TODAY)
    assert title == "Champis — 2 coins bons aujourd'hui"
    assert body == "Sapinière : bon. Combe : bon."


def test_day_words_is_a_weekday_and_its_number():
    assert day_words(date(2026, 11, 7)) == "samedi 7"
    assert day_words(date(2026, 11, 1)) == "dimanche 1"


@pytest.mark.asyncio
async def test_put_and_get_coins(api, store: Store):
    coin = _coin()
    r = await api.put("/coins", json={"apns_token": TOKEN, "coins": [coin]}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json() == {"coins": 1, "notifications": True}
    # remplacement complet : la seconde liste efface la première
    coin2 = _coin(name="Sapinière")
    await api.put("/coins", json={"apns_token": TOKEN, "coins": [coin2]}, headers=H)
    r = await api.get("/coins", headers=H)
    body = r.json()
    assert [c["name"] for c in body["coins"]] == ["Sapinière"]
    assert body["today"] == TODAY.isoformat()
    assert 0 <= body["coins"][0]["score"] <= 1 and body["coins"][0]["leading"] in coin2["t"]
    days = body["coins"][0]["days"]  # lot 1 : onze jours J−3 → J+7, le jour J égal au score
    assert [d["date"] for d in days] == [
        (TODAY + timedelta(days=k)).isoformat() for k in range(-3, 8)
    ]
    today_row = next(d for d in days if d["date"] == TODAY.isoformat())
    assert today_row["value"] == body["coins"][0]["score"]
    assert today_row["leading"] == body["coins"][0]["leading"]
    assert set(days[0]) == {"date", "value", "leading"}
    # étape B2 : les phrases du jour de l'espèce porteuse, à la classe du coin, pour sa fiche
    phrases = body["coins"][0]["summary"]
    assert phrases and all(isinstance(p, str) for p in phrases)
    # validation : cible inconnue, espèce inconnue dans t, T hors [0, 1], trop de coins
    bad = _coin(target="morilles")
    assert (await api.put("/coins", json={"coins": [bad]}, headers=H)).status_code == 422
    bad = _coin(t={"amanita": 0.5})
    assert (await api.put("/coins", json={"coins": [bad]}, headers=H)).status_code == 422
    bad = _coin(t={"boletus_edulis": 1.5})
    assert (await api.put("/coins", json={"coins": [bad]}, headers=H)).status_code == 422
    many = {"coins": [_coin() for _ in range(51)]}
    assert (await api.put("/coins", json=many, headers=H)).status_code == 422
    assert (await api.put("/coins", json={"coins": []})).status_code == 401


@pytest.mark.asyncio
async def test_notify_all_sends_one_recap_per_device(grid, client, store: Store):
    """Après la pré-chauffe : un appareil avec un coin bon reçoit une notification, un appareil
    dont les coins sont faibles n'en reçoit pas, un jeton désinscrit est retiré."""
    await warm(grid, client)
    now = client.now()
    good_user, quiet_user, dead_user = (str(uuid.uuid4()) for _ in range(3))
    good = [
        _coin(name="Combe", t={"boletus_edulis": 1.0}),
        _coin(name="Crête", alt_class=1350, t={"boletus_edulis": 1.0}),
    ]
    store.put_coins(good_user, good, "aa" * 16, False, now)
    store.put_coins(
        quiet_user, [_coin(name="Friche", t={"boletus_edulis": 0.05})], "bb" * 16, True, now
    )
    store.put_coins(
        dead_user, [_coin(name="Combe", t={"boletus_edulis": 1.0})], "dead" * 8, False, now
    )
    # le scénario météo de référence donne-t-il un M_eff × Φ ≥ bon pour un T = 1 ? on le vérifie via l'API interne
    from champis_api.coins import weather_for

    w = await weather_for(store.coins_of(good_user), grid, client, client.today())
    row = next(
        r for r in w["c_0930_6680"]["species"]["boletus_edulis"] if r["date"] == TODAY.isoformat()
    )
    factor = row["by_alt"]["450"]["M_eff"] * row["by_alt"]["450"]["phi"]
    assert factor >= good_threshold(), "le scénario de test doit être favorable au cèpe"

    apns = FakeApns()
    report = await notify_all(store, grid, client, apns)
    assert report.devices == 3 and report.sent == 1 and report.skipped == 1
    assert report.failed == 1 and report.tokens_cleared == 1
    assert len(apns.sent) == 2  # une seule notification par appareil, aucune pour l'appareil calme
    token, title, body, sandbox = apns.sent[0]
    assert (
        token == "aa" * 16 and sandbox is False and "Combe" in body and title.startswith("Champis")
    )
    assert apns.extras[0] == {"coin_id": good[0]["id"]}  # charge utile : le coin à ouvrir
    # le jeton désinscrit ne sera plus sollicité
    assert {d.user_id for d, _ in store.devices_with_coins()} == {good_user, quiet_user}


@pytest.mark.asyncio
async def test_apns_client_signs_with_es256_and_talks_http2_payload():
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/dead"):
            return httpx.Response(410, json={"reason": "Unregistered"})
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        clock = [1_700_000_000.0]
        apns = ApnsClient(
            http, pem, "KEYID1234", "TEAM567890", "fr.adhumi.champis", now=lambda: clock[0]
        )
        res = await apns.send("abc123", "Titre", "Corps", sandbox=False)
        assert res.ok and res.status == 200
        req = seen[0]
        assert req.url.host == "api.push.apple.com" and req.url.path == "/3/device/abc123"
        assert req.headers["apns-topic"] == "fr.adhumi.champis"
        assert req.headers["apns-push-type"] == "alert" and req.headers["apns-collapse-id"]
        token = req.headers["authorization"].split(" ", 1)[1]
        claims = jwt.decode(token, public, algorithms=["ES256"])
        assert claims == {"iss": "TEAM567890", "iat": 1_700_000_000}
        assert jwt.get_unverified_header(token)["kid"] == "KEYID1234"
        assert json.loads(req.content)["aps"]["alert"] == {"title": "Titre", "body": "Corps"}
        # le JWT est réutilisé 50 min puis régénéré
        clock[0] += 10 * 60
        await apns.send("abc123", "t", "b")
        assert seen[1].headers["authorization"] == req.headers["authorization"]
        clock[0] += 50 * 60
        await apns.send("abc123", "t", "b")
        assert seen[2].headers["authorization"] != req.headers["authorization"]
        # sandbox et jeton désinscrit
        res = await apns.send("dead", "t", "b", sandbox=True)
        assert seen[3].url.host == "api.sandbox.push.apple.com"
        assert not res.ok and res.token_invalid
