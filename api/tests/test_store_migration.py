"""Clé primaire `(user_id, id)` des observations et des coins (14 nov. 2026) : un appareil ne
bloque plus l'identifiant d'un autre, un doublon dans un lot ne donne plus un 500, et une base à
l'ancien schéma (clé `id` seul) est migrée sans perte, une seule fois.

La migration Postgres est celle de la production : son test tourne quand
`CHAMPIS_TEST_POSTGRES_URL` désigne une base jetable (sinon il est sauté ; SQLite seul en CI)."""

from __future__ import annotations

import os
import threading
import uuid
from datetime import UTC, date, datetime

import pytest
from champis_api import store as store_module
from champis_api.store import Store
from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    SmallInteger,
    String,
    Text,
    create_engine,
    insert,
    inspect,
    text,
)
from sqlalchemy import Table as SATable

NOW = datetime(2026, 11, 14, 9, 0, tzinfo=UTC)
ALICE, BOB = str(uuid.uuid4()), str(uuid.uuid4())
H_ALICE, H_BOB = {"X-Champis-User": ALICE}, {"X-Champis-User": BOB}


def _obs(oid: str, found: bool = True) -> dict:
    return {
        "id": oid,
        "species": "boletus_edulis",
        "lat": 47.12,
        "lon": 6.31,
        "accuracy_m": 8.0,
        "date": date(2026, 10, 17),
        "found": found,
        "quantity": 2 if found else None,
    }


def _coin(cid: str, name: str = "Combe") -> dict:
    return {
        "id": cid,
        "name": name,
        "lat": 47.12,
        "lon": 6.31,
        "target": "cepes",
        "alt_class": 450,
        "t": {"boletus_edulis": 0.9},
    }


def _old_schema(url: str) -> tuple[str, str]:
    """Crée les tables telles qu'elles étaient avant la migration (clé `id` seul, index sur
    `user_id`) et y range une observation et un coin par appareil."""
    old = MetaData()
    observations = SATable(
        "observations",
        old,
        Column("id", String(36), primary_key=True),
        Column("user_id", String(36), nullable=False, index=True),
        Column("species", String(64), nullable=False),
        Column("lat", Float, nullable=False),
        Column("lon", Float, nullable=False),
        Column("accuracy_m", Float),
        Column("date", Date, nullable=False),
        Column("found", Boolean, nullable=False),
        Column("quantity", SmallInteger),
        Column("shared", Boolean, nullable=False, default=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    coins = SATable(
        "coins",
        old,
        Column("id", String(36), primary_key=True),
        Column("user_id", String(36), nullable=False, index=True),
        Column("name", String(80), nullable=False),
        Column("lat", Float, nullable=False),
        Column("lon", Float, nullable=False),
        Column("target", String(64), nullable=False),
        Column("alt_class", Integer, nullable=False),
        Column("t_json", Text, nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    engine = create_engine(url)
    old.drop_all(engine)
    old.create_all(engine)
    oid, cid = str(uuid.uuid4()), str(uuid.uuid4())
    with engine.begin() as conn:
        for k, user in enumerate((ALICE, BOB)):
            conn.execute(
                insert(observations).values(
                    **_obs(oid if k == 0 else str(uuid.uuid4())),
                    user_id=user,
                    shared=False,
                    created_at=NOW,
                )
            )
            conn.execute(
                insert(coins).values(
                    id=cid if k == 0 else str(uuid.uuid4()),
                    user_id=user,
                    name=f"Coin {k}",
                    lat=47.1,
                    lon=6.3,
                    target="cepes",
                    alt_class=450,
                    t_json='{"boletus_edulis": 0.9}',
                    created_at=NOW,
                )
            )
    assert inspect(engine).get_pk_constraint("observations")["constrained_columns"] == ["id"]
    engine.dispose()
    return oid, cid


def _primary_keys(store: Store) -> dict[str, list[str]]:
    insp = inspect(store.engine)
    return {t: insp.get_pk_constraint(t)["constrained_columns"] for t in ("observations", "coins")}


def _check_migrated(url: str, oid: str, cid: str) -> None:
    store = Store(url)
    try:
        assert store.owner_keys
        assert _primary_keys(store) == {
            "observations": ["user_id", "id"],
            "coins": ["user_id", "id"],
        }
        # rien n'est perdu
        assert [o["id"] for o in store.observations_of(ALICE)] == [oid]
        assert len(store.observations_of(BOB)) == 1
        assert [c.id for c in store.coins_of(ALICE)] == [cid]
        assert [c.name for c in store.coins_of(BOB)] == ["Coin 1"]
        names = set(inspect(store.engine).get_table_names())
        assert not any(n.endswith("_avant_cle") for n in names)
        indexed = {
            tuple(i["column_names"]) for i in inspect(store.engine).get_indexes("observations")
        }
        assert ("user_id",) in indexed
        # l'identifiant d'Alice, envoyé par Bob, est maintenant le sien aussi
        assert store.add_observations(BOB, [_obs(oid)], NOW) == {"inserted": 1, "duplicates": 0}
        assert store.add_observations(ALICE, [_obs(oid)], NOW) == {"inserted": 0, "duplicates": 1}
        store.put_coins(BOB, [_coin(cid, "Le sien")], None, False, NOW)
        assert [c.name for c in store.coins_of(ALICE)] == ["Coin 0"]
    finally:
        store.close()
    # une seconde ouverture ne refait rien
    again = Store(url)
    try:
        assert again.owner_keys and len(again.observations_of(BOB)) == 2
    finally:
        again.close()


def test_sqlite_base_with_the_old_key_is_migrated_without_loss(tmp_path, caplog):
    url = f"sqlite:///{tmp_path / 'ancienne.db'}"
    oid, cid = _old_schema(url)
    with caplog.at_level("INFO", logger="champis_api.store"):
        _check_migrated(url, oid, cid)
    posed = [r.getMessage() for r in caplog.records if "clé primaire" in r.getMessage()]
    assert len(posed) == 2 and "(2 lignes)" in posed[0]  # une fois par table, pas à la réouverture


def test_a_failed_migration_keeps_the_old_key_and_the_service(tmp_path, monkeypatch, caplog):
    """La migration échoue (ici : en plein milieu) : rien n'est à moitié fait, la base s'ouvre
    avec l'ancienne clé, les données sont là, et l'essai suivant aboutit."""
    url = f"sqlite:///{tmp_path / 'ancienne.db'}"
    oid, _cid = _old_schema(url)
    create = store_module.Table.create

    def boom(self, *args, **kwargs):
        raise RuntimeError("panne pendant la migration")

    monkeypatch.setattr(store_module.Table, "create", boom)
    store = Store(url)
    assert not store.owner_keys and "nouvel essai au prochain démarrage" in caplog.text
    assert _primary_keys(store) == {"observations": ["id"], "coins": ["id"]}
    assert [o["id"] for o in store.observations_of(ALICE)] == [oid]
    assert store.add_observations(ALICE, [_obs(str(uuid.uuid4()))], NOW)["inserted"] == 1
    store.close()
    monkeypatch.setattr(store_module.Table, "create", create)
    again = Store(url)
    assert again.owner_keys and len(again.observations_of(ALICE)) == 2
    again.close()


def test_new_base_has_the_owner_key():
    store = Store("sqlite://")
    assert store.owner_keys
    assert _primary_keys(store) == {"observations": ["user_id", "id"], "coins": ["user_id", "id"]}
    store.close()


POSTGRES = os.environ.get("CHAMPIS_TEST_POSTGRES_URL", "")


@pytest.mark.skipif(not POSTGRES, reason="CHAMPIS_TEST_POSTGRES_URL : base Postgres jetable")
def test_postgres_base_with_the_old_key_is_migrated_without_loss():
    oid, cid = _old_schema(POSTGRES)
    # API, worker et crons ouvrent la base en même temps : quatre ouvertures concurrentes
    errors: list[BaseException] = []

    def open_store():
        try:
            Store(POSTGRES).close()
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=open_store) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    _check_migrated(POSTGRES, oid, cid)
    engine = create_engine(POSTGRES)
    with engine.begin() as conn:
        names = {r[0] for r in conn.execute(text("SELECT conname FROM pg_constraint"))}
    engine.dispose()
    assert {"observations_pkey", "coins_pkey"} <= names


# ------------------------------------------------------------------ par l'API


@pytest.mark.asyncio
async def test_a_device_cannot_block_the_identifier_of_another(api, store: Store):
    oid, cid = str(uuid.uuid4()), str(uuid.uuid4())
    o = _obs(oid) | {"date": "2026-10-17"}
    r = await api.post("/observations", json={"observations": [o]}, headers=H_BOB)
    assert r.json() == {"inserted": 1, "duplicates": 0}
    r = await api.post("/observations", json={"observations": [o]}, headers=H_ALICE)
    assert r.status_code == 201 and r.json() == {"inserted": 1, "duplicates": 0}
    assert (await api.get("/observations", headers=H_ALICE)).json()["count"] == 1
    for h in (H_BOB, H_ALICE):
        r = await api.put("/coins", json={"coins": [_coin(cid)]}, headers=h)
        assert r.status_code == 200, r.text
    assert len(store.coins_of(ALICE)) == len(store.coins_of(BOB)) == 1


@pytest.mark.asyncio
async def test_duplicates_inside_a_batch_never_give_a_500(api, store: Store):
    oid, cid = str(uuid.uuid4()), str(uuid.uuid4())
    o = _obs(oid) | {"date": "2026-10-17"}
    other = _obs(str(uuid.uuid4()), found=False) | {"date": "2026-10-17"}
    r = await api.post("/observations", json={"observations": [o, other, o]}, headers=H_ALICE)
    assert r.status_code == 201 and r.json() == {"inserted": 2, "duplicates": 1}
    r = await api.put(
        "/coins", json={"coins": [_coin(cid, "Avant"), _coin(cid, "Après")]}, headers=H_ALICE
    )
    assert r.status_code == 200 and r.json()["coins"] == 1
    assert [c.name for c in store.coins_of(ALICE)] == ["Après"]
