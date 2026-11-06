"""Mesure minimale (spec › Produit › Mesure minimale ; étape C, lot C2, 6 nov. 2026) : des totaux
par jour, sans adresse IP ni identifiant, gardés 13 mois, lus par `champis-api stats`."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from champis_api import counts
from champis_api.app import State, create_app
from champis_api.cli import main
from champis_api.settings import Settings
from champis_api.store import Store

D = date(2026, 11, 6)


def test_bump_adds_and_set_count_replaces():
    store = Store("sqlite://")
    store.bump(D, "web_map_opens")
    store.bump(D, "web_map_opens", 2)
    store.bump(D, "web_map_opens", 0)  # rien à ajouter : aucune ligne écrite pour rien
    store.set_count(D, "coins", 5)
    store.set_count(D, "coins", 7)
    store.bump(D - timedelta(days=1), "web_map_opens")
    assert store.counts_since(D) == {D: {"web_map_opens": 3, "coins": 7}}
    assert store.counts_since(D - timedelta(days=1))[D - timedelta(days=1)] == {"web_map_opens": 1}


def test_counts_are_kept_thirteen_months():
    store = Store("sqlite://")
    old = D - timedelta(days=counts.KEEP_DAYS + 1)
    store.bump(old, "web_map_opens")
    store.bump(D, "web_map_opens")
    assert store.purge_counts(D - timedelta(days=counts.KEEP_DAYS)) == 1
    assert list(store.counts_since(date(2000, 1, 1))) == [D]


def test_observations_are_counted_once_on_arrival():
    """Les observations nouvelles seulement, trouvé et rien trouvé à part ; un renvoi après une
    coupure réseau ne compte pas deux fois."""
    store = Store("sqlite://")
    now = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
    user = str(uuid.uuid4())

    def obs(found: bool) -> dict:
        return {
            "id": str(uuid.uuid4()),
            "species": "boletus_edulis",
            "lat": 47.1,
            "lon": 6.3,
            "date": D,  # la route passe une date validée
            "found": found,
            "quantity": 2 if found else None,
        }

    batch = [obs(True), obs(False), obs(False)]
    store.add_observations(user, batch, now)
    store.add_observations(user, batch, now)  # même lot renvoyé
    assert store.counts_since(D)[D] == {"observations_found": 1, "observations_not_found": 2}
    # l'effacement des observations d'un appareil ne touche pas aux totaux
    store.delete_observations(user)
    assert store.counts_since(D)[D]["observations_not_found"] == 2


def test_coin_totals_and_notify_record():
    store = Store("sqlite://")
    now = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)
    coin = {
        "name": "Combe",
        "lat": 47.1,
        "lon": 6.3,
        "target": "cepes",
        "alt_class": 450,
        "t": {"boletus_edulis": 0.9},
    }
    store.put_coins(
        "a", [coin | {"id": str(uuid.uuid4())}, coin | {"id": str(uuid.uuid4())}], None, False, now
    )
    store.put_coins("b", [coin | {"id": str(uuid.uuid4())}], "aa" * 16, False, now)
    store.put_coins("c", [], None, False, now)  # appareil sans coin
    assert store.coin_totals() == (2, 3)
    counts.record_notify(store, sent=1, failed=0, day=D)
    counts.record_notify(store, sent=2, failed=1, day=D)  # deux passages le même jour : s'ajoutent
    assert store.counts_since(D)[D] == {
        "notify_sent": 3,
        "notify_failed": 1,
        "devices_with_coins": 2,
        "coins": 3,
    }


def test_rows_and_summary_in_words():
    data = {
        D: {
            "web_map_opens": 12,
            "observations_found": 1,
            "observations_not_found": 2,
            "cells_without_weather": 0,
        },
    }
    rows = counts.rows(data, [D - timedelta(days=1), D])
    assert rows[0][0] == "2026-11-06" and rows[0][1] == "12"
    assert rows[1][1:] == ["·"] * len(counts.COUNTERS)  # rien de relevé : jamais un zéro inventé
    assert counts.header()[0] == "jour" and len(counts.header()) == len(rows[0])
    assert counts.summary(data, D) == (
        "2026-11-06 : 12 ouvertures de la carte, 3 observations, 0 cellules sans météo"
    )
    assert counts.summary({}, D) == "rien de relevé le 2026-11-06"


def test_a_failing_store_never_breaks_what_it_counts():
    class Broken:
        def bump(self, *a, **k):
            raise RuntimeError("base injoignable")

        coin_totals = set_count = purge_counts = bump

    counts.bump_quietly(Broken(), "web_map_opens")
    counts.record_notify(Broken(), 1, 0)
    counts.record_warm(Broken(), 10, 1)


@pytest.mark.asyncio
async def test_the_api_counts_map_opens_and_nothing_else(grid, client):
    """Une ouverture de `/web/carte.html` (200 ou 304) de plus ; ni l'accueil, ni l'API, ni une
    page absente. Aucune autre donnée de la requête n'est gardée."""
    store = Store("sqlite://")
    app = create_app(state=State(grid=grid, client=client, cache=client.cache, store=store))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c,
    ):
        r = await c.get("/web/carte.html")
        assert r.status_code == 200
        etag = r.headers.get("etag")
        if etag:
            again = await c.get("/web/carte.html", headers={"If-None-Match": etag})
            assert again.status_code in (200, 304)
        await c.get("/web/index.html")
        await c.get("/web/absente.html")
        await c.get("/health")
    expected = 2 if etag else 1
    day = counts.today()
    assert store.counts_since(day)[day] == {"web_map_opens": expected}
    assert set(store.counts_since(date(2000, 1, 1))) == {day}


def test_stats_prints_one_line_per_day(tmp_path, monkeypatch, capsys):
    db = f"sqlite:///{tmp_path / 'counts.db'}"
    store = Store(db)
    store.bump(counts.today(), "web_map_opens", 4)
    store.close()
    # Settings lit l'environnement à l'import : on lui donne directement la base du test
    settings = dataclasses.replace(Settings(), database_url=db)
    monkeypatch.setattr("champis_api.cli.Settings", lambda: settings)
    monkeypatch.setenv("CHAMPIS_LOG_DIR", str(tmp_path))
    assert main(["-q", "stats", "--days", "3"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[:2] == ["jour", "carte"]
    assert len(out) == 2 + 3  # en-tête, tirets, trois jours
    assert out[2].split()[:2] == [counts.today().isoformat(), "4"]
