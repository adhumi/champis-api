"""Observations terrain, favoris (« mes coins ») et jetons APNs : Postgres sur Railway.

Spec › Backend (30 oct. 2026) : Postgres Railway pour les observations, même base et même
identité pour les coins ; identité = identifiant généré par l'app, envoyé en en-tête. Pas de
photo, pas de partage entre utilisateurs (`shared` réservé, toujours faux). Accès par SQLAlchemy
Core : la même table se crée dans Postgres (`DATABASE_URL` de Railway) et dans SQLite (tests,
développement sans Postgres), le code ne porte aucun SQL spécifique à un moteur.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

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
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

metadata = MetaData()

observations = Table(
    "observations",
    metadata,
    Column("id", String(36), primary_key=True),  # UUID généré par l'app : idempotence
    Column("user_id", String(36), nullable=False, index=True),
    Column("species", String(64), nullable=False),
    Column("lat", Float, nullable=False),
    Column("lon", Float, nullable=False),
    Column("accuracy_m", Float),
    Column("date", Date, nullable=False),
    Column("found", Boolean, nullable=False),
    Column("quantity", SmallInteger),  # 1–3, seulement si trouvé
    Column("shared", Boolean, nullable=False, default=False),  # réservé, inactif
    Column("created_at", DateTime(timezone=True), nullable=False),
)

devices = Table(
    "devices",
    metadata,
    Column("user_id", String(36), primary_key=True),
    Column("apns_token", String(200)),  # None = pas de notification (refusée ou jeton invalide)
    Column("sandbox", Boolean, nullable=False, default=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

coins = Table(
    "coins",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("user_id", String(36), nullable=False, index=True),
    Column("name", String(80), nullable=False),
    Column("lat", Float, nullable=False),
    Column("lon", Float, nullable=False),
    Column("target", String(64), nullable=False),  # espèce ou groupe affiché
    Column("alt_class", Integer, nullable=False),  # centre de la classe d'altitude du pixel (m)
    Column("t_json", Text, nullable=False),  # {espèce: T du pixel} lu dans les tuiles par l'app
    Column("created_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class Coin:
    id: str
    user_id: str
    name: str
    lat: float
    lon: float
    target: str
    alt_class: int
    t: dict[str, float]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "target": self.target,
            "alt_class": self.alt_class,
            "t": self.t,
        }


@dataclass(frozen=True)
class Device:
    user_id: str
    apns_token: str | None
    sandbox: bool


class Store:
    """Une instance par service ; les méthodes sont synchrones (FastAPI les exécute hors de la
    boucle d'événements quand les endpoints sont des `def`)."""

    def __init__(self, url: str):
        kwargs: dict[str, Any] = {"future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool
        self.engine: Engine = create_engine(url, **kwargs)
        metadata.create_all(self.engine)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    # ------------------------------------------------------------------ observations

    def add_observations(self, user_id: str, rows: list[dict[str, Any]], now: datetime) -> dict:
        """Insère les observations absentes (par `id`) ; les déjà connues sont ignorées, pas
        écrasées : un renvoi après coupure réseau est sans effet."""
        if not rows:
            return {"inserted": 0, "duplicates": 0}
        ids = [r["id"] for r in rows]
        with self.engine.begin() as conn:
            known = {
                r[0]
                for r in conn.execute(select(observations.c.id).where(observations.c.id.in_(ids)))
            }
            fresh = [
                {
                    "id": r["id"],
                    "user_id": user_id,
                    "species": r["species"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "accuracy_m": r.get("accuracy_m"),
                    "date": r["date"],
                    "found": r["found"],
                    "quantity": r.get("quantity"),
                    "shared": False,
                    "created_at": now.astimezone(UTC),
                }
                for r in rows
                if r["id"] not in known
            ]
            if fresh:
                conn.execute(insert(observations), fresh)
        return {"inserted": len(fresh), "duplicates": len(known)}

    def observations_of(self, user_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            res = conn.execute(
                select(observations)
                .where(observations.c.user_id == user_id)
                .order_by(observations.c.date.desc(), observations.c.created_at.desc())
            )
            return [
                {
                    "id": r.id,
                    "species": r.species,
                    "lat": r.lat,
                    "lon": r.lon,
                    "accuracy_m": r.accuracy_m,
                    "date": r.date.isoformat() if isinstance(r.date, date) else r.date,
                    "found": bool(r.found),
                    "quantity": r.quantity,
                }
                for r in res
            ]

    def delete_observations(self, user_id: str) -> int:
        """Droit à l'effacement : toutes les lignes de l'identifiant, observations, coins et jeton."""
        with self.engine.begin() as conn:
            n = conn.execute(delete(observations).where(observations.c.user_id == user_id)).rowcount
            conn.execute(delete(coins).where(coins.c.user_id == user_id))
            conn.execute(delete(devices).where(devices.c.user_id == user_id))
        return n

    # ------------------------------------------------------------------ coins et appareils

    def put_coins(
        self,
        user_id: str,
        rows: list[dict[str, Any]],
        apns_token: str | None,
        sandbox: bool,
        now: datetime,
    ) -> int:
        """Remplace la liste complète des coins de l'appareil (pas de fusion) et son jeton APNs."""
        ts = now.astimezone(UTC)
        with self.engine.begin() as conn:
            conn.execute(delete(coins).where(coins.c.user_id == user_id))
            if rows:
                conn.execute(
                    insert(coins),
                    [
                        {
                            "id": r["id"],
                            "user_id": user_id,
                            "name": r["name"],
                            "lat": r["lat"],
                            "lon": r["lon"],
                            "target": r["target"],
                            "alt_class": r["alt_class"],
                            "t_json": json.dumps(r["t"]),
                            "created_at": ts,
                        }
                        for r in rows
                    ],
                )
            existing = conn.execute(
                select(devices.c.user_id).where(devices.c.user_id == user_id)
            ).first()
            values = {"apns_token": apns_token, "sandbox": sandbox, "updated_at": ts}
            if existing:
                conn.execute(update(devices).where(devices.c.user_id == user_id).values(**values))
            else:
                conn.execute(insert(devices).values(user_id=user_id, **values))
        return len(rows)

    def coins_of(self, user_id: str) -> list[Coin]:
        with self.engine.connect() as conn:
            res = conn.execute(
                select(coins).where(coins.c.user_id == user_id).order_by(coins.c.name)
            )
            return [self._coin(r) for r in res]

    def devices_with_coins(self) -> list[tuple[Device, list[Coin]]]:
        """Appareils notifiables (jeton présent) avec leurs coins."""
        with self.engine.connect() as conn:
            devs = conn.execute(select(devices).where(devices.c.apns_token.is_not(None))).all()
            out = []
            for d in devs:
                rows = conn.execute(select(coins).where(coins.c.user_id == d.user_id)).all()
                if rows:
                    out.append(
                        (
                            Device(d.user_id, d.apns_token, bool(d.sandbox)),
                            [self._coin(r) for r in rows],
                        )
                    )
            return out

    def clear_token(self, user_id: str) -> None:
        """Jeton refusé par APNs (appareil désinscrit) : on n'insiste pas."""
        with self.engine.begin() as conn:
            conn.execute(
                update(devices).where(devices.c.user_id == user_id).values(apns_token=None)
            )

    @staticmethod
    def _coin(r) -> Coin:
        return Coin(
            id=r.id,
            user_id=r.user_id,
            name=r.name,
            lat=r.lat,
            lon=r.lon,
            target=r.target,
            alt_class=int(r.alt_class),
            t={k: float(v) for k, v in json.loads(r.t_json).items()},
        )

    def close(self) -> None:
        self.engine.dispose()
