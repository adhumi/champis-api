"""Observations terrain, favoris (« mes coins ») et jetons APNs : Postgres sur Railway.

Spec › Backend (30 oct. 2026) : Postgres Railway pour les observations, même base et même
identité pour les coins ; identité = identifiant généré par l'app, envoyé en en-tête. Pas de
photo, pas de partage entre utilisateurs (`shared` réservé, toujours faux). Accès par SQLAlchemy
Core : la même table se crée dans Postgres (`DATABASE_URL` de Railway) et dans SQLite (tests,
développement sans Postgres), le code ne porte aucun SQL spécifique à un moteur.
"""

from __future__ import annotations

import json
import uuid
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
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

metadata = MetaData()


def _iso(dt: datetime | None) -> str | None:
    """Horodatage ISO en UTC ; SQLite rend des datetimes naïfs, écrits en UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


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


# Mesure minimale (étape C, lot C2, 6 nov. 2026, `counts.py`) : des totaux par jour, rien par
# personne ; aucune adresse IP, aucun identifiant.
daily_counts = Table(
    "daily_counts",
    metadata,
    Column("day", Date, primary_key=True),
    Column("name", String(48), primary_key=True),
    Column("value", Integer, nullable=False),
)

# Journal des tâches (spec › Backoffice, 7 nov. 2026, `jobs.py`) : une ligne par exécution d'une
# commande (cron Railway, outil lancé depuis le backoffice, CLI sur un poste), avec l'étape en
# cours et sa progression ; les événements (étapes, notes, avertissements) à côté. Gardé 400 jours.
jobs = Table(
    "jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("kind", String(32), nullable=False, index=True),  # warm, notify, seed…
    Column("args", Text, nullable=False, default="{}"),  # JSON : options de la commande
    Column("origin", String(16), nullable=False),  # cron | admin | cli
    Column("status", String(16), nullable=False, index=True),  # queued running ok failed cancelled
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    Column("heartbeat_at", DateTime(timezone=True)),
    Column("step_index", Integer),
    Column("step_total", Integer),
    Column("step_name", String(160)),
    Column("progress_done", Integer),
    Column("progress_total", Integer),
    Column("progress_unit", String(24)),
    Column("message", Text),  # dernière note ou détail
    Column("summary", Text),  # ligne de résumé finale
    Column("exit_code", Integer),
    Column("log_key", String(160)),  # journal complet dans le bucket (jobs/<id>.log)
    Column("worker", String(80)),
    Column("cancel_requested", Boolean, nullable=False, default=False),
)

job_events = Table(
    "job_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("job_id", String(36), nullable=False, index=True),
    Column("at", DateTime(timezone=True), nullable=False),
    Column("level", String(8), nullable=False),  # info | warn | error | step
    Column("text", Text, nullable=False),
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
                # mesure minimale : observations reçues ce jour-là, en nombre seulement
                from .counts import today

                day = today(now)
                found = sum(1 for r in fresh if r["found"])
                self._bump(conn, day, "observations_found", found)
                self._bump(conn, day, "observations_not_found", len(fresh) - found)
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

    # ------------------------------------------------------------------ mesure minimale

    def bump(self, day: date, name: str, n: int = 1) -> None:
        """Ajoute `n` au compteur du jour (plusieurs processus peuvent compter en même temps)."""
        with self.engine.begin() as conn:
            self._bump(conn, day, name, n)

    def set_count(self, day: date, name: str, value: int) -> None:
        """Relevé (état du jour) : remplace la valeur."""
        with self.engine.begin() as conn:
            self._write(conn, day, name, value, add=False)

    def _bump(self, conn: Connection, day: date, name: str, n: int) -> None:
        if n:
            self._write(conn, day, name, n, add=True)

    @staticmethod
    def _write(conn: Connection, day: date, name: str, value: int, add: bool) -> None:
        where = (daily_counts.c.day == day, daily_counts.c.name == name)
        new = daily_counts.c.value + value if add else value
        if conn.execute(update(daily_counts).where(*where).values(value=new)).rowcount:
            return
        try:
            with conn.begin_nested():  # un autre processus a pu créer la ligne entre-temps
                conn.execute(insert(daily_counts).values(day=day, name=name, value=value))
        except IntegrityError:
            conn.execute(update(daily_counts).where(*where).values(value=new))

    def counts_since(self, since: date) -> dict[date, dict[str, int]]:
        with self.engine.connect() as conn:
            res = conn.execute(select(daily_counts).where(daily_counts.c.day >= since))
            out: dict[date, dict[str, int]] = {}
            for r in res:
                out.setdefault(r.day, {})[r.name] = int(r.value)
            return out

    def purge_counts(self, before: date) -> int:
        """13 mois puis effacés (`counts.KEEP_DAYS`)."""
        with self.engine.begin() as conn:
            return conn.execute(delete(daily_counts).where(daily_counts.c.day < before)).rowcount

    def coin_totals(self) -> tuple[int, int]:
        """(appareils avec au moins un coin, nombre de coins)."""
        with self.engine.connect() as conn:
            row = conn.execute(
                select(func.count(func.distinct(coins.c.user_id)), func.count())
            ).one()
            return int(row[0]), int(row[1])

    def table_counts(self) -> dict[str, int]:
        """Lignes par table, pour la page Santé du backoffice."""
        with self.engine.connect() as conn:
            return {
                t.name: int(conn.execute(select(func.count()).select_from(t)).scalar_one())
                for t in (observations, coins, devices, daily_counts, jobs, job_events)
            }

    # ------------------------------------------------------------------ journal des tâches

    def create_job(
        self,
        kind: str,
        args: dict[str, Any],
        origin: str,
        now: datetime,
        job_id: str | None = None,
        status: str = "queued",
    ) -> str:
        job_id = job_id or str(uuid.uuid4())
        with self.engine.begin() as conn:
            conn.execute(
                insert(jobs).values(
                    id=job_id,
                    kind=kind,
                    args=json.dumps(args, ensure_ascii=False, sort_keys=True),
                    origin=origin,
                    status=status,
                    created_at=now.astimezone(UTC),
                    cancel_requested=False,
                )
            )
        return job_id

    def update_job(self, job_id: str, **fields: Any) -> None:
        """Champs d'avancement (`step_*`, `progress_*`, `message`, `heartbeat_at`…)."""
        if not fields:
            return
        with self.engine.begin() as conn:
            conn.execute(update(jobs).where(jobs.c.id == job_id).values(**fields))

    def start_job(self, job_id: str, worker: str, now: datetime) -> None:
        ts = now.astimezone(UTC)
        self.update_job(job_id, status="running", started_at=ts, heartbeat_at=ts, worker=worker)

    def finish_job(
        self,
        job_id: str,
        status: str,
        exit_code: int | None,
        now: datetime,
        summary: str | None = None,
    ) -> None:
        ts = now.astimezone(UTC)
        fields: dict[str, Any] = {
            "status": status,
            "exit_code": exit_code,
            "finished_at": ts,
            "heartbeat_at": ts,
        }
        if summary is not None:
            fields["summary"] = summary
        self.update_job(job_id, **fields)

    def add_event(self, job_id: str, level: str, text: str, now: datetime) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(job_events).values(
                    job_id=job_id, at=now.astimezone(UTC), level=level, text=text[:2000]
                )
            )

    def job(self, job_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            r = conn.execute(select(jobs).where(jobs.c.id == job_id)).first()
            return self._job(r) if r else None

    def list_jobs(self, limit: int = 50, kind: str | None = None) -> list[dict[str, Any]]:
        """Les plus récentes d'abord."""
        q = select(jobs).order_by(jobs.c.created_at.desc()).limit(limit)
        if kind:
            q = q.where(jobs.c.kind == kind)
        with self.engine.connect() as conn:
            return [self._job(r) for r in conn.execute(q)]

    def last_jobs_by_kind(self) -> dict[str, dict[str, Any]]:
        """La dernière exécution de chaque sorte de tâche (page Santé)."""
        out: dict[str, dict[str, Any]] = {}
        with self.engine.connect() as conn:
            for r in conn.execute(select(jobs).order_by(jobs.c.created_at.desc())):
                if r.kind not in out:
                    out[r.kind] = self._job(r)
        return out

    def events_of(self, job_id: str, limit: int = 500) -> list[dict[str, Any]]:
        """Dans l'ordre, les `limit` derniers."""
        q = (
            select(job_events)
            .where(job_events.c.job_id == job_id)
            .order_by(job_events.c.id.desc())
            .limit(limit)
        )
        with self.engine.connect() as conn:
            rows = conn.execute(q).all()
        return [{"at": _iso(r.at), "level": r.level, "text": r.text} for r in reversed(rows)]

    def purge_jobs(self, before: datetime) -> int:
        """400 jours, comme les compteurs : tâches et événements."""
        ts = before.astimezone(UTC)
        with self.engine.begin() as conn:
            old = [r[0] for r in conn.execute(select(jobs.c.id).where(jobs.c.created_at < ts))]
            if old:
                conn.execute(delete(job_events).where(job_events.c.job_id.in_(old)))
                conn.execute(delete(jobs).where(jobs.c.id.in_(old)))
        return len(old)

    @staticmethod
    def _job(r) -> dict[str, Any]:
        return {
            "id": r.id,
            "kind": r.kind,
            "args": json.loads(r.args or "{}"),
            "origin": r.origin,
            "status": r.status,
            "created_at": _iso(r.created_at),
            "started_at": _iso(r.started_at),
            "finished_at": _iso(r.finished_at),
            "heartbeat_at": _iso(r.heartbeat_at),
            "step_index": r.step_index,
            "step_total": r.step_total,
            "step_name": r.step_name,
            "progress_done": r.progress_done,
            "progress_total": r.progress_total,
            "progress_unit": r.progress_unit,
            "message": r.message,
            "summary": r.summary,
            "exit_code": r.exit_code,
            "log_key": r.log_key,
            "worker": r.worker,
            "cancel_requested": bool(r.cancel_requested),
        }

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
