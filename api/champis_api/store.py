"""Observations terrain, favoris (« mes coins ») et jetons APNs : Postgres sur Railway.

Spec › Backend (30 oct. 2026) : Postgres Railway pour les observations, même base et même
identité pour les coins ; identité = identifiant généré par l'app, envoyé en en-tête. Pas de
photo, pas de partage entre utilisateurs (`shared` réservé, toujours faux). Accès par SQLAlchemy
Core : la même table se crée dans Postgres (`DATABASE_URL` de Railway) et dans SQLite (tests,
développement sans Postgres), le code ne porte aucun SQL spécifique à un moteur.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    SmallInteger,
    String,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool

log = logging.getLogger(__name__)
metadata = MetaData()
# Verrou consultatif Postgres de la création des tables et de la migration des clés primaires
# (un entier arbitraire, fixe)
MIGRATION_LOCK = 0x43484D50  # « CHMP »
MIGRATION_LOCK_TIMEOUT = "10s"


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
    Column("id", String(36), nullable=False),  # UUID généré par l'app : idempotence
    Column("user_id", String(36), nullable=False, index=True),
    # un identifiant d'observation n'est unique que pour son appareil (`_migrate_owner_keys`)
    PrimaryKeyConstraint("user_id", "id"),
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
    Column("id", String(36), nullable=False),
    Column("user_id", String(36), nullable=False, index=True),
    PrimaryKeyConstraint("user_id", "id"),
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

# Appareils actifs (spec › Backoffice › Usage, tranché le 8 nov. 2026, `counts.py`) : une ligne
# par appareil de l'app et par jour, l'appareil réduit à une empreinte HMAC (secret serveur),
# différente chaque jour depuis le 14 nov. 2026, jamais l'identifiant lui-même ; non recoupable
# avec les coins ni les observations, ni d'un jour à l'autre. 400 jours.
device_days = Table(
    "device_days",
    metadata,
    Column("day", Date, primary_key=True),
    Column("device_hash", String(32), primary_key=True),
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

# Service d'exécution (lot BO3) : un battement par worker, pour la page Santé.
WORKER_FORGET = timedelta(hours=1)  # un worker muet depuis autant est un conteneur remplacé
workers = Table(
    "workers",
    metadata,
    Column("name", String(80), primary_key=True),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("heartbeat_at", DateTime(timezone=True), nullable=False),
    Column("current_job", String(36)),
    Column("disk_free", Integer),  # Mo
    Column("disk_total", Integer),  # Mo
    Column("build", String(80)),
    # lot BO5 : inventaire du volume (noms des T et PMTiles présents, état du batch), JSON
    Column("inventory", Text),
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


class QuotaExceededError(Exception):
    """Le lot ferait dépasser le quota d'observations du jour de cet identifiant."""

    def __init__(self, quota: int, used: int) -> None:
        super().__init__(f"quota de {quota} observations par jour atteint ({used} aujourd'hui)")
        self.quota, self.used = quota, used


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
        self._create_tables()
        self._add_missing_columns()
        # vrai quand observations et coins ont leur clé primaire `(user_id, id)`
        self.owner_keys = self._migrate_owner_keys()

    def _create_tables(self) -> None:
        """`create_all`, un processus à la fois sur Postgres : les workers de l'API, le service
        d'exécution et les crons ouvrent la base au même déploiement, et deux `CREATE TABLE` de
        la même table nouvelle se heurtaient (« duplicate key … pg_type_typname_nsp_index »)."""
        if self.dialect != "postgresql":
            metadata.create_all(self.engine)
            return
        with self.engine.begin() as conn:
            conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MIGRATION_LOCK})
            metadata.create_all(conn)

    def _add_missing_columns(self) -> None:
        """`create_all` ne touche pas une table qui existe : une colonne ajoutée au schéma (comme
        `workers.inventory`, lot BO5) est créée ici par `ALTER TABLE … ADD COLUMN`, nullable, sans
        valeur par défaut. Les colonnes retirées ou changées ne sont pas gérées (aucune à ce jour).
        L'API et le worker ouvrent la base en même temps : un ajout qui échoue parce qu'un autre
        processus vient de le faire est sans conséquence."""
        for table in metadata.sorted_tables:
            present = {c["name"] for c in inspect(self.engine).get_columns(table.name)}
            for col in table.columns:
                if col.name in present:
                    continue
                ctype = col.type.compile(dialect=self.engine.dialect)
                try:
                    with self.engine.begin() as conn:
                        conn.execute(
                            text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ctype}')
                        )
                except Exception:
                    now = {c["name"] for c in inspect(self.engine).get_columns(table.name)}
                    if col.name not in now:
                        raise

    def _migrate_owner_keys(self) -> bool:
        """Clé primaire `(user_id, id)` pour `observations` et `coins` (14 nov. 2026).

        Jusque-là la clé était `id` seul : un appareil qui envoyait l'identifiant d'un autre
        l'empêchait de l'enregistrer (et recevait un 500 pour ses coins). `create_all` ne touche
        pas une table qui existe : celles d'avant sont migrées ici, une fois.

        - Postgres : `ALTER TABLE … DROP CONSTRAINT <clé>, ADD PRIMARY KEY (user_id, id)`, une
          seule instruction dans une transaction. L'ancienne clé garantit déjà l'unicité de `id`,
          donc celle de `(user_id, id)` : la nouvelle ne peut pas être refusée par les données.
          API, worker et crons ouvrent la base en même temps : un verrou consultatif de transaction
          les met en file, et l'état est relu sous le verrou. `lock_timeout` borne l'attente du
          verrou de table.
        - SQLite (tests, développement) ne sait pas changer une clé primaire : la table est
          recréée et recopiée dans une transaction (`BEGIN IMMEDIATE`, relu sous le verrou).

        Idempotente. Un échec est journalisé et n'empêche pas le service : le code fonctionne
        avec l'ancienne clé (comportement d'avant), la migration est retentée au prochain
        démarrage et `champis-api doctor` le signale."""
        done = True
        for table in (observations, coins):
            try:
                if self._owner_key(self.engine, table):
                    continue
                with self.engine.begin() as conn:
                    if self.dialect == "postgresql":
                        self._migrate_postgres(conn, table)
                    else:
                        self._migrate_sqlite(conn, table)
            except Exception:
                done = False
                log.exception(
                    "base : clé primaire (user_id, id) de %s non posée ; l'ancienne clé reste en "
                    "service, nouvel essai au prochain démarrage",
                    table.name,
                )
        return done

    @staticmethod
    def _owner_key(bind, table: Table) -> bool:
        columns = inspect(bind).get_pk_constraint(table.name).get("constrained_columns") or []
        return set(columns) == {"user_id", "id"}

    def _migrate_postgres(self, conn: Connection, table: Table) -> None:
        conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": MIGRATION_LOCK})
        if self._owner_key(conn, table):  # un autre processus vient de le faire
            return
        name = inspect(conn).get_pk_constraint(table.name).get("name")
        conn.execute(text(f"SET LOCAL lock_timeout = '{MIGRATION_LOCK_TIMEOUT}'"))
        drop = f'DROP CONSTRAINT "{name}", ' if name else ""
        conn.execute(text(f'ALTER TABLE "{table.name}" {drop}ADD PRIMARY KEY (user_id, id)'))
        log.info("base : clé primaire (user_id, id) posée sur %s", table.name)

    def _migrate_sqlite(self, conn: Connection, table: Table) -> None:
        # pysqlite n'ouvre pas de transaction avant un DDL : on la demande, avec le verrou
        # d'écriture ; `engine.begin()` la valide ou l'annule en sortie
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        if self._owner_key(conn, table):
            return
        old = f"{table.name}_avant_cle"
        names = ", ".join(f'"{c.name}"' for c in table.columns)
        for index in inspect(conn).get_indexes(table.name):  # ils suivraient l'ancienne table
            conn.exec_driver_sql(f'DROP INDEX "{index["name"]}"')
        conn.exec_driver_sql(f'ALTER TABLE "{table.name}" RENAME TO "{old}"')
        table.create(conn)
        n = conn.exec_driver_sql(
            f'INSERT INTO "{table.name}" ({names}) SELECT {names} FROM "{old}"'
        ).rowcount
        conn.exec_driver_sql(f'DROP TABLE "{old}"')
        log.info("base : clé primaire (user_id, id) posée sur %s (%d lignes)", table.name, n)

    @property
    def dialect(self) -> str:
        return self.engine.dialect.name

    # ------------------------------------------------------------------ observations

    def add_observations(
        self, user_id: str, rows: list[dict[str, Any]], now: datetime, quota: int = 0
    ) -> dict:
        """Insère les observations absentes (par `id`, pour cet appareil) ; les déjà connues sont
        ignorées, pas écrasées : un renvoi après coupure réseau est sans effet. Un `id` répété
        dans le lot ne compte qu'une fois (le premier), les suivants sont des doublons.

        `quota` (0 = aucun) : nombre d'observations nouvelles qu'un identifiant peut enregistrer
        par jour civil (heure de Paris). Un lot qui le ferait dépasser est refusé en entier
        (`QuotaExceededError`), rien n'est écrit : l'app le garde et le renvoie tel quel le
        lendemain. Un renvoi de lignes déjà connues n'est jamais refusé."""
        if not rows:
            return {"inserted": 0, "duplicates": 0}
        unique = {r["id"]: r for r in reversed(rows)}  # le premier de chaque `id` l'emporte
        with self.engine.begin() as conn:
            known = {
                r[0]
                for r in conn.execute(
                    select(observations.c.id).where(
                        observations.c.user_id == user_id, observations.c.id.in_(list(unique))
                    )
                )
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
                for r in reversed(unique.values())
                if r["id"] not in known
            ]
            if fresh:
                from .counts import day_start, today

                day = today(now)
                if quota > 0:
                    used = conn.execute(
                        select(func.count())
                        .select_from(observations)
                        .where(
                            observations.c.user_id == user_id,
                            observations.c.created_at >= day_start(day).astimezone(UTC),
                        )
                    ).scalar_one()
                    if used + len(fresh) > quota:
                        raise QuotaExceededError(quota, int(used))
                conn.execute(insert(observations), fresh)
                # mesure minimale : observations reçues ce jour-là, en nombre seulement
                found = sum(1 for r in fresh if r["found"])
                self._bump(conn, day, "observations_found", found)
                self._bump(conn, day, "observations_not_found", len(fresh) - found)
        return {"inserted": len(fresh), "duplicates": len(rows) - len(fresh)}

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

    def delete_observations(self, user_id: str, device_prints: list[str] | None = None) -> int:
        """Droit à l'effacement : toutes les lignes de l'identifiant, observations, coins et jeton,
        et les empreintes de l'appareil dans `device_days` (`counts.device_prints` : celle de
        chaque jour conservé et l'ancienne, que seul l'appelant sait recalculer)."""
        with self.engine.begin() as conn:
            n = conn.execute(delete(observations).where(observations.c.user_id == user_id)).rowcount
            conn.execute(delete(coins).where(coins.c.user_id == user_id))
            conn.execute(delete(devices).where(devices.c.user_id == user_id))
            if device_prints:
                conn.execute(
                    delete(device_days).where(device_days.c.device_hash.in_(device_prints))
                )
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
        """Remplace la liste complète des coins de l'appareil (pas de fusion) et son jeton APNs.
        Un `id` répété dans la liste ne donne qu'un coin : le dernier."""
        ts = now.astimezone(UTC)
        rows = list({r["id"]: r for r in rows}.values())
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
                for t in (
                    observations,
                    coins,
                    devices,
                    daily_counts,
                    device_days,
                    jobs,
                    job_events,
                    workers,
                )
            }

    # ------------------------------------------------------------------ appareils actifs

    def mark_device_day(self, day: date, device_hash: str) -> bool:
        """Un appareil vu ce jour-là ; vrai s'il ne l'avait pas encore été (plusieurs workers
        peuvent le voir en même temps)."""
        with self.engine.begin() as conn:
            try:
                with conn.begin_nested():
                    conn.execute(insert(device_days).values(day=day, device_hash=device_hash))
            except IntegrityError:
                return False
        return True

    def active_devices(self, day: date) -> int:
        """Appareils vus ce jour-là. L'empreinte change chaque jour (`counts.device_hash`) : il
        n'y a pas de compte d'appareils distincts sur plusieurs jours."""
        with self.engine.connect() as conn:
            return int(
                conn.execute(
                    select(func.count()).select_from(device_days).where(device_days.c.day == day)
                ).scalar_one()
            )

    def has_legacy_device_rows(self, day: date) -> bool:
        """Ce jour a-t-il des lignes à l'ancienne empreinte (la même tous les jours) ?"""
        from .counts import DAILY_PREFIX

        with self.engine.connect() as conn:
            row = conn.execute(
                select(device_days.c.device_hash)
                .where(
                    device_days.c.day == day,
                    device_days.c.device_hash.not_like(f"{DAILY_PREFIX}%"),
                )
                .limit(1)
            ).first()
        return row is not None

    def daily_active_devices(self, since: date) -> dict[date, int]:
        """Appareils distincts par jour depuis `since`."""
        with self.engine.connect() as conn:
            res = conn.execute(
                select(device_days.c.day, func.count())
                .where(device_days.c.day >= since)
                .group_by(device_days.c.day)
            )
            return {r[0]: int(r[1]) for r in res}

    def purge_device_days(self, before: date) -> int:
        with self.engine.begin() as conn:
            return conn.execute(delete(device_days).where(device_days.c.day < before)).rowcount

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

    JOB_SORTS = ("created_at", "kind", "status", "origin")

    @staticmethod
    def _jobs_where(q, kind, status, origin, search):
        if kind:
            q = q.where(jobs.c.kind == kind)
        if status:
            q = q.where(jobs.c.status == status)
        if origin:
            q = q.where(jobs.c.origin == origin)
        if search:
            like = f"%{search}%"
            q = q.where(
                or_(
                    jobs.c.id.like(f"{search}%"),
                    jobs.c.args.like(like),
                    jobs.c.summary.like(like),
                    jobs.c.message.like(like),
                )
            )
        return q

    def list_jobs(
        self,
        limit: int = 50,
        kind: str | None = None,
        *,
        offset: int = 0,
        status: str | None = None,
        origin: str | None = None,
        search: str | None = None,
        sort: str = "created_at",
        descending: bool = True,
    ) -> list[dict[str, Any]]:
        """Les plus récentes d'abord ; filtres, tri et page pour la liste du backoffice (`search`
        cherche dans le début de l'identifiant, les options, le résumé et le dernier message)."""
        column = jobs.c[sort if sort in self.JOB_SORTS else "created_at"]
        order = [column.desc() if descending else column.asc()]
        if column is not jobs.c.created_at:
            order.append(jobs.c.created_at.desc())
        q = self._jobs_where(select(jobs), kind, status, origin, search)
        q = q.order_by(*order).limit(limit).offset(offset)
        with self.engine.connect() as conn:
            return [self._job(r) for r in conn.execute(q)]

    def count_jobs(
        self,
        kind: str | None = None,
        status: str | None = None,
        origin: str | None = None,
        search: str | None = None,
    ) -> int:
        q = self._jobs_where(select(func.count()).select_from(jobs), kind, status, origin, search)
        with self.engine.connect() as conn:
            return int(conn.execute(q).scalar() or 0)

    def last_jobs_by_kind(self) -> dict[str, dict[str, Any]]:
        """La dernière exécution de chaque sorte de tâche (page Santé)."""
        out: dict[str, dict[str, Any]] = {}
        with self.engine.connect() as conn:
            for r in conn.execute(select(jobs).order_by(jobs.c.created_at.desc())):
                if r.kind not in out:
                    out[r.kind] = self._job(r)
        return out

    def jobs_with_status(self, statuses: tuple[str, ...]) -> list[dict[str, Any]]:
        """Les tâches dans l'un de ces états, les plus anciennes d'abord (file du worker)."""
        q = select(jobs).where(jobs.c.status.in_(statuses)).order_by(jobs.c.created_at.asc())
        with self.engine.connect() as conn:
            return [self._job(r) for r in conn.execute(q)]

    def claim_job(self, job_id: str, worker: str, now: datetime) -> bool:
        """Le worker prend une tâche `queued` ; vrai s'il l'a eue (un seul worker la prend)."""
        ts = now.astimezone(UTC)
        with self.engine.begin() as conn:
            n = conn.execute(
                update(jobs)
                .where(jobs.c.id == job_id, jobs.c.status == "queued")
                .values(status="running", started_at=ts, heartbeat_at=ts, worker=worker)
            ).rowcount
        return n == 1

    def request_cancel(self, job_id: str, now: datetime) -> str | None:
        """Annule une tâche : `queued` → `cancelled` tout de suite ; `running` → le worker arrête
        le sous-processus. Renvoie le nouvel état, None si la tâche n'existe pas ou est finie."""
        ts = now.astimezone(UTC)
        with self.engine.begin() as conn:
            row = conn.execute(select(jobs.c.status).where(jobs.c.id == job_id)).first()
            if row is None:
                return None
            if row[0] == "queued":
                conn.execute(
                    update(jobs)
                    .where(jobs.c.id == job_id)
                    .values(status="cancelled", finished_at=ts, cancel_requested=True)
                )
                return "cancelled"
            if row[0] == "running":
                conn.execute(update(jobs).where(jobs.c.id == job_id).values(cancel_requested=True))
                return "running"
        return None

    # ------------------------------------------------------------------ service d'exécution

    def worker_heartbeat(
        self,
        name: str,
        now: datetime,
        current_job: str | None,
        disk_free_mb: int | None,
        disk_total_mb: int | None,
        build: str = "",
        started_at: datetime | None = None,
        inventory: dict[str, Any] | None = None,
    ) -> None:
        ts = now.astimezone(UTC)
        values: dict[str, Any] = {
            "heartbeat_at": ts,
            "current_job": current_job,
            "disk_free": disk_free_mb,
            "disk_total": disk_total_mb,
            "build": build[:80],
        }
        if inventory is not None:
            values["inventory"] = json.dumps(inventory, ensure_ascii=False)
        with self.engine.begin() as conn:
            # un conteneur remplacé par un déploiement ne bat plus jamais : sa ligne s'oublie
            conn.execute(
                delete(workers).where(
                    workers.c.name != name, workers.c.heartbeat_at < ts - WORKER_FORGET
                )
            )
            if conn.execute(
                update(workers).where(workers.c.name == name).values(**values)
            ).rowcount:
                return
            conn.execute(
                insert(workers).values(
                    name=name, started_at=(started_at or now).astimezone(UTC), **values
                )
            )

    def list_workers(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            return [
                {
                    "name": r.name,
                    "started_at": _iso(r.started_at),
                    "heartbeat_at": _iso(r.heartbeat_at),
                    "current_job": r.current_job,
                    "disk_free_mb": r.disk_free,
                    "disk_total_mb": r.disk_total,
                    "build": r.build,
                    "inventory": json.loads(r.inventory) if r.inventory else None,
                }
                for r in conn.execute(select(workers).order_by(workers.c.heartbeat_at.desc()))
            ]

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
