"""Stockage SQLite du service météo : cache des prévisions brutes, historique accumulé,
climatologie du sol.

- `raw` : réponses prévision normalisées, par cellule et seau de 12 h. Si Open-Meteo est
  injoignable, `latest` sert la dernière réponse connue, marquée périmée par l'appelant.
- `history` : une ligne par (cellule, jour) pour les jours passés. Alimentée chaque jour par les
  ~60 jours de passé de la prévision (`source = forecast`) et, une fois, par l'amorçage ERA5-Land
  (`source = seed`, jamais écrasé par un amorçage ultérieur mais remplacé par la prévision quand
  elle couvre la date). Décision du 17 oct. 2026 : « amorcer, puis accumuler », l'API ne parle
  plus qu'à la prévision.
- `soil_clim` : 11 quantiles d'humidité du sol par cellule (amorçage), permanents : par mois (clés
  `1`…`12`) et, depuis le 7 nov. 2026, de l'année entière (clé `year`, dans le même JSON ; absente
  d'une base amorcée avant, l'API la dérive des mensuels).
- `cell_weather` : les onze jours détaillés (`M`, `M_eff`, `phi` par classe d'altitude) de chaque
  (cellule, espèce), rangés par la pré-chauffe, servis par `/weather/bbox` (2 nov. 2026).
- `cell_field` : dérivée de `cell_weather` dans la même transaction, une ligne par (espèce, jour,
  cellule) avec `f = M_eff × phi` arrondi par classe d'altitude ; c'est la seule table que lit
  `/weather/field` (spec › Points ouverts › Stockage compact par jour pour /weather/field,
  5 nov. 2026). `WITHOUT ROWID` : les lignes vivent dans l'arbre de la clé primaire, sans index
  à côté ; France, 7 espèces : ≈ 1,7 M de lignes, ≈ 170 Mo, dont une requête ne touche que les
  (espèce, jour) demandés, au lieu de parcourir les 326 Mo de `cell_weather` pour un jour.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from champis_pipeline.weather_store import SCHEMA

BUCKET_HOURS = 12  # deux rafraîchissements par jour (décision du 17 oct. 2026)
HISTORY_VARS = ("t_mean", "t_min", "t_max", "rain", "snow", "et0", "soil", "soil_t")
# Variables sans lesquelles un jour n'est pas rangé ; `soil_t` (température du sol, 9 nov. 2026)
# est optionnelle : `null` dans la ligne, absente des lignes écrites avant elle (sept valeurs)
REQUIRED_HISTORY_VARS = HISTORY_VARS[:7]


def _history_row(values_json: str) -> dict[str, float | None]:
    """Une ligne d'historique → variables ; une ligne plus courte (écrite avant une variable
    optionnelle) est complétée par `None`, jamais rejetée."""
    vals = json.loads(values_json)
    vals = [*vals, *([None] * (len(HISTORY_VARS) - len(vals)))]
    return dict(zip(HISTORY_VARS, vals, strict=True))


HISTORY_KEEP_DAYS = 400  # au-delà, plus aucune fenêtre du modèle ne lit
# Fenêtre des onze lignes d'une entrée `cell_weather` autour de son `today` : J−3 → J+7
# (`weather_service.DAYS_BEFORE` / `DAYS_AFTER`, repris ici pour ne pas importer le service).
CELL_WEATHER_DAYS_BEFORE = 3
CELL_WEATHER_DAYS_AFTER = 7
# Décimales de `f` dans `cell_field` et `/weather/field` : les seuils d'affichage sont au
# centième, la troisième décimale garde le classement entre cellules proches.
FIELD_DECIMALS = 3

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredCellWeather:
    """Météo précalculée d'une cellule pour une espèce : les onze lignes `{"date", "by_alt"}` de
    `/weather/bbox`, le `today` du calcul (les dates sont relatives à lui) et son horodatage."""

    today: str
    computed_at: str
    stale: bool
    rows_json: str  # le texte rangé, décodé à la demande seulement

    @property
    def rows(self) -> list[dict[str, Any]]:
        """Décodées à chaque accès : garder en mémoire les lignes de toute une emprise (plus
        d'un million d'objets pour 5 000 cellules) déclenche des passes complètes du ramasse-
        miettes, 0,25 s de gel chacune pour tous les fils (mesuré le 13 nov. 2026)."""
        return json.loads(self.rows_json)


@dataclass(frozen=True)
class StoredCellField:
    """Le champ compact d'une cellule pour un jour, assemblé par SQLite depuis ses lignes
    `cell_field` : `f[espèce][classe d'altitude] = M_eff × phi` arrondi, tel que `/weather/field`
    le sert ; `stale` si une des espèces vient d'un précalcul périmé, `computed_at` le plus
    récent des espèces."""

    computed_at: str
    stale: bool
    f: dict[str, dict[str, float]]


@dataclass(frozen=True)
class CellFieldStatus:
    """État de `cell_field` par rapport à `cell_weather`, pour `doctor` et la migration."""

    present: bool  # la table existe (base créée ou ouverte depuis le 5 nov. 2026)
    weather_entries: int
    weather_at: str | None  # MAX(computed_at) de `cell_weather`
    field_rows: int
    field_at: str | None  # MAX(computed_at) de `cell_field`

    @property
    def current(self) -> bool:
        """À jour : dérivée du même passage que `cell_weather` (même `computed_at` le plus
        récent), ou rien à dériver. Le nombre de lignes n'est pas 11 × les entrées : une entrée
        d'un passage antérieur n'a que ses jours encore dans la fenêtre courante."""
        if not self.present:
            return False
        if self.weather_entries == 0:
            return self.field_rows == 0
        return self.field_at == self.weather_at and self.field_rows > 0


def field_of(by_alt: dict[str, dict[str, float]]) -> dict[str, float]:
    """`f` d'une ligne `by_alt` : `M_eff × phi` arrondi à `FIELD_DECIMALS` par classe d'altitude,
    mêmes clés que `by_alt` (contrat de `/weather/field`, PR #75)."""
    return {alt: round(v["M_eff"] * v["phi"], FIELD_DECIMALS) for alt, v in by_alt.items()}


def cell_field_status(db: sqlite3.Connection) -> CellFieldStatus:
    """Lecture seule, sur n'importe quelle connexion (le `doctor` n'ouvre pas de `WeatherCache`
    pour ne rien créer dans une base d'avant la table)."""
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    w_n, w_at = db.execute("SELECT COUNT(*), MAX(computed_at) FROM cell_weather").fetchone()
    if "cell_field" not in tables:
        return CellFieldStatus(False, w_n, w_at, 0, None)
    f_n, f_at = db.execute("SELECT COUNT(*), MAX(computed_at) FROM cell_field").fetchone()
    return CellFieldStatus(True, w_n, w_at, f_n, f_at)


def _locked[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    """Méthode qui se sert de la connexion principale : un fil à la fois (`WeatherCache._lock`).

    Une méthode qui échoue ne laisse jamais de transaction ouverte (21 nov. 2026). `sqlite3`
    ouvre la transaction avant la première écriture et ne l'annule pas quand celle-ci échoue
    (verrou d'écriture tenu par un autre processus au-delà de `BUSY_TIMEOUT_S`) : la lecture
    suivante y figeait un instantané de la base, et dès qu'un autre worker avait écrit, toute
    écriture de cette connexion échouait aussitôt (« database is locked », sans attente),
    jusqu'au redémarrage du worker."""

    @functools.wraps(method)
    def locked(*args: P.args, **kwargs: P.kwargs) -> R:
        cache = args[0]
        with cache._lock:  # type: ignore[attr-defined]
            try:
                return method(*args, **kwargs)
            except BaseException:
                cache._rollback()  # type: ignore[attr-defined]
                raise

    return locked


def bucket_now(now: datetime) -> str:
    """Seau de 12 h en UTC : `2026-10-17T00` ou `2026-10-17T12`."""
    h = (now.hour // BUCKET_HOURS) * BUCKET_HOURS
    return now.astimezone(UTC).strftime("%Y-%m-%d") + f"T{h:02d}"


class WeatherCache:
    SQL_CHUNK = 500  # identifiants par requête `IN (...)`, loin sous la limite de variables SQLite

    BUSY_TIMEOUT_S = 30.0  # plusieurs workers uvicorn écrivent dans le même fichier (4 nov. 2026)
    # Tables copiées en bloc par `import_cache_db` : l'état complet d'une pré-chauffe
    TABLES = ("raw", "history", "soil_clim", "dept_index", "cell_weather", "cell_field", "meta")
    REBUILD_BATCH = 20_000  # lignes `cell_field` par `executemany` à la reconstruction

    def __init__(self, path: Path | str = ":memory:", shared: bool = False):
        """`shared` : le fichier est lu et écrit par plusieurs processus (cache du service,
        workers uvicorn, import en arrière-plan) → journal WAL, les lecteurs ne sont jamais
        bloqués par un écrivain ; une base d'amorçage, écrite puis téléversée, reste en mode
        classique (un seul fichier à tout moment).

        Fils (13 nov. 2026) : les routes lourdes lisent et calculent hors de la boucle
        d'événements. Une connexion `sqlite3` de Python ne se partage pas entre fils, même avec
        SQLite en mode sérialisé : deux exécutions simultanées de la même requête se partagent
        l'instruction préparée du cache de la connexion, et l'une lit avec les paramètres de
        l'autre (constaté par `test_offload.py`). Donc :

        - la connexion principale `db` ne sert qu'un fil à la fois, sous `_lock` (toutes les
          méthodes ; une écriture y est une transaction entière, close par son `commit`) ;
        - les grandes lectures des routes (`cell_weather`, `cell_field`, `dept_index`) passent
          par `_reading` : sur un cache partagé (fichier en WAL), chaque fil a sa propre
          connexion de lecture, qui ne bloque ni n'est bloquée par personne et ne retient pas
          `_lock` pendant une lecture d'une seconde ; sur une base en mémoire ou non partagée
          (tests, amorçage), la connexion principale sous `_lock`.

        `field_generation` compte les écritures de `cell_field` par cette instance : le cache
        en mémoire de `/weather/field` s'y fie pour se vider."""
        self._lock = threading.RLock()
        self._path = str(path)
        self._own_readers = shared and str(path) != ":memory:"
        self._readers: list[sqlite3.Connection] = []
        self._local = threading.local()
        self.field_generation = 0
        self.db = sqlite3.connect(str(path), timeout=self.BUSY_TIMEOUT_S, check_same_thread=False)
        if shared and str(path) != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")  # suffisant en WAL, bien plus rapide
        self.db.executescript(SCHEMA)  # schéma partagé avec la base d'amorçage du pipeline
        self.db.commit()

    def _rollback(self) -> None:
        """Annule la transaction qu'une méthode en échec laisserait ouverte (voir `_locked`)."""
        try:
            if self.db.in_transaction:
                self.db.rollback()
        except sqlite3.Error:  # connexion fermée ou inutilisable : l'erreur d'origine prime
            log.exception("cache : transaction en échec non annulée")

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        """Connexion pour une grande lecture : celle du fil courant sur un cache partagé (ouverte
        à la première demande, en lecture seule), sinon la principale sous `_lock`."""
        if not self._own_readers:
            with self._lock:
                yield self.db
            return
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(self._path, timeout=self.BUSY_TIMEOUT_S, check_same_thread=False)
            db.execute("PRAGMA query_only=ON")
            with self._lock:
                self._readers.append(db)
            self._local.db = db
        yield db

    # ------------------------------------------------------------------ prévisions brutes

    @_locked
    def get(self, cell_id: str, kind: str, bucket: str) -> Any | None:
        row = self.db.execute(
            "SELECT payload FROM raw WHERE cell_id = ? AND kind = ? AND bucket = ?",
            (cell_id, kind, bucket),
        ).fetchone()
        return json.loads(row[0]) if row else None

    @_locked
    def latest(self, cell_id: str, kind: str) -> tuple[Any, str] | None:
        """Dernière réponse connue (payload, fetched_at), quel que soit le seau."""
        row = self.db.execute(
            "SELECT payload, fetched_at FROM raw WHERE cell_id = ? AND kind = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (cell_id, kind),
        ).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    @_locked
    def put(self, cell_id: str, kind: str, bucket: str, payload: Any, now: datetime) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO raw VALUES (?, ?, ?, ?, ?)",
            (cell_id, kind, bucket, now.astimezone(UTC).isoformat(), json.dumps(payload)),
        )
        # On ne garde qu'un seau par (cellule, type) : le plus récent suffit au mode périmé.
        self.db.execute(
            "DELETE FROM raw WHERE cell_id = ? AND kind = ? AND bucket != ?",
            (cell_id, kind, bucket),
        )
        self.db.commit()

    # ------------------------------------------------------------------ historique accumulé

    @_locked
    def put_history(
        self, cell_id: str, rows: dict[str, dict[str, float | None]], source: str
    ) -> int:
        """Range des jours passés. `forecast` écrase ; `seed` ne remplit que les trous.

        Un jour dont une variable requise manque n'est pas rangé : l'historique ne contient que des
        jours complets, la règle « jamais d'approximation silencieuse » s'applique au moment du
        calcul. Les variables optionnelles (température du sol) sont rangées `null` si absentes.
        """
        verb = "INSERT OR REPLACE" if source == "forecast" else "INSERT OR IGNORE"
        n = 0
        for day, vals in rows.items():
            if any(vals.get(v) is None for v in REQUIRED_HISTORY_VARS):
                continue
            row = [None if vals.get(v) is None else float(vals[v]) for v in HISTORY_VARS]
            self.db.execute(
                f"{verb} INTO history VALUES (?, ?, ?, ?)",  # verbe contrôlé ci-dessus
                (cell_id, day, source, json.dumps(row)),
            )
            n += 1
        self.db.commit()
        return n

    @_locked
    def history(self, cell_id: str, start: date, end: date) -> dict[str, dict[str, float]]:
        rows = self.db.execute(
            "SELECT day, values_json FROM history WHERE cell_id = ? AND day BETWEEN ? AND ?",
            (cell_id, start.isoformat(), end.isoformat()),
        ).fetchall()
        return {day: _history_row(js) for day, js in rows}

    @_locked
    def history_days_with(self, cell_id: str, var: str) -> int:
        """Jours d'historique où la variable optionnelle `var` est renseignée (collecte d'une
        variable mise en service après l'amorçage : `soil_t` depuis le 9 nov. 2026)."""
        rows = self.db.execute(
            "SELECT values_json FROM history WHERE cell_id = ?", (cell_id,)
        ).fetchall()
        return sum(1 for (js,) in rows if _history_row(js).get(var) is not None)

    @_locked
    def last_history_day(self, cell_id: str) -> date | None:
        """Dernier jour présent dans l'historique (amorçage ou prévision), None si aucun."""
        row = self.db.execute(
            "SELECT MAX(day) FROM history WHERE cell_id = ?", (cell_id,)
        ).fetchone()
        return date.fromisoformat(row[0]) if row and row[0] else None

    @_locked
    def history_days(self, cell_id: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM history WHERE cell_id = ?", (cell_id,)
        ).fetchone()[0]

    @_locked
    def prune_history(self, today: date) -> None:
        self.db.execute(
            "DELETE FROM history WHERE day < ?",
            ((today - timedelta(days=HISTORY_KEEP_DAYS)).isoformat(),),
        )
        self.db.commit()

    # ------------------------------------------------------------------ climatologie du sol

    @_locked
    def put_soil_clim(
        self, cell_id: str, months: dict[str, list[float]], year: list[float] | None = None
    ) -> None:
        """`months` : 11 quantiles par mois civil ; `year` : les 11 quantiles de l'année entière,
        rangés sous la clé `year` du même JSON (None pour une climatologie d'avant le 7 nov. 2026)."""
        stored = {k: v for k, v in months.items() if k != "year"}
        if year is not None:
            stored["year"] = list(year)
        self.db.execute(
            "INSERT OR REPLACE INTO soil_clim VALUES (?, ?)", (cell_id, json.dumps(stored))
        )
        self.db.commit()

    @_locked
    def soil_clim(self, cell_id: str) -> dict[str, Any] | None:
        """`{"months": {"1": […], …}, "year": […] | None}` ; `year` manque dans une base amorcée
        avant le 7 nov. 2026 : `soil_percentile` le dérive alors des mensuels."""
        row = self.db.execute(
            "SELECT months_json FROM soil_clim WHERE cell_id = ?", (cell_id,)
        ).fetchone()
        if not row:
            return None
        stored = json.loads(row[0])
        return {
            "months": {k: v for k, v in stored.items() if k != "year"},
            "year": stored.get("year"),
        }

    @_locked
    def seeded_cells(self) -> int:
        """Cellules disposant d'une climatologie du sol, donc amorcées."""
        return self.db.execute("SELECT COUNT(*) FROM soil_clim").fetchone()[0]

    @_locked
    def seeded_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT cell_id FROM soil_clim")}

    @_locked
    def history_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT DISTINCT cell_id FROM history")}

    @_locked
    def import_seed_db(self, path: Path) -> int:
        """Importe une base d'amorçage (même schéma) par ATTACH : l'historique ne remplit que les
        trous (jamais par-dessus la prévision), la climatologie est remplacée. Rien en mémoire."""
        self.db.execute("ATTACH DATABASE ? AS seed", (str(path),))
        try:
            self.db.execute("INSERT OR IGNORE INTO history SELECT * FROM seed.history")
            self.db.execute("INSERT OR REPLACE INTO soil_clim SELECT * FROM seed.soil_clim")
            n = self.db.execute("SELECT COUNT(*) FROM seed.soil_clim").fetchone()[0]
            gen = self.db.execute(
                "SELECT value FROM seed.meta WHERE key = 'seed_generated'"
            ).fetchone()
            if gen:
                self.meta_set("seed_generated", gen[0])
            self.db.commit()
        finally:
            self.db.execute("DETACH DATABASE seed")
        return n

    @_locked
    def import_cache_db(self, path: Path) -> dict[str, int]:
        """Remplace tout le contenu par celui d'une base au même schéma (cache publié par le
        service cron, `champis-api warm --publish`), en une transaction : les lecteurs voient
        l'ancien état, puis le nouveau, jamais un mélange. Renvoie le nombre de lignes par table.

        Un instantané d'avant `cell_field` (cron déployé avant le 5 nov. 2026) n'a pas la table,
        ou l'a vide : elle est dérivée de `cell_weather` dans la même transaction
        (`rebuild_cell_field`), dans le fil de l'import, jamais dans la boucle d'événements."""
        self.db.execute("ATTACH DATABASE ? AS pub", (str(path),))
        counts: dict[str, int] = {}
        try:
            pub_tables = {
                r[0]
                for r in self.db.execute("SELECT name FROM pub.sqlite_master WHERE type = 'table'")
            }
            for table in self.TABLES:  # noms contrôlés ci-dessus
                self.db.execute(f"DELETE FROM {table}")
                if table in pub_tables:
                    self.db.execute(f"INSERT INTO {table} SELECT * FROM pub.{table}")
                counts[table] = self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            if counts["cell_weather"] and not counts["cell_field"]:
                log.info("instantané sans cell_field : dérivée de cell_weather à l'import")
                counts["cell_field"] = self._rebuild_cell_field()
            self.db.commit()
            self.field_generation += 1
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self.db.execute("DETACH DATABASE pub")
        return counts

    @_locked
    def snapshot(self, path: Path) -> None:
        """Copie compacte et cohérente de la base dans `path` (`VACUUM INTO`) : un seul fichier,
        sans journal WAL à côté, prêt à être téléversé."""
        path.unlink(missing_ok=True)
        self.db.execute("VACUUM INTO ?", (str(path),))

    # ------------------------------------------------------------------ indice par département

    META_DEPT_INDEX_AT = "dept_index_computed_at"
    META_DEPT_INDEX_TODAY = "dept_index_today"

    @_locked
    def put_dept_index(
        self, rows: list[tuple[str, str, str, float | None, float]], today: date, now: datetime
    ) -> None:
        """Remplace l'indice entier : (département, espèce, jour ISO, valeur ou None, couverture)."""
        self.db.execute("DELETE FROM dept_index")
        self.db.executemany("INSERT INTO dept_index VALUES (?, ?, ?, ?, ?)", rows)
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)",
            (self.META_DEPT_INDEX_AT, now.astimezone(UTC).isoformat(timespec="seconds")),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO meta VALUES (?, ?)",
            (self.META_DEPT_INDEX_TODAY, today.isoformat()),
        )
        self.db.commit()

    def dept_index(
        self, species: list[str] | None = None, day: date | None = None
    ) -> list[tuple[str, str, str, float | None, float]]:
        sql = "SELECT dept, species, day, value, coverage FROM dept_index"
        where, params = [], []
        if species:
            where.append(f"species IN ({','.join('?' * len(species))})")
            params += species
        if day is not None:
            where.append("day = ?")
            params.append(day.isoformat())
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._reading() as db:
            return db.execute(sql + " ORDER BY dept, species, day", params).fetchall()

    @_locked
    def dept_index_meta(self) -> tuple[str, str] | None:
        """(computed_at, today) du dernier calcul, None si jamais calculé."""
        at, today = (
            self.meta_get(self.META_DEPT_INDEX_AT),
            self.meta_get(self.META_DEPT_INDEX_TODAY),
        )
        return (at, today) if at and today else None

    # ------------------------------------------------------------------ météo par cellule

    @_locked
    def put_cell_weather(
        self,
        entries: Iterable[tuple[str, str, bool, list[dict[str, Any]]]],
        today: date,
        now: datetime,
    ) -> int:
        """Range les lignes `M`, `M_eff`, `phi` par jour et classe d'altitude d'une cellule et
        d'une espèce, telles que `/weather/bbox` les sert : (cellule, espèce, périmé, lignes).
        Remplace l'entrée existante de la paire ; renvoie le nombre d'entrées écrites.

        Dans la même transaction, chaque jour de l'entrée devient une ligne de `cell_field`
        (`f = M_eff × phi` arrondi par classe d'altitude), remplacée par (espèce, jour, cellule) :
        `/weather/field` ne lit que cette table. Les jours d'un passage antérieur sortis de la
        fenêtre courante sont purgés une fois par passe (`prune_cell_field`), pas ici."""
        at = now.astimezone(UTC).isoformat(timespec="seconds")
        rows, field_rows = [], []
        for cell_id, species, stale, day_rows in entries:
            rows.append((cell_id, species, today.isoformat(), at, int(stale), json.dumps(day_rows)))
            field_rows.extend(
                (cell_id, species, row["date"], at, int(stale), json.dumps(field_of(row["by_alt"])))
                for row in day_rows
            )
        self.db.executemany("INSERT OR REPLACE INTO cell_weather VALUES (?, ?, ?, ?, ?, ?)", rows)
        self.db.executemany(
            "INSERT OR REPLACE INTO cell_field VALUES (?, ?, ?, ?, ?, ?)", field_rows
        )
        self.db.commit()
        self.field_generation += bool(rows)
        return len(rows)

    def cell_weather(
        self, cell_ids: Iterable[str], species: Iterable[str]
    ) -> dict[tuple[str, str], StoredCellWeather]:
        """Les entrées précalculées pour ces cellules et ces espèces, par (cellule, espèce) ;
        absentes : jamais calculées (cellule nouvelle, indisponible à la pré-chauffe)."""
        ids = list(cell_ids)
        sp = list(species)
        if not ids or not sp:
            return {}
        out: dict[tuple[str, str], StoredCellWeather] = {}
        sp_sql = ",".join("?" * len(sp))
        for i in range(0, len(ids), self.SQL_CHUNK):
            chunk = ids[i : i + self.SQL_CHUNK]
            with self._reading() as db:  # par lot : jamais `_lock` le temps de toute l'emprise
                rows = db.execute(
                    "SELECT cell_id, species, today, computed_at, stale, rows_json "
                    f"FROM cell_weather WHERE species IN ({sp_sql}) "
                    f"AND cell_id IN ({','.join('?' * len(chunk))})",
                    sp + chunk,
                ).fetchall()
            for cell_id, sid, today, at, stale, rows_json in rows:
                out[cell_id, sid] = StoredCellWeather(today, at, bool(stale), rows_json)
        return out

    @_locked
    def cell_weather_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM cell_weather").fetchone()[0]

    # ------------------------------------------------------------------ champ compact par jour

    def cell_field(
        self, cell_ids: Iterable[str], species: Iterable[str], day: date
    ) -> dict[str, StoredCellField]:
        """Le champ compact du jour `day` pour ces cellules et ces espèces, par cellule, pour
        `/weather/field` sur une emprise de plusieurs milliers de cellules.

        Une seule requête, quelle que soit l'emprise, sur la clé primaire (espèce, jour, cellule) :
        les identifiants passent en un tableau JSON (`json_each`), et SQLite assemble lui-même
        `f` par cellule (`json_group_object`), un seul `json.loads` par cellule au lieu d'un par
        espèce (France : 22 672 au lieu de 90 688, ≈ 0,2 s de moins). Une cellule sans ligne pour
        `day` et **toutes** les espèces demandées (jamais calculée, J+7 entre minuit et la
        pré-chauffe, espèce manquante) est absente du résultat. Les clés de `f` suivent l'ordre de
        l'index (espèces triées), pas celui de la requête : un objet JSON n'est pas ordonné.
        """
        ids = list(cell_ids)
        sp = list(species)
        if not ids or not sp:
            return {}
        with self._reading() as db:
            rows = db.execute(
                "SELECT cell_id, MAX(computed_at), MAX(stale), "
                "json_group_object(species, json(f)) FROM cell_field "
                f"WHERE species IN ({','.join('?' * len(sp))}) AND date = ? "
                "AND cell_id IN (SELECT value FROM json_each(?)) "
                "GROUP BY cell_id HAVING COUNT(*) = ?",
                [*sp, day.isoformat(), json.dumps(ids), len(sp)],
            ).fetchall()
        return {
            cell_id: StoredCellField(at, bool(stale), json.loads(f))
            for cell_id, at, stale, f in rows
        }

    @_locked
    def prune_cell_field(self, today: date) -> int:
        """Purge les jours hors de la fenêtre J−3 → J+7 de `today` (ceux des passages antérieurs
        que le remplacement par (espèce, jour, cellule) n'a pas recouverts). Un parcours de la
        table, une fois par passe de pré-chauffe ; renvoie le nombre de lignes supprimées."""
        first = (today - timedelta(days=CELL_WEATHER_DAYS_BEFORE)).isoformat()
        last = (today + timedelta(days=CELL_WEATHER_DAYS_AFTER)).isoformat()
        cur = self.db.execute("DELETE FROM cell_field WHERE date < ? OR date > ?", (first, last))
        self.db.commit()
        self.field_generation += bool(cur.rowcount)
        return cur.rowcount

    @_locked
    def cell_field_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM cell_field").fetchone()[0]

    @_locked
    def cell_field_status(self) -> CellFieldStatus:
        return cell_field_status(self.db)

    def _rebuild_cell_field(self) -> int:
        """Vide `cell_field` et la re-dérive de `cell_weather`, sans valider : l'appelant tient la
        transaction. Les jours hors de la fenêtre du `today` le plus récent sont écartés."""
        self.db.execute("DELETE FROM cell_field")
        latest = self.db.execute("SELECT MAX(today) FROM cell_weather").fetchone()[0]
        if latest is None:
            return 0
        today = date.fromisoformat(latest)
        first = (today - timedelta(days=CELL_WEATHER_DAYS_BEFORE)).isoformat()
        last = (today + timedelta(days=CELL_WEATHER_DAYS_AFTER)).isoformat()
        n = 0
        batch: list[tuple[str, str, str, str, int, str]] = []
        cursor = self.db.execute(
            "SELECT cell_id, species, computed_at, stale, rows_json FROM cell_weather"
        )
        for cell_id, species, at, stale, rows_json in cursor:
            for row in json.loads(rows_json):
                if first <= row["date"] <= last:
                    batch.append(
                        (
                            cell_id,
                            species,
                            row["date"],
                            at,
                            stale,
                            json.dumps(field_of(row["by_alt"])),
                        )
                    )
            if len(batch) >= self.REBUILD_BATCH:
                self.db.executemany(
                    "INSERT OR REPLACE INTO cell_field VALUES (?, ?, ?, ?, ?, ?)", batch
                )
                n += len(batch)
                batch.clear()
        if batch:
            self.db.executemany(
                "INSERT OR REPLACE INTO cell_field VALUES (?, ?, ?, ?, ?, ?)", batch
            )
            n += len(batch)
        return n

    @_locked
    def rebuild_cell_field(self) -> int:
        """Migration : dérive `cell_field` de `cell_weather` (base d'une API déployée avant la
        table, instantané ancien). Idempotente, en une transaction, journalisée ; sur la France
        ≈ 1 M de lignes à écrire, de l'ordre de la minute, à faire dans un fil (démarrage, import),
        jamais dans la boucle d'événements. Renvoie le nombre de lignes écrites."""
        t0 = time.perf_counter()
        try:
            n = self._rebuild_cell_field()
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self.field_generation += 1
        log.info(
            "cell_field reconstruite depuis cell_weather : %d lignes en %.1f s",
            n,
            time.perf_counter() - t0,
        )
        return n

    @_locked
    def ensure_cell_field(self) -> int | None:
        """Au démarrage : reconstruit `cell_field` si elle n'est pas dérivée du même passage que
        `cell_weather` (base d'avant la table). Renvoie le nombre de lignes écrites, None si rien
        à faire."""
        status = self.cell_field_status()
        if status.current:
            return None
        log.info(
            "cell_field absente ou en retard (%d lignes, %s) sur cell_weather (%d entrées, %s)",
            status.field_rows,
            status.field_at,
            status.weather_entries,
            status.weather_at,
        )
        return self.rebuild_cell_field()

    # ------------------------------------------------------------------ métadonnées

    @_locked
    def meta_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    @_locked
    def meta_set(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
        self.db.commit()

    @_locked
    def close(self) -> None:
        for reader in self._readers:
            reader.close()
        self._readers.clear()
        self._local = threading.local()
        self.db.close()
